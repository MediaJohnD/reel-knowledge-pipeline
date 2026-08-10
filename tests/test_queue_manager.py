from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta

from reel_pipeline.config import DownloadConfig, Settings, TextCaptureConfig
from reel_pipeline.models import ItemStatus, QueueSource
from reel_pipeline.queue_manager import QueueManager


def _install_fake_yt_dlp(monkeypatch, *, raises: bool):
    """Same pattern as tests/test_validators.py's helper - fakes yt_dlp for
    _classify_drive_url_kind()'s metadata-only probe.
    """

    class FakeYoutubeDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def extract_info(self, url, download=True):
            if raises:
                raise RuntimeError("[GoogleDrive] Unable to download JSON metadata: 400")
            return {"ext": "mp4"}

    fake_module = types.ModuleType("yt_dlp")
    fake_module.YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_module)


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
        text_capture=TextCaptureConfig(blocked_domains=["evil.example.com"]),
    )


def test_get_actionable_items_excludes_failed_permanent(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    now = datetime.now(UTC)

    from reel_pipeline.models import StateRecord

    permanent = StateRecord(
        content_id="perm1",
        url="https://youtube.com/1",
        normalized_url="https://youtube.com/1",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.FAILED_PERMANENT,
        added_at=now,
        updated_at=now,
    )
    qm.save_state({"perm1": permanent})

    assert qm.get_actionable_items() == []


def test_get_actionable_items_excludes_failed_item_whose_backoff_has_not_elapsed(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    now = datetime.now(UTC)

    from reel_pipeline.models import StateRecord

    waiting = StateRecord(
        content_id="wait1",
        url="https://youtube.com/1",
        normalized_url="https://youtube.com/1",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.FAILED,
        added_at=now,
        updated_at=now,
        attempt_count=1,
        next_retry_at=now + timedelta(minutes=5),
    )
    ready = StateRecord(
        content_id="ready1",
        url="https://youtube.com/2",
        normalized_url="https://youtube.com/2",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.FAILED,
        added_at=now,
        updated_at=now,
        attempt_count=1,
        next_retry_at=now - timedelta(minutes=1),
    )
    qm.save_state({"wait1": waiting, "ready1": ready})

    actionable_ids = {r.content_id for r in qm.get_actionable_items()}
    assert actionable_ids == {"ready1"}


def test_reset_for_retry_by_content_id_resets_failed_permanent_record(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    now = datetime.now(UTC)

    from reel_pipeline.models import ItemStage, StateRecord

    permanent = StateRecord(
        content_id="perm1",
        url="https://youtube.com/1",
        normalized_url="https://youtube.com/1",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.FAILED_PERMANENT,
        added_at=now,
        updated_at=now,
        attempt_count=5,
        error="boom",
        last_completed_stage=ItemStage.DOWNLOADED,
    )
    qm.save_state({"perm1": permanent})

    reset_ids = qm.reset_for_retry(content_id="perm1")

    assert reset_ids == ["perm1"]
    record = qm.load_state()["perm1"]
    assert record.status == ItemStatus.PENDING
    assert record.attempt_count == 0
    assert record.next_retry_at is None
    assert record.error is None
    # Already-completed download stage is preserved so a retry doesn't
    # redownload media that's still on disk.
    assert record.last_completed_stage == ItemStage.DOWNLOADED
    # Regression: reset_for_retry() used to leave updated_at stale, unlike
    # every other mutation path in this file (update_record, _register, etc).
    assert record.updated_at > now


def test_reset_for_retry_ignores_content_id_not_in_failed_permanent_status(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    now = datetime.now(UTC)

    from reel_pipeline.models import StateRecord

    still_failing = StateRecord(
        content_id="stillfailing",
        url="https://youtube.com/1",
        normalized_url="https://youtube.com/1",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.FAILED,
        added_at=now,
        updated_at=now,
        attempt_count=2,
    )
    qm.save_state({"stillfailing": still_failing})

    reset_ids = qm.reset_for_retry(content_id="stillfailing")

    assert reset_ids == []
    record = qm.load_state()["stillfailing"]
    assert record.status == ItemStatus.FAILED
    assert record.attempt_count == 2


def test_reset_for_retry_all_failed_permanent_resets_only_those(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    now = datetime.now(UTC)

    from reel_pipeline.models import StateRecord

    perm_a = StateRecord(
        content_id="perma",
        url="https://youtube.com/a",
        normalized_url="https://youtube.com/a",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.FAILED_PERMANENT,
        added_at=now,
        updated_at=now,
        attempt_count=5,
    )
    perm_b = StateRecord(
        content_id="permb",
        url="https://youtube.com/b",
        normalized_url="https://youtube.com/b",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.FAILED_PERMANENT,
        added_at=now,
        updated_at=now,
        attempt_count=5,
    )
    still_failing = StateRecord(
        content_id="stillfailing",
        url="https://youtube.com/c",
        normalized_url="https://youtube.com/c",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.FAILED,
        added_at=now,
        updated_at=now,
        attempt_count=1,
    )
    qm.save_state({"perma": perm_a, "permb": perm_b, "stillfailing": still_failing})

    reset_ids = qm.reset_for_retry(all_failed_permanent=True)

    assert set(reset_ids) == {"perma", "permb"}
    state = qm.load_state()
    assert state["perma"].status == ItemStatus.PENDING
    assert state["permb"].status == ItemStatus.PENDING
    assert state["stillfailing"].status == ItemStatus.FAILED


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
        content_id="done1",
        url="https://youtube.com/1",
        normalized_url="https://youtube.com/1",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.DONE,
        added_at=now,
        updated_at=now,
    )
    blocked = StateRecord(
        content_id="blocked1",
        url="https://instagram.com/1",
        normalized_url="https://instagram.com/1",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.BLOCKED,
        added_at=now,
        updated_at=now,
    )
    crashed = StateRecord(
        content_id="crashed1",
        url="https://youtube.com/2",
        normalized_url="https://youtube.com/2",
        source=QueueSource.QUEUE_FILE,
        status=ItemStatus.TRANSCRIBING,
        added_at=now,
        updated_at=now,
    )
    qm.save_state({"done1": done, "blocked1": blocked, "crashed1": crashed})

    actionable_ids = {r.content_id for r in qm.get_actionable_items()}
    assert actionable_ids == {"crashed1"}


def test_state_record_new_reliability_fields_have_safe_defaults(tmp_path):
    """Regression test: existing state.json files on disk predate attempt_count/
    next_retry_at/last_completed_stage/skill_error - they must deserialize with
    safe defaults, not raise a validation error.
    """
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    qm.state_file.write_text(
        '{"items": {"legacy1": {'
        '"content_id": "legacy1", "url": "https://youtube.com/1", '
        '"normalized_url": "https://youtube.com/1", "source": "queue_file", '
        '"status": "pending", "added_at": "2026-01-01T00:00:00+00:00", '
        '"updated_at": "2026-01-01T00:00:00+00:00"}}}',
        encoding="utf-8",
    )

    state = qm.load_state()

    record = state["legacy1"]
    assert record.attempt_count == 0
    assert record.next_retry_at is None
    assert record.last_completed_stage is None
    assert record.skill_error is None


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


def _make_drive_settings(tmp_path) -> Settings:
    return Settings(
        project_root=tmp_path,
        download=DownloadConfig(allowed_domains=["drive.google.com"]),
    )


def test_drive_video_url_registers_with_media_content_kind(tmp_path, monkeypatch):
    _install_fake_yt_dlp(monkeypatch, raises=False)
    settings = _make_drive_settings(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://drive.google.com/file/d/abc123/view\n", encoding="utf-8")

    registered = qm.sync_queue_file_into_state()

    assert registered[0].content_kind == "media"


def test_drive_document_url_registers_with_text_content_kind(tmp_path, monkeypatch):
    """A shared Drive document (not a video) - the real case that surfaced
    this: a shared .md skill file that yt-dlp's GoogleDrive video extractor
    couldn't handle - registers as text instead of an unfetchable "media"
    item that would exhaust its retries and land in failed_permanent.
    """
    _install_fake_yt_dlp(monkeypatch, raises=True)
    settings = _make_drive_settings(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://drive.google.com/file/d/abc123/view\n", encoding="utf-8")

    registered = qm.sync_queue_file_into_state()

    assert registered[0].status == ItemStatus.PENDING
    assert registered[0].content_kind == "text"


def test_unenumerated_domain_registers_as_text_by_default(tmp_path):
    """2026-08-10 catch-all policy: an arbitrary knowledge-source domain is
    text-capture by default, not rejected, as long as it isn't explicitly
    blocked - see CLAUDE.md's 2026-08-10 entry.
    """
    settings = make_settings_with_text_capture(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://airtable.com/base/abc\n", encoding="utf-8")

    registered = qm.sync_queue_file_into_state()

    assert registered[0].status == ItemStatus.PENDING
    assert registered[0].content_kind == "text"


def test_blocked_text_capture_domain_still_rejected(tmp_path):
    settings = make_settings_with_text_capture(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://evil.example.com/base/abc\n", encoding="utf-8")

    registered = qm.sync_queue_file_into_state()

    assert registered[0].status == ItemStatus.BLOCKED


def test_add_url_waits_for_a_concurrently_held_state_lock_instead_of_racing(tmp_path):
    """Regression test: add_url() used to do an unlocked load-modify-save, so a
    webhook arriving while another process held the state.json lock could read a
    stale snapshot and clobber that process's write. Now it must wait for the
    lock (briefly) instead of proceeding unguarded.
    """
    import threading
    import time

    from filelock import FileLock

    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://www.youtube.com/watch?v=held1\n", encoding="utf-8")
    qm.sync_queue_file_into_state()  # ensure state.json exists on disk

    lock_path = qm.state_file.with_suffix(".lock")
    held = threading.Event()
    release = threading.Event()

    def hold_lock():
        with FileLock(str(lock_path), timeout=5):
            held.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    held.wait(timeout=5)

    start = time.monotonic()
    release_soon = threading.Timer(0.3, release.set)
    release_soon.start()
    qm.add_url("https://www.youtube.com/watch?v=new1", source=QueueSource.WEBHOOK)
    elapsed = time.monotonic() - start

    holder.join(timeout=5)
    assert elapsed >= 0.25  # actually waited for the externally-held lock
    assert len(qm.load_state()) == 2
