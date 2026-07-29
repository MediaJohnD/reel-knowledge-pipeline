from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reel_pipeline.config import DownloadConfig, MaintenanceConfig, Settings, TextCaptureConfig
from reel_pipeline.models import (
    DownloadResult,
    EnrichmentResult,
    ItemStatus,
    MediaType,
    QueueSource,
    TranscriptResult,
)
from reel_pipeline.queue_manager import QueueManager
from reel_pipeline.worker import WorkerPipeline


class FakeDownloader:
    def __init__(self):
        self.calls = []

    def download(self, url: str, content_id: str) -> DownloadResult:
        self.calls.append(url)
        return DownloadResult(
            content_id=content_id,
            media_type=MediaType.VIDEO,
            media_paths=[f"/fake/{content_id}.mp3"],
            platform="youtube",
            source_title="Fake Source Title",
            duration_seconds=99.0,
        )


class FakeMultiVideoDownloader:
    def download(self, url: str, content_id: str) -> DownloadResult:
        return DownloadResult(
            content_id=content_id,
            media_type=MediaType.VIDEO,
            media_paths=[f"/fake/{content_id}_1.mp4", f"/fake/{content_id}_2.mp4"],
            platform="instagram",
        )


class FakeImagePostDownloader:
    def download(self, url: str, content_id: str) -> DownloadResult:
        return DownloadResult(
            content_id=content_id,
            media_type=MediaType.IMAGE,
            media_paths=[f"/fake/{content_id}_1.jpg", f"/fake/{content_id}_2.jpg"],
            platform="instagram",
        )


