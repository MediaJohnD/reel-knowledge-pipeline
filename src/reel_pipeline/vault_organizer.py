"""Nightly vault housekeeping.

Normalizes each DONE record's note filename to its frontmatter-title slug,
in whatever folder it currently lives in. Does NOT move notes between
folders - an earlier version filed notes into `Resources/` vs. vault root
by content_kind (media vs. text-capture), but that fights this vault's real
organization: notes get manually sorted into Areas/Projects/Resources/etc.
by topic, independent of content_kind, and the content_kind-based move
undid that sorting the one time it ran against healed note_path bookkeeping
(see the 2026-08-12 revert). Filename-only normalization has no such
conflict - it's safe regardless of which folder a note lives in.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from reel_pipeline.config import Settings
from reel_pipeline.models import ItemStatus, StateRecord
from reel_pipeline.obsidian_writer import note_filename, read_frontmatter
from reel_pipeline.queue_manager import QueueManager


def organize_vault(settings: Settings) -> list[str]:
    """Moves/renames each DONE record's note to its canonical path.

    Returns one human-readable "old -> new" line per note actually moved.
    """
    manager = QueueManager(settings)
    changes: list[str] = []

    def mutate(state: dict[str, StateRecord]) -> None:
        for record in state.values():
            if record.status != ItemStatus.DONE or not record.note_path:
                continue
            current = Path(record.note_path)
            if not current.is_file():
                continue  # already gone / moved out-of-band; nothing to repair

            frontmatter = read_frontmatter(current)
            file_content_id = frontmatter.get("content_id") if frontmatter else None
            if file_content_id is not None and file_content_id != record.content_id:
                # state.json's note_path for this record doesn't actually belong to
                # it - the file's own frontmatter says otherwise (stale note_path
                # from a past content_id collision/reprocessing). Renaming here
                # would misattribute someone else's note and, since the collision
                # check re-derives differently depending on which name currently
                # holds the file, can oscillate forever on repeat runs. Leave it
                # for manual review instead.
                changes.append(
                    f"skipped {current}: frontmatter content_id {file_content_id!r} "
                    f"!= state record {record.content_id!r} (stale note_path)"
                )
                continue

            title = frontmatter.get("title") if frontmatter else None
            if not title:
                title = current.stem

            target_dir = current.parent
            target_name = note_filename(target_dir, record.content_id, title)
            target_path = target_dir / target_name

            if target_path == current:
                continue

            current.rename(target_path)
            changes.append(f"moved {current} -> {target_path}")
            record.note_path = str(target_path)

    manager.mutate_state(mutate)
    return changes


def find_duplicate_notes(settings: Settings) -> list[str]:
    """Scans every note under vault_dir for two files sharing a content_id or
    source_url - the pipeline dedups on ingestion (see validators.normalize_url),
    so any pair found here means a note was created, edited, or moved outside
    that path. Read-only: reports for manual review rather than deleting
    anything, since picking which duplicate is "the real one" isn't safe to
    automate.
    """
    by_content_id: dict[str, list[Path]] = defaultdict(list)
    by_source_url: dict[str, list[Path]] = defaultdict(list)

    for path in Path(settings.vault_dir).rglob("*.md"):
        frontmatter = read_frontmatter(path)
        if not frontmatter:
            continue
        content_id = frontmatter.get("content_id")
        if content_id:
            by_content_id[content_id].append(path)
        source_url = frontmatter.get("source_url")
        if source_url:
            by_source_url[source_url].append(path)

    findings: list[str] = []
    for content_id, paths in by_content_id.items():
        if len(paths) > 1:
            joined = ", ".join(str(p) for p in paths)
            findings.append(f"duplicate content_id {content_id!r}: {joined}")
    for source_url, paths in by_source_url.items():
        if len(paths) > 1:
            joined = ", ".join(str(p) for p in paths)
            findings.append(f"duplicate source_url {source_url!r}: {joined}")
    return findings
