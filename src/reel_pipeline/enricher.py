"""Transcript enrichment: turns raw transcript text into the structured
EnrichmentResult contract (title, summary, tags, tools_mentioned,
key_takeaways, high_signal, skill_candidate_reason) via an LLM (Claude or a
local Ollama model, per settings.llm.provider), using the prompt template at
config/prompts/enrich_transcript.md.
"""

from __future__ import annotations

import json
import re

import httpx

from reel_pipeline.config import Settings
from reel_pipeline.llm_client import LlmCallError, call_llm
from reel_pipeline.models import EnrichmentResult, TranscriptResult

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class EnrichmentError(RuntimeError):
    """Raised when the enrichment call fails or returns unparsable output."""


def render_template(template: str, **values: str) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def _extract_json(raw_text: str) -> dict:
    candidate = _FENCE_RE.sub("", raw_text.strip()).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        # Fall back to the outermost {...} span in case the model added stray prose.
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise EnrichmentError(f"Could not parse JSON from model output: {raw_text!r}") from exc


class Enricher:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client
        self._transcript_prompt_template = settings.enrich_transcript_prompt.read_text(
            encoding="utf-8"
        )
        self._text_capture_prompt_template = settings.enrich_text_capture_prompt.read_text(
            encoding="utf-8"
        )

    def enrich(self, transcript: TranscriptResult, source_url: str) -> EnrichmentResult:
        template = (
            self._text_capture_prompt_template
            if transcript.content_kind == "text"
            else self._transcript_prompt_template
        )
        prompt = render_template(
            template,
            source_url=source_url,
            transcript=transcript.text,
        )
        try:
            raw_text = call_llm(
                self.settings,
                prompt,
                model=self.settings.enrichment.model,
                max_tokens=self.settings.enrichment.max_tokens,
                client=self._client,
            )
        except LlmCallError as exc:
            raise EnrichmentError(str(exc)) from exc
        data = _extract_json(raw_text)
        return EnrichmentResult(**data)
