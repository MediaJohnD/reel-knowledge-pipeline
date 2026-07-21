"""Optional skill artifact writer.

Only produces a SKILL.md when the enrichment result marked the content
high_signal AND supplied a skill_candidate_reason (i.e. the transcript
contains reusable workflow knowledge, not just an opinion or one-off
anecdote). Uses config/prompts/create_skill.md via an LLM (Claude or a local
Ollama model, per settings.llm.provider).

Generated skills are written under settings.skills_dir (default
data/generated_skills/), deliberately separate from .claude/skills/, which
holds this repository's own authoring skills.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from reel_pipeline.config import Settings
from reel_pipeline.enricher import render_and_split
from reel_pipeline.llm_client import LlmCallError, call_llm
from reel_pipeline.models import ContentItem
from reel_pipeline.obsidian_writer import slugify


class SkillGenerationError(RuntimeError):
    """Raised when skill artifact generation fails."""


def should_generate_skill(item: ContentItem, settings: Settings) -> bool:
    return (
        settings.skill_writer.enabled
        and item.enrichment.high_signal
        and bool(item.enrichment.skill_candidate_reason)
    )


def skill_path(settings: Settings, title: str) -> Path:
    return settings.skills_dir / slugify(title) / "SKILL.md"


class SkillWriter:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client
        self._prompt_template = settings.create_skill_prompt.read_text(encoding="utf-8")

    def generate(self, item: ContentItem) -> Path | None:
        if not should_generate_skill(item, self.settings):
            return None

        enrichment = item.enrichment
        static_prefix, prompt = render_and_split(
            self._prompt_template,
            title=enrichment.title,
            summary=enrichment.summary,
            skill_candidate_reason=enrichment.skill_candidate_reason or "",
            key_takeaways="\n".join(f"- {t}" for t in enrichment.key_takeaways) or "- (none)",
            transcript=item.transcript.text,
        )
        try:
            skill_markdown = call_llm(
                self.settings,
                prompt,
                model=self.settings.skill_writer.model,
                max_tokens=self.settings.skill_writer.max_tokens,
                static_prefix=static_prefix,
                client=self._client,
            )
        except LlmCallError as exc:
            raise SkillGenerationError(str(exc)) from exc

        path = skill_path(self.settings, enrichment.title)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(skill_markdown.strip() + "\n", encoding="utf-8")
        return path
