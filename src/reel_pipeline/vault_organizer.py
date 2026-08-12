"""Nightly vault housekeeping.

Repairs filename/location drift for notes already written by
obsidian_writer.py: re-derives each DONE record's canonical slug filename
from its frontmatter title, and files it under the vault-root subfolder that
matches its content_kind - media (reels) stay at the vault root, text-capture
notes move into `Resources/`. obsidian_writer.write_note() already gets new
notes right on first write; this only fixes drift (legacy filenames, notes
moved out-of-band) and is safe to run repeatedly - a note already in its
canonical spot is left untouched.
"""

from __future__ import annotations

from pathlib import Path

from reel_pipeline.config import Settings
from reel_pipeline.models import ItemStatus, StateRecord
from reel_pipeline.obsidian_writer import note_filename, read_frontmatter
from reel_pipeline.queue_manager import QueueManager

TEXT_CAPTURE_SUBFOLDER = "Resources"


def _target_dir(settings: Settings, record: StateRecord) -> Path:
    if record.content_kind == "text":
        return settings.vault_dir / TEXT_CAPTURE_SUBFOLDER
    return settings.vault_dir


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

            target_dir = _target_dir(settings, record)
            target_dir.mkdir(parents=True, exist_ok=True)
            target_name = note_filename(target_dir, record.content_id, title)
            target_path = target_dir / target_name

            if target_path == current:
                continue

            current.rename(target_path)
            changes.append(f"moved {current} -> {target_path}")
            record.note_path = str(target_path)

    manager.mutate_state(mutate)
    return changes
