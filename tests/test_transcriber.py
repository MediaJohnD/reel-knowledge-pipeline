from __future__ import annotations

import sys
import types

import pytest

from reel_pipeline.config import Settings
from reel_pipeline.transcriber import _MODEL_CACHE, LocalWhisperTranscriber, TranscriptionError


@pytest.fixture(autouse=True)
def _clear_model_cache():
    # _MODEL_CACHE is process-lifetime state (intentionally, in production - see
    # transcriber.py) but must not leak a fake model instance from one test into the
    # next, since they all resolve to the same (model_size, device, compute_type) key.
    _MODEL_CACHE.clear()
    yield
    _MODEL_CACHE.clear()


def _install_fake_faster_whisper(monkeypatch, transcribe_impl):
    fake_module = types.ModuleType("faster_whisper")
    init_calls = []

    class FakeWhisperModel:
        def __init__(self, *args, **kwargs):
            init_calls.append((args, kwargs))

        def transcribe(self, media_path, **kwargs):
            return transcribe_impl(media_path, **kwargs)

    fake_module.WhisperModel = FakeWhisperModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)
    return init_calls


def test_silent_video_raises_clear_transcription_error_not_raw_indexerror(tmp_path, monkeypatch):
    def transcribe_impl(media_path, **kwargs):
        # Reproduces faster-whisper/PyAV's real failure mode: container.streams.audio[0]
        # on a video with no audio track raises exactly this IndexError.
        raise IndexError("tuple index out of range")

    _install_fake_faster_whisper(monkeypatch, transcribe_impl)
    settings = Settings(project_root=tmp_path)
    transcriber = LocalWhisperTranscriber(settings)
    media_path = tmp_path / "silent.mp4"
    media_path.write_bytes(b"fake")

    with pytest.raises(TranscriptionError, match="no audio track"):
        transcriber.transcribe(media_path, "cid1")


def test_corrupted_file_raises_clear_transcription_error_not_raw_exception(tmp_path, monkeypatch):
    def transcribe_impl(media_path, **kwargs):
        raise ValueError("invalid data found when processing input")

    _install_fake_faster_whisper(monkeypatch, transcribe_impl)
    settings = Settings(project_root=tmp_path)
    transcriber = LocalWhisperTranscriber(settings)
    media_path = tmp_path / "corrupted.mp4"
    media_path.write_bytes(b"fake")

    with pytest.raises(TranscriptionError, match="failed to transcribe"):
        transcriber.transcribe(media_path, "cid1")


def test_normal_video_still_transcribes_successfully(tmp_path, monkeypatch):
    class FakeSegment:
        def __init__(self, text):
            self.text = text

    class FakeInfo:
        language = "en"
        duration = 12.5

    def transcribe_impl(media_path, **kwargs):
        assert kwargs.get("vad_filter") is True
        return [FakeSegment(" hello "), FakeSegment("world ")], FakeInfo()

    _install_fake_faster_whisper(monkeypatch, transcribe_impl)
    settings = Settings(project_root=tmp_path)
    transcriber = LocalWhisperTranscriber(settings)
    media_path = tmp_path / "normal.mp4"
    media_path.write_bytes(b"fake")

    result = transcriber.transcribe(media_path, "cid2")

    assert result.text == "hello world"
    assert result.language == "en"
    assert result.duration_seconds == 12.5


def test_empty_transcript_is_returned_not_raised(tmp_path, monkeypatch, caplog):
    class FakeInfo:
        language = "en"
        duration = 3.0

    def transcribe_impl(media_path, **kwargs):
        return [], FakeInfo()  # e.g. vad_filter dropped every segment as silence

    _install_fake_faster_whisper(monkeypatch, transcribe_impl)
    settings = Settings(project_root=tmp_path)
    transcriber = LocalWhisperTranscriber(settings)
    media_path = tmp_path / "wordless.mp4"
    media_path.write_bytes(b"fake")

    result = transcriber.transcribe(media_path, "cid3")

    assert result.text == ""


def test_model_is_cached_across_calls_not_reloaded_every_time(tmp_path, monkeypatch):
    class FakeInfo:
        language = "en"
        duration = 1.0

    def transcribe_impl(media_path, **kwargs):
        return [], FakeInfo()

    init_calls = _install_fake_faster_whisper(monkeypatch, transcribe_impl)
    settings = Settings(project_root=tmp_path)
    transcriber = LocalWhisperTranscriber(settings)
    media_path = tmp_path / "a.mp4"
    media_path.write_bytes(b"fake")

    transcriber.transcribe(media_path, "cid4")
    transcriber.transcribe(media_path, "cid5")
    LocalWhisperTranscriber(settings).transcribe(media_path, "cid6")  # fresh instance too

    assert len(init_calls) == 1


def test_model_resolves_device_and_compute_type_from_settings(tmp_path, monkeypatch):
    class FakeInfo:
        language = "en"
        duration = 1.0

    def transcribe_impl(media_path, **kwargs):
        return [], FakeInfo()

    init_calls = _install_fake_faster_whisper(monkeypatch, transcribe_impl)
    settings = Settings(project_root=tmp_path)
    settings.transcription.device = "cpu"
    settings.transcription.compute_type = "int8"
    media_path = tmp_path / "a.mp4"
    media_path.write_bytes(b"fake")

    LocalWhisperTranscriber(settings).transcribe(media_path, "cid7")

    (args, kwargs) = init_calls[0]
    assert kwargs.get("device") == "cpu"
    assert kwargs.get("compute_type") == "int8"
