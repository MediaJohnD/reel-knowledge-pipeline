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

from pathlib import Path
from typing import Protocol

from reel_pipeline.config import Settings
from reel_pipeline.enricher import render_template
from reel_pipeline.llm_client import LlmCallError, describe_images
from reel_pipeline.models import TranscriptResult


class ImageDescriber(Protocol):
    def describe(self, media_paths: list[Path], content_id: str) -> TranscriptResult: ...


class ImageDescriptionError(RuntimeError):
    """Raised when image description fails."""


class LlmImageDescriber:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._prompt_template = settings.describe_image_post_prompt.read_text(encoding="utf-8")

    def describe(self, media_paths: list[Path], content_id: str) -> TranscriptResult:
        prompt = render_template(self._prompt_template, image_count=str(len(media_paths)))
        try:
            text = describe_images(
                self.settings,
                prompt,
                media_paths,
                model=self.settings.image_description.model,
                max_tokens=self.settings.image_description.max_tokens,
            )
        except LlmCallError as exc:
            raise ImageDescriptionError(str(exc)) from exc

        return TranscriptResult(
            content_id=content_id,
            text=text.strip(),
            language=None,
            backend=f"vision:{self.settings.llm.provider}:{self.settings.image_description.model}",
            duration_seconds=None,
        )


def get_image_describer(settings: Settings) -> ImageDescriber:
    return LlmImageDescriber(settings)
