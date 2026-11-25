from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

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


def _write_done_record(
    settings, manager, content_id, title, content_kind: Literal["media", "text"] = "media"
):
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


def _add_merged_alias(manager, alias_id, kept_path):
    # A duplicate capture merged into another record's note: its own record
    # stays DONE (so the URL is never re-ingested) but points at the kept note,
    # whose frontmatter carries the owner's content_id.
    record = StateRecord(
        content_id=alias_id,
        url=f"https://example.com/{alias_id}",
        normalized_url=f"https://example.com/{alias_id}",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.DONE,
        content_kind="text",
        added_at=datetime(2026, 1, 2, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        note_path=str(kept_path),
    )
    manager.update_record(record)


def test_merged_duplicate_record_pointing_at_owners_note_is_not_reported(tmp_path):
    settings = Settings(project_root=tmp_path)
    manager = QueueManager(settings)
    kept = _write_done_record(settings, manager, "owner-id", "Kept Note", content_kind="text")
    _add_merged_alias(manager, "alias-id", kept)

    assert organize_vault(settings) == []
    assert kept.is_file()
    assert manager.load_state()["alias-id"].note_path == str(kept)


def test_merged_duplicate_record_follows_owner_rename(tmp_path):
    settings = Settings(project_root=tmp_path)
    manager = QueueManager(settings)
    path = _write_done_record(settings, manager, "owner-id", "Kept Note", content_kind="text")
    stale = path.with_name("Old Name.md")
    path.rename(stale)

    def point_owner_at_stale(state):
        state["owner-id"].note_path = str(stale)

    manager.mutate_state(point_owner_at_stale)
    _add_merged_alias(manager, "alias-id", stale)

    changes = organize_vault(settings)

    normalized = stale.with_name("kept-note.md")
    assert changes == [f"moved {stale} -> {normalized}"]
    state = manager.load_state()
    assert state["owner-id"].note_path == str(normalized)
    assert state["alias-id"].note_path == str(normalized)
    assert organize_vault(settings) == []


def test_all_merged_aliases_follow_owner_rename_regardless_of_order(tmp_path):
    # The owner is processed first here (dict order), so the aliases must be
    # matched against the path it had *before* the rename.
    settings = Settings(project_root=tmp_path)
    manager = QueueManager(settings)
    path = _write_done_record(settings, manager, "a-owner", "Kept Note", content_kind="text")
    stale = path.with_name("Old Name.md")
    path.rename(stale)

    def point_owner_at_stale(state):
        state["a-owner"].note_path = str(stale)

    manager.mutate_state(point_owner_at_stale)
    _add_merged_alias(manager, "y-alias", stale)
    _add_merged_alias(manager, "z-alias", stale)

    changes = organize_vault(settings)

    normalized = stale.with_name("kept-note.md")
    assert changes == [f"moved {stale} -> {normalized}"]
    state = manager.load_state()
    assert {state[k].note_path for k in ("a-owner", "y-alias", "z-alias")} == {str(normalized)}
    assert organize_vault(settings) == []


def test_non_scalar_frontmatter_content_id_is_reported_not_crashed(tmp_path):
    settings = Settings(project_root=tmp_path)
    manager = QueueManager(settings)
    path = _write_done_record(settings, manager, "doc1", "Some Note", content_kind="text")
    path.write_text("---\ncontent_id: [doc1]\ntitle: Some Note\n---\nbody\n", encoding="utf-8")

    changes = organize_vault(settings)

    assert len(changes) == 1 and "skipped" in changes[0]
    assert path.is_file()


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


def test_auto_hub_links_unhubbed_notes(tmp_path, monkeypatch):
    import json

    from reel_pipeline import llm_client
    from reel_pipeline.vault_organizer import auto_hub

    settings = Settings(project_root=tmp_path)
    manager = QueueManager(settings)
    a = _write_done_record(settings, manager, "aaa", "Travel Hacks")
    b = _write_done_record(settings, manager, "bbb", "Junk")
    hubs = settings.vault_dir / "Hubs"
    hubs.mkdir()
    (hubs / "travel-hub.md").write_text(
        "# T\n\n## Flights\n- [[old]]\n\n## See Also\n- [[x]]\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        llm_client,
        "call_llm",
        lambda *a_, **k: json.dumps({a.stem: "travel-hub/Flights", b.stem: "travel-hub/See Also"}),
    )

    changes = auto_hub(settings)

    hub = (hubs / "travel-hub.md").read_text(encoding="utf-8")
    assert f"- [[old]]\n- [[{a.stem}]]\n\n## See Also" in hub
    assert b.stem not in hub and any(c.startswith(f"unhubbed {b.stem}") for c in changes)
    assert auto_hub(settings) == [
        f'unhubbed {b.stem}: model picked "travel-hub/See Also"'
    ]  # idempotent
