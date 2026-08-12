from __future__ import annotations

from datetime import UTC, datetime

from reel_pipeline.config import Settings
from reel_pipeline.models import (
    ContentItem,
    EnrichmentResult,
    ItemStatus,
    QueueSource,
    StateRecord,
    TranscriptResult,
)
from reel_pipeline.obsidian_writer import write_note
from reel_pipeline.queue_manager import QueueManager
from reel_pipeline.vault_organizer import organize_vault


def _write_done_record(settings, manager, content_id, title, content_kind="media"):
    item = ContentItem(
        content_id=content_id,
        source_url=f"https://example.com/{content_id}",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        transcript=TranscriptResult(content_id=content_id, text="text", backend="fake"),
        enrichment=EnrichmentResult(title=title, summary="summary"),
    )
    path = write_note(settings, item)
    record = StateRecord(
        content_id=content_id,
        url=item.source_url,
        normalized_url=item.source_url,
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.DONE,
        content_kind=content_kind,
        added_at=item.created_at,
        updated_at=item.created_at,
        note_path=str(path),
    )
    manager.update_record(record)
    return path


def test_note_already_in_a_manually_sorted_subfolder_is_left_there(tmp_path):
    # This is the whole point: the organizer must never move a note between
    # folders based on content_kind - the vault's real foldering is manual,
    # topic-based sorting, unrelated to media vs. text-capture.
    settings = Settings(project_root=tmp_path)
    manager = QueueManager(settings)
    path = _write_done_record(settings, manager, "vid1", "A Reel Title", content_kind="media")

    manually_sorted_dir = settings.vault_dir / "Projects"
    manually_sorted_dir.mkdir(parents=True, exist_ok=True)
    moved_path = manually_sorted_dir / path.name
    path.rename(moved_path)

    def mutate(state):
        state["vid1"].note_path = str(moved_path)

    manager.mutate_state(mutate)

    changes = organize_vault(settings)

    assert changes == []
    assert moved_path.is_file()


def test_filename_is_normalized_in_place_without_changing_folder(tmp_path):
    settings = Settings(project_root=tmp_path)
    manager = QueueManager(settings)
    path = _write_done_record(settings, manager, "doc1", "A Text Capture", content_kind="text")

    subfolder = settings.vault_dir / "Resources"
    subfolder.mkdir(parents=True, exist_ok=True)
    stale_name = subfolder / "Stale Old Name.md"
    path.rename(stale_name)

    def mutate(state):
        state["doc1"].note_path = str(stale_name)

    manager.mutate_state(mutate)

    changes = organize_vault(settings)

    assert len(changes) == 1
    normalized = subfolder / "a-text-capture.md"
    assert normalized.is_file()
    state = manager.load_state()
    assert state["doc1"].note_path == str(normalized)


def test_stale_note_path_with_mismatched_frontmatter_content_id_is_skipped_not_renamed(tmp_path):
    # Simulates a record whose note_path points at a file that actually
    # belongs to a different (possibly purged/orphaned) content_id - a real
    # state.json/frontmatter drift case, not something safe to rename around.
    settings = Settings(project_root=tmp_path)
    manager = QueueManager(settings)
    path = _write_done_record(settings, manager, "orphan-id", "Shared Title", content_kind="media")

    def corrupt(state):
        # Mutate the stored content_id in place (same dict key) so the record
        # disagrees with the file's own frontmatter, without going through
        # update_record()'s re-keying-by-content_id behavior.
        state["orphan-id"].content_id = "claimant-id"

    manager.mutate_state(corrupt)

    first_changes = organize_vault(settings)
    second_changes = organize_vault(settings)

    assert len(first_changes) == 1 and "skipped" in first_changes[0]
    assert first_changes == second_changes  # stable, not oscillating between names
    assert path.is_file()  # left untouched on disk


def test_rerun_is_a_no_op(tmp_path):
    settings = Settings(project_root=tmp_path)
    manager = QueueManager(settings)
    _write_done_record(settings, manager, "doc1", "A Text Capture", content_kind="text")

    organize_vault(settings)
    second_run_changes = organize_vault(settings)

    assert second_run_changes == []


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
