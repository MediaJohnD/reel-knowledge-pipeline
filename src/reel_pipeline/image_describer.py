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
    r"(?:image|photo|picture|screenshot)s?\b",
    re.IGNORECASE,
)
# Vision models routinely emit typographic quotes ("can’t", "don’t") rather
# than ASCII ones - normalized before matching so _REFUSAL_RE's ASCII-only
# apostrophes don't silently miss them (found by review, 2026-08-10).
_CURLY_QUOTES = str.maketrans({"’": "'", "‘": "'"})


class LlmImageDescriber:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client
        self._prompt_template = settings.describe_image_post_prompt.read_text(encoding="utf-8")

    def describe(self, media_paths: list[Path], content_id: str) -> TranscriptResult:
        static_prefix, prompt = render_and_split(
            self._prompt_template, image_count=str(len(media_paths))
        )
        provider = self.settings.image_description.provider or self.settings.llm.provider
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
        if _REFUSAL_RE.search(normalized) or _REQUEST_FOR_IMAGES_RE.search(normalized):
            raise ImageDescriptionError(
                "the vision model refused to describe the image(s) rather than "
                f"returning a description: {text[:200]!r}"
            )

        return TranscriptResult(
            content_id=content_id,
            text=text,
            language=None,
            backend=f"vision:{provider}:{self.settings.image_description.model}",
            duration_seconds=None,
        )


def get_image_describer(settings: Settings, client: httpx.Client | None = None) -> ImageDescriber:
    return LlmImageDescriber(settings, client=client)
