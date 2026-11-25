"""Transcription module with two pluggable backends, selected by
config/settings.yaml: transcription.backend ("local" | "openai").

- LocalWhisperTranscriber: runs faster-whisper on this machine. No network
  call, no API key, but requires the optional `local-whisper` extra and a
  downloaded model on first use.
- OpenAIWhisperTranscriber: uploads the media file to OpenAI's hosted
  Whisper API. Requires OPENAI_API_KEY.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

import httpx

from reel_pipeline.config import Settings
from reel_pipeline.logging_setup import get_logger, log_context
from reel_pipeline.models import TranscriptResult

logger = get_logger(__name__)


class Transcriber(Protocol):
    def transcribe(self, media_path: Path, content_id: str) -> TranscriptResult: ...


class TranscriptionError(RuntimeError):
    """Raised when a transcription backend fails to produce text."""


# Process-lifetime cache of loaded WhisperModel instances, keyed by the resolved
# (model_size, device, compute_type). Loading a model re-reads weights from disk and
# re-initializes the CTranslate2 runtime (and GPU context, if device="cuda") - without
# this cache, a long-lived webhook server process pays that cost on every single
# transcribe() call, since build_worker() constructs a fresh LocalWhisperTranscriber
# per run_once() (see worker.py) rather than reusing one across the process lifetime.
_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}


def _get_whisper_model(model_size: str, device: str, compute_type: str) -> Any:
    from faster_whisper import WhisperModel  # type: ignore[import-not-found]

    key = (model_size, device, compute_type)
    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = WhisperModel(model_size, device=device, compute_type=compute_type)
    return _MODEL_CACHE[key]


# Chunked fallback for audio too long to transcribe in one pass. 10 min of 16 kHz
# audio is a ~190 MB spectrogram, versus ~1.1 GiB for the hour-long video that failed.
_SAMPLE_RATE = 16000
_CHUNK_SECONDS = 600


def _transcribe_in_chunks(
    model: Any, media_path: Path, extra: dict[str, str]
) -> tuple[str, str | None, float]:
    from faster_whisper import decode_audio  # type: ignore[import-not-found]

    audio = decode_audio(str(media_path), sampling_rate=_SAMPLE_RATE)
    step = _CHUNK_SECONDS * _SAMPLE_RATE
    parts: list[str] = []
    language: str | None = None
    for start in range(0, len(audio), step):
        segments, info = model.transcribe(audio[start : start + step], vad_filter=True, **extra)
        parts.extend(segment.text.strip() for segment in segments)
        language = language or getattr(info, "language", None)
    return " ".join(p for p in parts if p), language, len(audio) / _SAMPLE_RATE


class LocalWhisperTranscriber:
    backend_name = "local"

    def __init__(self, settings: Settings):
        self.settings = settings

    def transcribe(self, media_path: Path, content_id: str) -> TranscriptResult:
        try:
            model = _get_whisper_model(
                self.settings.transcription.local_model_size,
                self.settings.transcription.device,
                self.settings.transcription.compute_type,
            )
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise TranscriptionError(
                "Local transcription backend selected but faster-whisper is not "
                "installed. Install the 'local-whisper' extra (uv sync --extra "
                "local-whisper) or set REEL_TRANSCRIPTION_BACKEND=openai."
            ) from exc

        try:
            # vad_filter skips silence/music-only stretches before decoding - without it,
            # Whisper is documented to hallucinate repetitive boilerplate text (e.g. "Thank
            # you for watching!") on silent segments, which is common in short-form video
            # (music-bed intros, on-screen-text-only clips) and would otherwise silently
            # pollute the transcript/enrichment/note with fabricated speech.
            hotwords = self.settings.transcription.hotwords
            extra = {"hotwords": hotwords} if hotwords else {}
            segments, info = model.transcribe(str(media_path), vad_filter=True, **extra)
            text = " ".join(segment.text.strip() for segment in segments)
            language = getattr(info, "language", None)
            duration = getattr(info, "duration", None)
        except MemoryError:
            # faster-whisper builds one spectrogram for the whole file (complex128), so
            # a ~1 h video needs over 1 GiB in a single allocation and can fail while
            # short reels never do (real failure 2026-11-22). Retry in fixed slices.
            log_context(
                logger,
                20,
                "whole-file transcription ran out of memory, retrying in chunks",
                content_id=content_id,
                chunk_seconds=_CHUNK_SECONDS,
            )
            try:
                text, language, duration = _transcribe_in_chunks(model, media_path, extra)
            except Exception as exc:  # noqa: BLE001 - same normalization as below
                raise TranscriptionError(
                    f"faster-whisper failed to transcribe {media_path.name!r} in chunks: {exc}"
                ) from exc
        except IndexError as exc:
            # faster-whisper (via PyAV) indexes container.streams.audio[0] internally;
            # a video with no audio track makes that tuple empty, surfacing as a bare
            # "tuple index out of range" with no indication of the actual cause. This is
            # not transient - retrying the same silent-video file will fail identically
            # every time, so unbounded retry (see worker.py) will loop on it forever.
            raise TranscriptionError(
                f"faster-whisper could not transcribe {media_path.name!r}: it appears to "
                "have no audio track (silent video). This item will keep failing on retry "
                "until removed from state.json - local-whisper cannot process video with "
                "no audio."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - normalize any other decode failure
            # PyAV/ctranslate2 can raise several other exception types on corrupted or
            # degenerate media (truncated downloads, zero-byte files) - all of them should
            # become a clear TranscriptionError, not an opaque backend-specific crash.
            raise TranscriptionError(
                f"faster-whisper failed to transcribe {media_path.name!r}: {exc}"
            ) from exc

        text = text.strip()
        if not text:
            # A legitimately wordless clip (music-only b-roll, on-screen-text-only) is a
            # valid outcome, not an error - but it looks identical to a silent decoding
            # problem from the note alone, so log it for anyone reviewing needs-attention
            # or a thin transcript later.
            log_context(
                logger,
                30,
                "transcription produced no text",
                content_id=content_id,
                media_path=str(media_path),
            )

        return TranscriptResult(
            content_id=content_id,
            text=text,
            language=language,
            backend=self.backend_name,
            duration_seconds=duration,
        )


class OpenAIWhisperTranscriber:
    backend_name = "openai"
    _endpoint = "https://api.openai.com/v1/audio/transcriptions"

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client

    def transcribe(self, media_path: Path, content_id: str) -> TranscriptResult:
        api_key = self.settings.openai_api_key
        if not api_key:
            raise TranscriptionError(
                "OpenAI transcription backend selected but OPENAI_API_KEY is not set."
            )

        client = self._client or httpx.Client(timeout=120.0)
        owns_client = self._client is None
        try:
            with media_path.open("rb") as fh:
                response = client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {api_key}"},
                    data={"model": self.settings.transcription.openai_model},
                    files={"file": (media_path.name, fh)},
                )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise TranscriptionError(f"OpenAI transcription request failed: {exc}") from exc
        finally:
            if owns_client:
                client.close()

        return TranscriptResult(
            content_id=content_id,
            text=payload.get("text", "").strip(),
            language=payload.get("language"),
            backend=self.backend_name,
            duration_seconds=payload.get("duration"),
        )


def get_transcriber(settings: Settings, client: httpx.Client | None = None) -> Transcriber:
    if settings.transcription.backend == "openai":
        return OpenAIWhisperTranscriber(settings, client=client)
    if settings.transcription.backend == "local":
        return LocalWhisperTranscriber(settings)
    raise ValueError(f"Unknown transcription backend: {settings.transcription.backend!r}")