class FakeDownloaderWithRealTmpFile:
    """Writes an actual file under settings.tmp_dir, so tests can verify cleanup."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def download(self, url: str, content_id: str) -> DownloadResult:
        tmp_dir = self.settings.tmp_dir / content_id
        tmp_dir.mkdir(parents=True, exist_ok=True)
        media_path = tmp_dir / "audio.mp3"
        media_path.write_bytes(b"fake-audio-bytes")
        return DownloadResult(
            content_id=content_id,
            media_type=MediaType.VIDEO,
            media_paths=[str(media_path)],
            platform="youtube",
        )


class FailingDownloader:
    def download(self, url: str, content_id: str) -> DownloadResult:
        raise RuntimeError("simulated download failure")


class FailNTimesThenSucceedDownloader:
    def __init__(self, fail_count: int):
        self.fail_count = fail_count
        self.calls = 0

    def download(self, url: str, content_id: str) -> DownloadResult:
        self.calls += 1
        if self.calls <= self.fail_count:
            raise RuntimeError(f"simulated failure #{self.calls}")
        return DownloadResult(
            content_id=content_id,
            media_type=MediaType.VIDEO,
            media_paths=[f"/fake/{content_id}.mp3"],
            platform="youtube",
        )


class FakeTranscriber:
    def transcribe(self, media_path: Path, content_id: str) -> TranscriptResult:
        return TranscriptResult(
            content_id=content_id,
            text="This transcript explains a repeatable three-step build workflow.",
            language="en",
            backend="fake",
        )


class FakePerPathTranscriber:
    """Returns distinct text/duration keyed by the media_path it was asked to
    transcribe, so a test can verify each clip in a multi-video carousel is
    actually transcribed (not just the first, silently) and combined.
    """

    def __init__(self):
        self.calls = []

    def transcribe(self, media_path: Path, content_id: str) -> TranscriptResult:
        self.calls.append(media_path)
        return TranscriptResult(
            content_id=content_id,
            text=f"transcript for {media_path.name}",
            language="en",
            backend="fake",
            duration_seconds=10.0,
        )


class FakeImageDescriber:
    def __init__(self):
        self.calls = []

    def describe(self, media_paths: list[Path], content_id: str) -> TranscriptResult:
        self.calls.append(media_paths)
        return TranscriptResult(
            content_id=content_id,
            text="A carousel describing a repeatable three-step build workflow.",
            backend="fake-vision",
        )


class FakeTextFetcher:
    def __init__(self):
        self.calls = []

    def fetch(self, url: str, content_id: str) -> TranscriptResult:
        self.calls.append(url)
        return TranscriptResult(
            content_id=content_id,
            text="# repo\n\nA CLI tool that automates a repeatable build workflow.",
            content_kind="text",
            backend="github-api",
        )


class FakeEnricher:
    def enrich(self, transcript: TranscriptResult, source_url: str) -> EnrichmentResult:
        return EnrichmentResult(
            title="A Repeatable Build Workflow",
            summary="A short summary of the workflow.",
            tags=["howto", "workflow"],
            tools_mentioned=["Widget"],
            key_takeaways=["Step one", "Step two", "Step three"],
            high_signal=True,
            skill_candidate_reason="Demonstrates a reusable three-step build process.",
        )


class FakeSkillWriter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def generate(self, item):
        path = self.settings.skills_dir / "a-repeatable-build-workflow" / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nname: a-repeatable-build-workflow\n---\nbody\n", encoding="utf-8")
        return path


def make_settings(tmp_path) -> Settings:
    return Settings(
        project_root=tmp_path,
        download=DownloadConfig(allowed_domains=["youtube.com"], blocked_domains=["instagram.com"]),
    )


def build_pipeline(
    settings, downloader, transcriber=None, image_describer=None, text_fetcher=None
) -> WorkerPipeline:
    return WorkerPipeline(
        settings=settings,
        queue_manager=QueueManager(settings),
        downloader=downloader,
        transcriber=transcriber or FakeTranscriber(),
        image_describer=image_describer or FakeImageDescriber(),
        text_fetcher=text_fetcher or FakeTextFetcher(),
        enricher=FakeEnricher(),
        skill_writer=FakeSkillWriter(settings),
    )


def test_failed_item_gets_backoff_and_attempt_count_increment(tmp_path):
    settings = make_settings(tmp_path)
    pipeline = build_pipeline(settings, FailingDownloader())
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=backoff1\n", encoding="utf-8"
    )

    before = datetime.now(UTC)
    pipeline.run_once()

    (record,) = pipeline.queue_manager.load_state().values()
    assert record.status == ItemStatus.FAILED
    assert record.attempt_count == 1
    assert record.next_retry_at is not None
    assert record.next_retry_at > before + timedelta(seconds=30)  # first backoff step is 1 minute


def test_item_becomes_failed_permanent_after_max_attempts(tmp_path):
    settings = make_settings(tmp_path)
    settings.retry.max_attempts = 2
    settings.retry.backoff_schedule_minutes = [0, 0]  # no actual waiting needed for this test
    pipeline = build_pipeline(settings, FailingDownloader())
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=permfail1\n", encoding="utf-8"
    )

    pipeline.run_once()  # attempt 1 -> FAILED
    pipeline.run_once()  # attempt 2 -> FAILED_PERMANENT

    (record,) = pipeline.queue_manager.load_state().values()
    assert record.status == ItemStatus.FAILED_PERMANENT
    assert record.attempt_count == 2

    third = pipeline.run_once()
    assert third.processed == 0  # get_actionable_items() no longer returns it


def test_successful_retry_resets_attempt_count(tmp_path):
    settings = make_settings(tmp_path)
    settings.retry.backoff_schedule_minutes = [0, 0, 0, 0, 0]
    downloader = FailNTimesThenSucceedDownloader(fail_count=1)
    pipeline = build_pipeline(settings, downloader)
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=retry1\n", encoding="utf-8"
    )

    pipeline.run_once()  # fails once
    pipeline.run_once()  # succeeds (next_retry_at already elapsed at 0-minute backoff)

    (record,) = pipeline.queue_manager.load_state().values()
    assert record.status == ItemStatus.DONE
    assert record.attempt_count == 0


def test_happy_path_produces_note_and_skill_and_marks_done(tmp_path):
    settings = make_settings(tmp_path)
    pipeline = build_pipeline(settings, FakeDownloader())
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=happy1\n", encoding="utf-8"
    )

    summary = pipeline.run_once()

    assert summary.processed == 1
    assert summary.done == 1
    assert summary.failed == 0
    assert len(summary.note_paths) == 1
    assert len(summary.skill_paths) == 1
    assert Path(summary.note_paths[0]).exists()
    assert Path(summary.skill_paths[0]).exists()

    state = pipeline.queue_manager.load_state()
    (record,) = state.values()
    assert record.status == ItemStatus.DONE
    assert record.note_path == summary.note_paths[0]
    assert record.skill_path == summary.skill_paths[0]


def test_second_run_once_is_a_no_op_after_success(tmp_path):
    settings = make_settings(tmp_path)
    pipeline = build_pipeline(settings, FakeDownloader())
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=happy1\n", encoding="utf-8"
    )

    pipeline.run_once()
    second_summary = pipeline.run_once()

    assert second_summary.processed == 0


def test_download_failure_marks_failed_and_records_needs_attention(tmp_path):
    settings = make_settings(tmp_path)
    pipeline = build_pipeline(settings, FailingDownloader())
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=fail1\n", encoding="utf-8"
    )

    summary = pipeline.run_once()

    assert summary.processed == 1
    assert summary.failed == 1
    assert summary.done == 0
    assert len(summary.errors) == 1

    state = pipeline.queue_manager.load_state()
    (record,) = state.values()
    assert record.status == ItemStatus.FAILED
    assert "simulated download failure" in (record.error or "")

    needs_attention = pipeline.queue_manager.needs_attention_file.read_text(encoding="utf-8")
    assert "simulated download failure" in needs_attention


def test_webhook_ingested_item_is_processed_same_as_queue_file(tmp_path):
    settings = make_settings(tmp_path)
    pipeline = build_pipeline(settings, FakeDownloader())
    pipeline.queue_manager.add_url(
        "https://www.youtube.com/watch?v=webhook1", source=QueueSource.WEBHOOK
    )

    summary = pipeline.run_once()

    assert summary.done == 1


def test_image_post_routes_to_image_describer_not_transcriber(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        download=DownloadConfig(allowed_domains=["instagram.com"], blocked_domains=[]),
    )

    def fail_transcribe(media_path, content_id):
        raise AssertionError("transcriber should not be used for an image-only post")

    fake_transcriber = FakeTranscriber()
    fake_transcriber.transcribe = fail_transcribe
    fake_image_describer = FakeImageDescriber()

    pipeline = build_pipeline(
        settings,
        FakeImagePostDownloader(),
        transcriber=fake_transcriber,
        image_describer=fake_image_describer,
    )
    pipeline.queue_manager.queue_file.write_text(
        "https://www.instagram.com/p/carousel1/\n", encoding="utf-8"
    )

    summary = pipeline.run_once()

    assert summary.done == 1
    assert len(fake_image_describer.calls) == 1
    described_paths = fake_image_describer.calls[0]
    assert len(described_paths) == 2
    assert described_paths[0].name.endswith("_1.jpg")
    assert described_paths[1].name.endswith("_2.jpg")


def test_successful_processing_cleans_up_tmp_dir(tmp_path):
    settings = make_settings(tmp_path)
    downloader = FakeDownloaderWithRealTmpFile(settings)
    pipeline = build_pipeline(settings, downloader)
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=cleanup1\n", encoding="utf-8"
    )

    summary = pipeline.run_once()
    assert summary.done == 1

    (content_id,) = pipeline.queue_manager.load_state().keys()
    assert not (settings.tmp_dir / content_id).exists()


def test_repeated_identical_failure_does_not_duplicate_needs_attention_lines(tmp_path):
    settings = make_settings(tmp_path)
    settings.retry.backoff_schedule_minutes = [0, 0]  # retry immediately available in this test
    pipeline = build_pipeline(settings, FailingDownloader())
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=fail-repeat\n", encoding="utf-8"
    )

    pipeline.run_once()
    pipeline.run_once()
    pipeline.run_once()

    needs_attention_text = pipeline.queue_manager.needs_attention_file.read_text(encoding="utf-8")
    lines = [line for line in needs_attention_text.splitlines() if line]
    assert len(lines) == 1


def test_needs_attention_gets_new_line_when_error_reason_changes(tmp_path):
    settings = make_settings(tmp_path)
    settings.retry.backoff_schedule_minutes = [0, 0]  # retry immediately available in this test

    class FlakyDownloader:
        def __init__(self):
            self.attempt = 0

        def download(self, url: str, content_id: str) -> DownloadResult:
            self.attempt += 1
            raise RuntimeError(f"failure variant {self.attempt}")

    pipeline = build_pipeline(settings, FlakyDownloader())
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=fail-changing\n", encoding="utf-8"
    )

    pipeline.run_once()
    pipeline.run_once()

    needs_attention_text = pipeline.queue_manager.needs_attention_file.read_text(encoding="utf-8")
    lines = [line for line in needs_attention_text.splitlines() if line]
    assert len(lines) == 2


def test_run_once_sweeps_stale_tmp_dirs_regardless_of_status(tmp_path):
    settings = make_settings(tmp_path)
    pipeline = build_pipeline(settings, FakeDownloader())
    settings.ensure_directories()

    stale_dir = settings.tmp_dir / "orphaned-content-id"
    stale_dir.mkdir(parents=True)
    (stale_dir / "leftover.mp3").write_bytes(b"leftover")
    old_time = time.time() - 40 * 86400
    os.utime(stale_dir, (old_time, old_time))

    fresh_dir = settings.tmp_dir / "fresh-content-id"
    fresh_dir.mkdir(parents=True)
    (fresh_dir / "leftover.mp3").write_bytes(b"leftover")

    pipeline.run_once()

    assert not stale_dir.exists()
    assert fresh_dir.exists()


def test_tmp_sweep_disabled_when_retention_is_zero(tmp_path):
    settings = make_settings(tmp_path)
    settings.maintenance = MaintenanceConfig(tmp_retention_days=0)
    pipeline = build_pipeline(settings, FakeDownloader())
    settings.ensure_directories()

    stale_dir = settings.tmp_dir / "orphaned-content-id"
    stale_dir.mkdir(parents=True)
    old_time = time.time() - 999 * 86400
    os.utime(stale_dir, (old_time, old_time))

    pipeline.run_once()

    assert stale_dir.exists()


def test_text_capture_item_routes_to_text_fetcher_not_downloader(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        download=DownloadConfig(allowed_domains=["youtube.com"]),
        text_capture=TextCaptureConfig(allowed_domains=["github.com"]),
    )
    # FailingDownloader (defined earlier in this file) raises on any call - if the
    # worker mistakenly routed this text item through Downloader, the item would
    # end up FAILED instead of DONE, which the assertions below would catch.
    text_fetcher = FakeTextFetcher()
    pipeline = build_pipeline(settings, FailingDownloader(), text_fetcher=text_fetcher)
    pipeline.queue_manager.queue_file.write_text(
        "https://github.com/owner/repo\n", encoding="utf-8"
    )

    summary = pipeline.run_once()

    assert summary.done == 1
    assert text_fetcher.calls == ["https://github.com/owner/repo"]


def test_multi_video_carousel_transcribes_every_clip_and_combines_them(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        download=DownloadConfig(allowed_domains=["instagram.com"], blocked_domains=[]),
    )
    fake_transcriber = FakePerPathTranscriber()
    pipeline = build_pipeline(settings, FakeMultiVideoDownloader(), transcriber=fake_transcriber)
    pipeline.queue_manager.queue_file.write_text(
        "https://www.instagram.com/p/multivideo1/\n", encoding="utf-8"
    )

    summary = pipeline.run_once()

    assert summary.done == 1
    assert len(fake_transcriber.calls) == 2  # both clips transcribed, not just the first
    note_path = Path(summary.note_paths[0])
    note_text = note_path.read_text(encoding="utf-8")
    assert "transcript for" in note_text
    assert note_text.count("transcript for") == 2  # both clips' text made it into the note


def test_reprocessing_with_a_new_title_removes_the_stale_note_and_skill(tmp_path):
    # Regression test: enrichment (an LLM call) isn't deterministic, so reprocessing
    # an already-DONE item (a concurrent duplicate run, or a manual retry) can produce
    # a different title -> a different <title-slug>.md filename. Without
    # cleanup, the old file/skill-dir would sit around forever as an orphan duplicate.
    settings = make_settings(tmp_path)

    class VariableTitleEnricher:
        def __init__(self):
            self.call_count = 0

        def enrich(self, transcript, source_url):
            self.call_count += 1
            title = "First Title" if self.call_count == 1 else "Second Title"
            return EnrichmentResult(
                title=title,
                summary="A short summary.",
                tags=["howto"],
                tools_mentioned=["Widget"],
                key_takeaways=["Step one"],
                high_signal=True,
                skill_candidate_reason="Reusable process.",
            )

    pipeline = WorkerPipeline(
        settings=settings,
        queue_manager=QueueManager(settings),
        downloader=FakeDownloader(),
        transcriber=FakeTranscriber(),
        image_describer=FakeImageDescriber(),
        text_fetcher=FakeTextFetcher(),
        enricher=VariableTitleEnricher(),
        skill_writer=FakeSkillWriter(settings),
    )
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=rerun1\n", encoding="utf-8"
    )
    pipeline.run_once()
    (content_id,) = pipeline.queue_manager.load_state().keys()
    record = pipeline.queue_manager.load_state()[content_id]
    assert record.note_path is not None
    first_note_path = Path(record.note_path)
    assert first_note_path.exists()

    # Simulate reprocessing the same (already-DONE) record - e.g. the concurrency race
    # this fix closes, or a manual retry - producing a different title this time.
    pipeline.process_item(record)

    assert not first_note_path.exists()  # stale note cleaned up
    second_record = pipeline.queue_manager.load_state()[content_id]
    assert second_record.note_path is not None
    assert Path(second_record.note_path).exists()
    assert second_record.note_path != str(first_note_path)


def test_resuming_a_crash_interrupted_item_does_not_redownload_if_media_still_on_disk(tmp_path):
    """Regression test: process_item() used to always call downloader.download()
    regardless of the record's last_completed_stage, so a crash after a
    successful download (e.g. during transcription) re-downloaded on the next
    run_once() - discarding already-completed, possibly-costly work.
    """
    settings = make_settings(tmp_path)
    downloader = FakeDownloaderWithRealTmpFile(settings)
    pipeline = build_pipeline(settings, downloader)
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=resume1\n", encoding="utf-8"
    )
    pipeline.queue_manager.sync_queue_file_into_state()
    (record,) = pipeline.queue_manager.load_state().values()

    # Simulate a crash right after a successful download: the media file exists
    # on disk and last_completed_stage reflects it, but status never advanced
    # past DOWNLOADING (the crash happened before the next update_record() call).
    from reel_pipeline.models import ItemStage

    media_path = settings.tmp_dir / record.content_id / "audio.mp3"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"pre-existing audio from a completed download")
    record.status = ItemStatus.DOWNLOADING
    record.last_completed_stage = ItemStage.DOWNLOADED
    pipeline.queue_manager.update_record(record)

    class CountingDownloader:
        def __init__(self):
            self.calls = 0

        def download(self, url, content_id):
            self.calls += 1
            raise AssertionError("download() should not be called - media already on disk")

    pipeline.downloader = CountingDownloader()

    result = pipeline.process_item(record)

    assert result.status == ItemStatus.DONE


def test_resuming_a_crash_interrupted_item_does_not_retranscribe_if_transcript_cached(tmp_path):
    """Regression test: a crash after a successful transcription (e.g. during
    enrichment) used to redo transcription on the next run_once() even though
    last_completed_stage was never actually advanced past DOWNLOADED - there
    was no cached transcript artifact to resume from. Now the transcript is
    cached alongside the download, so a resume from TRANSCRIBED skips both
    the download and the transcription call.
    """
    settings = make_settings(tmp_path)
    downloader = FakeDownloaderWithRealTmpFile(settings)
    pipeline = build_pipeline(settings, downloader)
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=resumetranscribed1\n", encoding="utf-8"
    )
    pipeline.queue_manager.sync_queue_file_into_state()
    (record,) = pipeline.queue_manager.load_state().values()

    from reel_pipeline.models import ItemStage, TranscriptResult

    tmp_dir = settings.tmp_dir / record.content_id
    tmp_dir.mkdir(parents=True, exist_ok=True)
    (tmp_dir / "transcript.json").write_text(
        TranscriptResult(
            content_id=record.content_id,
            text="Cached transcript from a completed prior attempt.",
            backend="fake",
        ).model_dump_json(),
        encoding="utf-8",
    )
    record.status = ItemStatus.TRANSCRIBING
    record.last_completed_stage = ItemStage.TRANSCRIBED
    pipeline.queue_manager.update_record(record)

    class CountingTranscriber:
        def __init__(self):
            self.calls = 0

        def transcribe(self, media_path, content_id):
            self.calls += 1
            raise AssertionError("transcribe() should not be called - transcript already cached")

    class CountingDownloader:
        def download(self, url, content_id):
            raise AssertionError("download() should not be called - transcript already cached")

    pipeline.downloader = CountingDownloader()
    pipeline.transcriber = CountingTranscriber()

    result = pipeline.process_item(record)

    assert result.status == ItemStatus.DONE


def test_resuming_a_crash_interrupted_item_does_not_reenrich_if_enrichment_cached(tmp_path):
    """Same rationale as the transcript-caching test, one stage further: a
    crash after a successful enrichment (e.g. during note writing) should not
    redo the LLM enrichment call on the next run_once().
    """
    settings = make_settings(tmp_path)
    downloader = FakeDownloaderWithRealTmpFile(settings)
    pipeline = build_pipeline(settings, downloader)
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=resumeenriched1\n", encoding="utf-8"
    )
    pipeline.queue_manager.sync_queue_file_into_state()
    (record,) = pipeline.queue_manager.load_state().values()

    from reel_pipeline.models import EnrichmentResult, ItemStage, TranscriptResult

    tmp_dir = settings.tmp_dir / record.content_id
    tmp_dir.mkdir(parents=True, exist_ok=True)
    (tmp_dir / "transcript.json").write_text(
        TranscriptResult(
            content_id=record.content_id,
            text="Cached transcript from a completed prior attempt.",
            backend="fake",
        ).model_dump_json(),
        encoding="utf-8",
    )
    (tmp_dir / "enrichment.json").write_text(
        EnrichmentResult(
            title="Cached Title",
            summary="Cached summary.",
            tags=["cached"],
        ).model_dump_json(),
        encoding="utf-8",
    )
    record.status = ItemStatus.ENRICHING
    record.last_completed_stage = ItemStage.ENRICHED
    pipeline.queue_manager.update_record(record)

    class CountingEnricher:
        def enrich(self, transcript, source_url):
            raise AssertionError("enrich() should not be called - enrichment already cached")

    class CountingDownloader:
        def download(self, url, content_id):
            raise AssertionError("download() should not be called - enrichment already cached")

    class CountingTranscriber:
        def transcribe(self, media_path, content_id):
            raise AssertionError("transcribe() should not be called - enrichment already cached")

    pipeline.downloader = CountingDownloader()
    pipeline.transcriber = CountingTranscriber()
    pipeline.enricher = CountingEnricher()

    result = pipeline.process_item(record)

    assert result.status == ItemStatus.DONE
    (final_record,) = pipeline.queue_manager.load_state().values()
    assert final_record.note_path is not None
    assert "Cached Title" in Path(final_record.note_path).read_text(encoding="utf-8")


def test_missing_cached_transcript_falls_back_to_redownload_not_just_retranscribe(tmp_path):
    """If last_completed_stage says TRANSCRIBED but the transcript cache file is
    gone (e.g. data/tmp/ was cleaned) and the download cache is also gone,
    process_item must fall all the way back to redownloading - trusting the
    stage marker alone without a matching artifact would crash instead.
    """
    settings = make_settings(tmp_path)

    class OneShotDownloader:
        def __init__(self):
            self.calls = 0

        def download(self, url, content_id):
            self.calls += 1
            return DownloadResult(
                content_id=content_id,
                media_type=MediaType.VIDEO,
                media_paths=[f"/fake/{content_id}.mp3"],
                platform="youtube",
            )

    downloader = OneShotDownloader()
    pipeline = build_pipeline(settings, downloader)
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=fallback1\n", encoding="utf-8"
    )
    pipeline.queue_manager.sync_queue_file_into_state()
    (record,) = pipeline.queue_manager.load_state().values()

    from reel_pipeline.models import ItemStage

    record.status = ItemStatus.TRANSCRIBING
    record.last_completed_stage = ItemStage.TRANSCRIBED
    pipeline.queue_manager.update_record(record)

    result = pipeline.process_item(record)

    assert result.status == ItemStatus.DONE
    assert downloader.calls == 1


class SlowFakeDownloader:
    """Sleeps mid-download and tracks concurrent entries - lets a test prove
    two run_once() calls never overlap inside the locked section, regardless
    of which WorkerPipeline instance (i.e. which "process") is running it.
    """

    def __init__(self):
        self.active = 0
        self.max_concurrent_observed = 0
        self._lock = threading.Lock()

    def download(self, url: str, content_id: str) -> DownloadResult:
        with self._lock:
            self.active += 1
            self.max_concurrent_observed = max(self.max_concurrent_observed, self.active)
        time.sleep(0.2)
        with self._lock:
            self.active -= 1
        return DownloadResult(
            content_id=content_id,
            media_type=MediaType.VIDEO,
            media_paths=[f"/fake/{content_id}.mp3"],
            platform="youtube",
        )


def test_concurrent_run_once_calls_never_interleave(tmp_path):
    """Two separate WorkerPipeline instances (simulating two separate processes -
    e.g. a manual CLI run-once racing the webhook server's own background run)
    must not process items concurrently - run_once()'s cross-process file lock
    should serialize them.
    """
    settings = make_settings(tmp_path)
    shared_downloader = SlowFakeDownloader()

    def build():
        return WorkerPipeline(
            settings=settings,
            queue_manager=QueueManager(settings),
            downloader=shared_downloader,
            transcriber=FakeTranscriber(),
            image_describer=FakeImageDescriber(),
            text_fetcher=FakeTextFetcher(),
            enricher=FakeEnricher(),
            skill_writer=FakeSkillWriter(settings),
        )

    pipeline_a = build()
    pipeline_a.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=race1\nhttps://www.youtube.com/watch?v=race2\n",
        encoding="utf-8",
    )

    pipeline_b = build()

    results = {}

    def run(name, pipeline):
        results[name] = pipeline.run_once()

    t1 = threading.Thread(target=run, args=("a", pipeline_a))
    t2 = threading.Thread(target=run, args=("b", pipeline_b))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert shared_downloader.max_concurrent_observed == 1
    # Between them, both queued items were processed exactly once (whichever
    # thread's run_once() picked them up first, since the second call's
    # sync_queue_file_into_state() runs against an already-drained queue.txt).
    total_done = results["a"].done + results["b"].done
    assert total_done == 2


def test_skill_generation_failure_does_not_undo_a_successful_note(tmp_path):
    """Regression test: a skill_writer.generate() exception used to be caught by
    the same try/except as the whole pipeline, marking an already-written note
    as FAILED - causing the entire pipeline to re-run on the next pass even
    though the note had already succeeded.
    """
    settings = make_settings(tmp_path)

    class FailingSkillWriter:
        def generate(self, item):
            raise RuntimeError("simulated skill generation failure")

    pipeline = WorkerPipeline(
        settings=settings,
        queue_manager=QueueManager(settings),
        downloader=FakeDownloader(),
        transcriber=FakeTranscriber(),
        image_describer=FakeImageDescriber(),
        text_fetcher=FakeTextFetcher(),
        enricher=FakeEnricher(),
        skill_writer=FailingSkillWriter(),
    )
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=skillfail1\n", encoding="utf-8"
    )

    summary = pipeline.run_once()

    assert summary.done == 1
    assert summary.failed == 0
    (record,) = pipeline.queue_manager.load_state().values()
    assert record.status == ItemStatus.DONE
    assert record.note_path is not None
    assert Path(record.note_path).exists()
    assert record.skill_path is None
    assert record.skill_error is not None
    assert "simulated skill generation failure" in record.skill_error


def test_note_success_is_persisted_before_skill_generation_runs(tmp_path):
    """Regression test for Important #1: write_note() success must be durable in
    state.json BEFORE skill_writer.generate() is attempted, so a crash during the
    slowest/most kill-prone remaining step (skill generation) doesn't lose an
    already-completed note-writing success. Uses a spy on update_record() plus a
    skill_writer that raises, to prove ordering: the DONE-status write must have
    already happened by the time generate() runs.
    """
    settings = make_settings(tmp_path)

    calls: list[str] = []

    class ExplodingSkillWriter:
        def generate(self, item):
            calls.append("generate")
            raise RuntimeError("boom during skill generation")

    queue_manager = QueueManager(settings)
    original_update_record = queue_manager.update_record

    def spying_update_record(record):
        if record.status == ItemStatus.DONE:
            calls.append("update_record(DONE)")
        return original_update_record(record)

    queue_manager.update_record = spying_update_record  # type: ignore[method-assign]

    pipeline = WorkerPipeline(
        settings=settings,
        queue_manager=queue_manager,
        downloader=FakeDownloader(),
        transcriber=FakeTranscriber(),
        image_describer=FakeImageDescriber(),
        text_fetcher=FakeTextFetcher(),
        enricher=FakeEnricher(),
        skill_writer=ExplodingSkillWriter(),
    )
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=persistfirst1\n", encoding="utf-8"
    )

    pipeline.run_once()

    assert "update_record(DONE)" in calls
    assert "generate" in calls
    assert calls.index("update_record(DONE)") < calls.index("generate")

    (record,) = pipeline.queue_manager.load_state().values()
    assert record.status == ItemStatus.DONE
    assert record.note_path is not None


def test_transition_to_failed_permanent_appends_needs_attention_even_with_same_error(tmp_path):
    """Regression test for Important #4: the FAILED -> FAILED_PERMANENT transition
    must always append a needs-attention line, even when the error message is
    identical on every attempt (the overwhelmingly common shape of a permanent
    failure) - otherwise the moment the pipeline gives up on an item never
    reaches the human-readable log.
    """
    settings = make_settings(tmp_path)
    settings.retry.max_attempts = 2
    settings.retry.backoff_schedule_minutes = [0, 0]
    pipeline = build_pipeline(settings, FailingDownloader())
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=givingup1\n", encoding="utf-8"
    )

    pipeline.run_once()  # attempt 1 -> FAILED, same error logged (first occurrence)
    pipeline.run_once()  # attempt 2 -> FAILED_PERMANENT, same error text as attempt 1

    (record,) = pipeline.queue_manager.load_state().values()
    assert record.status == ItemStatus.FAILED_PERMANENT

    needs_attention = pipeline.queue_manager.needs_attention_file.read_text(encoding="utf-8")
    lines = [line for line in needs_attention.splitlines() if line.strip()]
    giving_up_lines = [line for line in lines if "giving up" in line]
    assert len(giving_up_lines) == 1
    assert "2 attempts" in giving_up_lines[0]


def test_add_url_is_not_blocked_by_an_in_progress_run_once(tmp_path):
    """Regression test proving the headline property Task 3 was built for: add_url()
    (webhook registration) must stay fast even while a run_once() pass is actively
    running and holding the separate run_once.lock - it only waits on the much
    briefer per-mutation state.json lock. Without this, every webhook POST would
    block for the duration of a whole backlog pass whenever one happened to be
    running, defeating the purpose of running the worker in the background.
    """
    settings = make_settings(tmp_path)
    shared_downloader = SlowFakeDownloader()

    def build():
        return WorkerPipeline(
            settings=settings,
            queue_manager=QueueManager(settings),
            downloader=shared_downloader,
            transcriber=FakeTranscriber(),
            image_describer=FakeImageDescriber(),
            text_fetcher=FakeTextFetcher(),
            enricher=FakeEnricher(),
            skill_writer=FakeSkillWriter(settings),
        )

    pipeline = build()
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=slowrun1\n", encoding="utf-8"
    )

    run_once_thread = threading.Thread(target=pipeline.run_once)
    run_once_thread.start()
    time.sleep(0.05)  # give run_once() time to acquire its lock and start downloading

    add_url_qm = QueueManager(settings)
    start = time.monotonic()
    add_url_qm.add_url(
        "https://www.youtube.com/watch?v=addedwhilebusy1", source=QueueSource.WEBHOOK
    )
    elapsed = time.monotonic() - start

    run_once_thread.join(timeout=10)

    # SlowFakeDownloader sleeps 0.2s; add_url() must return well under that,
    # proving it never waited for the whole run_once() pass to finish.
    assert elapsed < 0.15


def test_run_once_warns_when_state_size_crosses_configured_threshold(tmp_path, caplog):
    """Regression test: state.json's O(n^2) full-rewrite-per-mutation cost was a
    silent deferral - nothing would ever tell an operator their backlog had
    outgrown this project's "handful of reels" assumption. run_once() now
    logs a warning once item count reaches maintenance.state_size_warning_threshold.
    """
    import logging

    settings = Settings(
        project_root=tmp_path,
        download=DownloadConfig(allowed_domains=["youtube.com"], blocked_domains=[]),
        maintenance=MaintenanceConfig(state_size_warning_threshold=2),
    )
    qm = QueueManager(settings)
    now = datetime.now(UTC)
    from reel_pipeline.models import StateRecord

    qm.save_state(
        {
            "a": StateRecord(
                content_id="a",
                url="https://youtube.com/a",
                normalized_url="https://youtube.com/a",
                source=QueueSource.QUEUE_FILE,
                status=ItemStatus.DONE,
                added_at=now,
                updated_at=now,
            ),
            "b": StateRecord(
                content_id="b",
                url="https://youtube.com/b",
                normalized_url="https://youtube.com/b",
                source=QueueSource.QUEUE_FILE,
                status=ItemStatus.DONE,
                added_at=now,
                updated_at=now,
            ),
        }
    )
    pipeline = build_pipeline(settings, FakeDownloader())

    with caplog.at_level(logging.WARNING):
        pipeline.run_once()

    assert any("state.json" in record.message for record in caplog.records)


def test_run_once_does_not_warn_when_state_size_is_below_threshold(tmp_path, caplog):
    import logging

    settings = make_settings(tmp_path)  # default threshold (500), well above 1 item
    pipeline = build_pipeline(settings, FakeDownloader())
    pipeline.queue_manager.queue_file.write_text(
        "https://www.youtube.com/watch?v=belowthreshold1\n", encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING):
        pipeline.run_once()

    assert not any("state.json" in record.message for record in caplog.records)
