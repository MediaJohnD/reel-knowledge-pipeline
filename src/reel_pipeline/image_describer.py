"""Turns downloaded image(s) - e.g. an Instagram photo post or carousel with no
video - into a text description via a vision-capable LLM (Claude, or a local
Ollama vision model, per settings.llm.provider), using the prompt template at
config/prompts/describe_image_post.md.

Produces the same TranscriptResult shape transcriber.py does, so downstream
enrichment and note-writing are identical regardless of whether the source
was a video or a photo post - the worker just picks this or the Transcriber
based on DownloadResult.media_type.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

import httpx

from reel_pipeline.config import Settings
from reel_pipeline.enricher import render_and_split
from reel_pipeline.llm_client import LlmCallError, describe_images
from reel_pipeline.models import TranscriptResult


class ImageDescriber(Protocol):
    def describe(self, media_paths: list[Path], content_id: str) -> TranscriptResult: ...


class ImageDescriptionError(RuntimeError):
    """Raised when image description fails."""


# A model that can't actually see the attached images answers with an apology
# ("I cannot directly view images", "since I cannot view the actual images...")
# instead of a description. That text used to flow on to the enricher, which
# summarised and tagged the apology and wrote it out as a normal note - two
# such notes reached the vault two weeks apart. Anchored on the model declining
# to *perceive* the images, not on the word "image" alone, so a genuine
# description that merely talks about images still passes through: silently
# dropping good captures would be worse than the bug.
_REFUSAL_RE = re.compile(
    r"\b(?:cannot|can ?not|can't|unable to|not able to|don't have the ability to)\s+"
    r"(?:\w+\s+){0,2}"  # "directly", "actually", ...
    r"(?:view|see|access|open|display|process|read|analyze|interpret|identify|look at)\s+"
    r"(?:\w+\s+){0,3}"  # "the actual", "any of the", ...
    r"(?:image|photo|picture|screenshot)s?\b",
    re.IGNORECASE,
)
# A second, positively-phrased refusal shape: the model agrees to help but asks
# to be *sent* the images, rather than describing the ones already attached
# ("Certainly, please provide the images so I can analyze them for you") - it
# never says "cannot", so _REFUSAL_RE alone missed it (found 2026-08-12, one
# such note reached the vault before this was added). "please" is required
# (not just the verb+noun pair) - a genuine description narrating someone
# sharing/providing something ("offers to share the photos from the trip")
# matches the verb+noun proximity alone but would never plausibly say
# "please" with no addressee, so requiring it keeps the false-positive rate
# down per this module's founding tradeoff: under-catching beats dropping a
# good capture.
_REQUEST_FOR_IMAGES_RE = re.compile(
    r"\bplease\s+(?:\w+\s+){0,2}"
    r"(?:provide|share|send|attach|upload|paste)\s+"
    r"(?:\w+\s+){0,3}"  # "the", "each of the", "me the actual", ...
    # The object can be the images themselves, or - found live 2026-08-21 -
    # what the model wants *about* them ("please provide a description of the
    # images", "please paste a transcription of the screenshots"). Matching the
    # description noun directly keeps the wildcard gap above at three words
    # rather than widening it to six to reach the trailing "images".
    r"(?:image|photo|picture|screenshot|transcription|transcript|description)s?\b",
    re.IGNORECASE,
)
# Vision models routinely emit typographic quotes ("can’t", "don’t") rather
# than ASCII ones - normalized before matching so _REFUSAL_RE's ASCII-only
# apostrophes don't silently miss them (found by review, 2026-08-10).
_CURLY_QUOTES = str.maketrans({"’": "'", "‘": "'"})


# Formats the vision path cannot rely on the model decoding. Ollama with
# mistral-small3.1 does not decode WebP and - measured 2026-08-21 - does not
# error either: it returns HTTP 200 with the image dropped and answers as if
# nothing were attached (2.7s, against 70.3s for the same picture as JPEG), so
# the failure arrives looking like a refusal rather than a fault. Instagram
# serves WebP routinely. Both image-selection sites accept ".webp"
# (downloader._IMAGE_SUFFIXES and worker._cached_download_result's rescan), so
# the re-encode belongs here, where every source converges before the call.
_NEEDS_REENCODE_SUFFIXES = (".webp",)


def _reencode_for_vision(source: Path, workdir: Path, index: int) -> Path:
    """Re-encodes one image into PNG so the vision model can actually decode it.

    ffmpeg rather than Pillow: Pillow is not a declared dependency, while ffmpeg
    already is (downloader._extract_first_frame). PNG rather than JPEG because
    the source is already lossy and these carousels are frequently screenshots
    of small text - a second lossy pass is exactly what would cost the model
    the characters it is being asked to transcribe.

    Output goes to a caller-owned temp dir, never back into data/tmp/: the
    resume path (worker._cached_download_result) rescans that directory for
    images, so a converted copy left beside its original would come back as a
    second, duplicate image on the next attempt.
    """
    target = workdir / f"{index:03d}-{source.stem}.png"
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell, path is our own download
            ["ffmpeg", "-y", "-i", str(source), str(target)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ImageDescriptionError(
            f"ffmpeg failed to convert {source.suffix} image {source}: {exc}"
        ) from exc
    if result.returncode != 0 or not target.exists():
        raise ImageDescriptionError(
            f"ffmpeg failed to convert {source.suffix} image {source}: {result.stderr.strip()}"
        )
    return target


class LlmImageDescriber:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client
        self._prompt_template = settings.describe_image_post_prompt.read_text(encoding="utf-8")

    def describe(self, media_paths: list[Path], content_id: str) -> TranscriptResult:
        provider = self.settings.image_description.provider or self.settings.llm.provider
        with tempfile.TemporaryDirectory(prefix="reel-vision-") as workdir:
            prepared = [
                _reencode_for_vision(path, Path(workdir), i)
                if path.suffix.lower() in _NEEDS_REENCODE_SUFFIXES
                else path
                for i, path in enumerate(media_paths)
            ]
            batches = self._batch(prepared)
            texts = [self._describe_batch(batch, provider) for batch in batches]

        if len(batches) == 1:
            text = texts[0]
        else:
            # Labelled like worker._transcribe_media_paths' "[Clip i/n]" so the
            # enricher can see where one batch's description ends and the next
            # begins. Each batch is described in isolation, so the model cannot
            # narrate the arc across a split carousel the way the prompt asks
            # for - an accepted loss, since the alternative was a permanent
            # HTTP 400 and no description at all.
            total = len(media_paths)
            labelled, start = [], 0
            for batch, part in zip(batches, texts, strict=True):
                labelled.append(f"[Images {start + 1}-{start + len(batch)} of {total}] {part}")
                start += len(batch)
            text = "\n\n".join(labelled)

        return TranscriptResult(
            content_id=content_id,
            text=text,
            language=None,
            backend=f"vision:{provider}:{self.settings.image_description.model}",
            duration_seconds=None,
        )

    def _batch(self, media_paths: list[Path]) -> list[list[Path]]:
        """Splits a carousel into per-request groups. Always yields at least one
        batch, so an empty media_paths still reaches describe_images() and
        raises there rather than silently producing an empty description.
        """
        size = max(1, self.settings.image_description.max_images_per_call)
        return [media_paths[i : i + size] for i in range(0, len(media_paths), size)] or [
            media_paths
        ]

    def _describe_batch(self, media_paths: list[Path], provider: str) -> str:
        static_prefix, prompt = render_and_split(
            self._prompt_template, image_count=str(len(media_paths))
        )
        try:
            text = describe_images(
                self.settings,
                prompt,
                media_paths,
                model=self.settings.image_description.model,
                max_tokens=self.settings.image_description.max_tokens,
                static_prefix=static_prefix,
                provider=provider,
                client=self._client,
            )
        except LlmCallError as exc:
            raise ImageDescriptionError(str(exc)) from exc

        text = text.strip()
        normalized = text.translate(_CURLY_QUOTES)
        # Per batch, not on the joined text: a refusal in one batch alongside a
        # genuine description in another would otherwise reach the vault.
        if _REFUSAL_RE.search(normalized) or _REQUEST_FOR_IMAGES_RE.search(normalized):
            raise ImageDescriptionError(
                "the vision model refused to describe the image(s) rather than "
                f"returning a description: {text[:200]!r}"
            )
        return text


def get_image_describer(settings: Settings, client: httpx.Client | None = None) -> ImageDescriber:
    return LlmImageDescriber(settings, client=client)
