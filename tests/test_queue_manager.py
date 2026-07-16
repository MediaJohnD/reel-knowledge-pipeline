from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reel_pipeline.config import DownloadConfig, Settings, TextCaptureConfig
from reel_pipeline.models import ItemStatus, QueueSource
from reel_pipeline.queue_manager import QueueManager


def make_settings(tmp_path) -> Settings:
    return Settings(
        project_root=tmp_path,
        download=DownloadConfig(
            allowed_domains=["youtube.com", "vimeo.com"],
            blocked_domains=["instagram.com"],
        ),
    )


def make_settings_with_text_capture(tmp_path) -> Settings:
    return Settings(
        project_root=tmp_path,
        download=DownloadConfig(allowed_domains=["youtube.com"], blocked_domains=["instagram.com"]),
        text_capture=TextCaptureConfig(allowed_domains=["github.com"]),
    )


def test_sync_registers_new_pending_item_and_drains_queue_file(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://www.youtube.com/watch?v=abc123\n", encoding="utf-8")

    registered = qm.sync_queue_file_into_state()

    assert len(registered) == 1
    assert registered[0].status == ItemStatus.PENDING
    assert qm.queue_file.read_text(encoding="utf-8").strip() == ""

    state = qm.load_state()
    assert len(state) == 1


def test_sync_is_idempotent_across_restarts(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://www.youtube.com/watch?v=abc123\n", encoding="utf-8")

    first = qm.sync_queue_file_into_state()
    # Simulate re-adding the exact same URL (e.g. pasted twice) plus a restart with an
    # empty queue.txt (already drained) - state.json must be the thing that prevents dupes.
    qm.queue_file.write_text("https://www.youtube.com/watch?v=abc123\n", encoding="utf-8")
    second = qm.sync_queue_file_into_state()

    assert len(first) == 1
    assert len(second) == 0  # already known -> no new registration
    assert len(qm.load_state()) == 1


def test_blocked_domain_routes_to_needs_attention_not_state_pending(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://www.instagram.com/reel/xyz/\n", encoding="utf-8")

    registered = qm.sync_queue_file_into_state()

    assert len(registered) == 1
    assert registered[0].status == ItemStatus.BLOCKED
    needs_attention_content = qm.needs_attention_file.read_text(encoding="utf-8")
    assert "instagram" in needs_attention_content.lower()

    # Blocked items must never show up as actionable work for the worker.
    assert qm.get_actionable_items() == []


def test_needs_attention_is_not_duplicated_on_repeated_sync(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://www.instagram.com/reel/xyz/\n", encoding="utf-8")
    qm.sync_queue_file_into_state()

    qm.queue_file.write_text("https://www.instagram.com/reel/xyz/\n", encoding="utf-8")
    qm.sync_queue_file_into_state()

    content = qm.needs_attention_file.read_text(encoding="utf-8")
    lines = [line for line in content.splitlines() if line]
    assert len(lines) == 1


def test_webhook_add_url_dedups_against_queue_file_entry(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://www.youtube.com/watch?v=abc123\n", encoding="utf-8")
    qm.sync_queue_file_into_state()

    record = qm.add_url("https://youtube.com/watch?v=abc123", source=QueueSource.WEBHOOK)

    assert len(qm.load_state()) == 1
    assert record.source == QueueSource.QUEUE_FILE  # original registration wins


def test_get_actionable_items_excludes_done_and_blocked_but_includes_crash_interrupted(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    now = datetime.now(UTC)

    from reel_pipeline.models import StateRecord

    done = StateRecord(
        content_id="done1", url="https://youtube.com/1", normalized_url="https://youtube.com/1",
        source=QueueSource.QUEUE_FILE, status=ItemStatus.DONE, added_at=now, updated_at=now,
    )
    blocked = StateRecord(
        content_id="blocked1", url="https://instagram.com/1", normalized_url="https://instagram.com/1",
        source=QueueSource.QUEUE_FILE, status=ItemStatus.BLOCKED, added_at=now, updated_at=now,
    )
    crashed = StateRecord(
        content_id="crashed1", url="https://youtube.com/2", normalized_url="https://youtube.com/2",
        source=QueueSource.QUEUE_FILE, status=ItemStatus.TRANSCRIBING, added_at=now, updated_at=now,
    )
    qm.save_state({"done1": done, "blocked1": blocked, "crashed1": crashed})

    actionable_ids = {r.content_id for r in qm.get_actionable_items()}
    assert actionable_ids == {"crashed1"}


def test_prune_needs_attention_drops_lines_older_than_retention(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    old_ts = (datetime.now(UTC) - timedelta(days=45)).isoformat()
    recent_ts = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    qm.needs_attention_file.write_text(
        f"{old_ts}\thttps://youtube.com/old\tstale failure\n"
        f"{recent_ts}\thttps://youtube.com/recent\trecent failure\n",
        encoding="utf-8",
    )

    removed = qm.prune_needs_attention(retention_days=30)

    assert removed == 1
    remaining = qm.needs_attention_file.read_text(encoding="utf-8")
    assert "recent failure" in remaining
    assert "stale failure" not in remaining


def test_prune_needs_attention_disabled_when_retention_is_zero(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    old_ts = (datetime.now(UTC) - timedelta(days=999)).isoformat()
    qm.needs_attention_file.write_text(
        f"{old_ts}\thttps://youtube.com/old\tstale failure\n", encoding="utf-8"
    )

    removed = qm.prune_needs_attention(retention_days=0)

    assert removed == 0
    assert "stale failure" in qm.needs_attention_file.read_text(encoding="utf-8")


def test_prune_needs_attention_keeps_malformed_lines(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    qm.needs_attention_file.write_text("not-a-timestamp\tsome garbage line\n", encoding="utf-8")

    removed = qm.prune_needs_attention(retention_days=30)

    assert removed == 0
    assert "some garbage line" in qm.needs_attention_file.read_text(encoding="utf-8")


def test_github_url_registers_as_pending_with_text_content_kind(tmp_path):
    settings = make_settings_with_text_capture(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://github.com/owner/repo\n", encoding="utf-8")

    registered = qm.sync_queue_file_into_state()

    assert len(registered) == 1
    assert registered[0].status == ItemStatus.PENDING
    assert registered[0].content_kind == "text"


def test_youtube_url_registers_with_media_content_kind(tmp_path):
    settings = make_settings_with_text_capture(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://www.youtube.com/watch?v=abc123\n", encoding="utf-8")

    registered = qm.sync_queue_file_into_state()

    assert registered[0].content_kind == "media"


def test_unmatched_domain_still_rejected_with_text_capture_configured(tmp_path):
    settings = make_settings_with_text_capture(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://airtable.com/base/abc\n", encoding="utf-8")

    registered = qm.sync_queue_file_into_state()

    assert registered[0].status == ItemStatus.BLOCKED
