"""Writes Obsidian-compatible markdown notes from a fully-enriched ContentItem.

Filenames are deterministic (`<content_id>-<title-slug>.md`), so re-running the
pipeline for the same content_id always overwrites the same file instead of
accumulating duplicates - this is what makes note writing idempotent.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from reel_pipeline.config import Settings
from reel_pipeline.models import ContentItem

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_length: int = 60) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "untitled"


def note_filename(content_id: str, title: str) -> str:
    return f"{content_id}-{slugify(title)}.md"


def note_path(settings: Settings, content_id: str, title: str) -> Path:
    return settings.vault_dir / note_filename(content_id, title)


def _render_frontmatter(item: ContentItem) -> str:
    frontmatter = {
        "title": item.enrichment.title,
        "source_url": item.source_url,
        "content_id": item.content_id,
        "created_at": item.created_at.isoformat(),
        "tags": item.enrichment.tags,
        "tools_mentioned": item.enrichment.tools_mentioned,
        "high_signal": item.enrichment.high_signal,
    }
    dumped = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    return f"---\n{dumped}---\n"


def _render_body(item: ContentItem) -> str:
    enrichment = item.enrichment
    lines: list[str] = [f"# {enrichment.title}", ""]

    lines.append("## Summary")
    lines.append(enrichment.summary)
    lines.append("")

    lines.append("## Key Takeaways")
    if enrichment.key_takeaways:
        lines.extend(f"- {takeaway}" for takeaway in enrichment.key_takeaways)
    else:
        lines.append("- (none extracted)")
    lines.append("")

    lines.append("## Tools Mentioned")
    if enrichment.tools_mentioned:
        lines.extend(f"- {tool}" for tool in enrichment.tools_mentioned)
    else:
        lines.append("- (none mentioned)")
    lines.append("")

    if enrichment.high_signal and enrichment.skill_candidate_reason:
        lines.append("## Skill Candidate")
        lines.append(enrichment.skill_candidate_reason)
        lines.append("")

    lines.append("## Transcript")
    lines.append("")
    lines.append(item.transcript.text)
    lines.append("")

    return "\n".join(lines)


def write_note(settings: Settings, item: ContentItem) -> Path:
    settings.vault_dir.mkdir(parents=True, exist_ok=True)
    path = note_path(settings, item.content_id, item.enrichment.title)
    content = _render_frontmatter(item) + "\n" + _render_body(item)
    path.write_text(content, encoding="utf-8")
    return path
