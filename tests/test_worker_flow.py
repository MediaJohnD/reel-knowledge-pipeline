from __future__ import annotations

import os
import threading
import time
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
    pipeline = build_pipeline(
        settings, FakeMultiVideoDownloader(), transcriber=fake_transcriber
    )
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
    # a different title -> a different <content_id>-<title-slug>.md filename. Without
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
