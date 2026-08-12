"""Writes Obsidian-compatible markdown notes from a fully-enriched ContentItem.

Filenames are the title slug alone (`<title-slug>.md`), not prefixed with
content_id - a hex content_id in every filename showed up as low-signal noise
in Obsidian's file explorer and graph view (see the 2026-07-19 vault title
cleanup). Idempotency on re-processing does NOT depend on the filename: it's
handled by worker.py's `_cleanup_stale_note`, which tracks each item's
previous note_path in state.json and deletes it when a re-run produces a
different path. The content_id is only consulted here to disambiguate a
genuine slug collision between two *different* pieces of content that happen
to produce the same title slug - re-processing the *same* content_id is
expected to reuse its own existing file rather than being treated as a
collision.
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


def read_frontmatter(path: Path) -> dict | None:
    """Parses a note's YAML frontmatter, or None if the file doesn't exist or
    has no parseable frontmatter block.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    frontmatter = yaml.safe_load(text[4:end])
    return frontmatter if isinstance(frontmatter, dict) else None


def _existing_content_id(path: Path) -> str | None:
    """Reads just the content_id out of a note's frontmatter, or None if the
    file doesn't exist or has no parseable frontmatter.
    """
    frontmatter = read_frontmatter(path)
    return frontmatter.get("content_id") if frontmatter else None


def note_filename(vault_dir: Path, content_id: str, title: str) -> str:
    """Picks `<slug>.md`, or `<slug>-2.md`, `<slug>-3.md`, ... on collision.

    A "collision" is an existing file with the same slug but a *different*
    content_id in its frontmatter. If the existing file belongs to this same
    content_id (the normal re-processing case), its filename is reused as-is.
    """
    slug = slugify(title)
    candidate = f"{slug}.md"
    suffix = 2
    while True:
        existing = _existing_content_id(vault_dir / candidate)
        if existing is None or existing == content_id:
            return candidate
        candidate = f"{slug}-{suffix}.md"
        suffix += 1


def note_path(settings: Settings, content_id: str, title: str) -> Path:
    return settings.vault_dir / note_filename(settings.vault_dir, content_id, title)


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
