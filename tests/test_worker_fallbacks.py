"""The worker's half of the media and text fallbacks: MediaFound, media-to-text capture,
the last-attempt archive lookup, and the rule that yt-dlp only sees validated hosts."""

from __future__ import annotations

import pytest
from test_worker_flow import (
    CapturingEnricher,
    FakeDownloader,
    FakeEnricher,
    FakeImageDescriber,
    FakeSkillWriter,
    FakeTranscriber,
)

from reel_pipeline.config import DownloadConfig, RetryConfig, Settings
from reel_pipeline.downloader import DownloadError, NoMediaError, PostUnavailableError
from reel_pipeline.models import DownloadResult, ItemStatus, MediaType, TranscriptResult
from reel_pipeline.queue_manager import QueueManager
from reel_pipeline.text_fetcher import ArchiveEligibleError, MediaFound, TextFetchError
from reel_pipeline.worker import WorkerPipeline

_LONG_TEXT = "A thread about building a useful habit tracker, one step at a time. " * 5


def _settings(tmp_path, max_attempts=3, domains=("youtube.com", "x.com")) -> Settings:
    return Settings(
        project_root=tmp_path,
        download=DownloadConfig(allowed_domains=list(domains), blocked_domains=["blocked.example"]),
        retry=RetryConfig(max_attempts=max_attempts, backoff_schedule_minutes=[0]),
    )


def _pipeline(
    settings, *, downloader=None, text_fetcher=None, transcriber=None, enricher=None
) -> WorkerPipeline:
    return WorkerPipeline(
        settings=settings,
        queue_manager=QueueManager(settings),
        downloader=downloader or FakeDownloader(),
        transcriber=transcriber or FakeTranscriber(),
        image_describer=FakeImageDescriber(),
        text_fetcher=text_fetcher or ScriptedFetcher(),
        enricher=enricher or FakeEnricher(),
        skill_writer=FakeSkillWriter(settings),
    )


def _text(content_id: str, text: str = _LONG_TEXT, backend: str = "html-fetch") -> TranscriptResult:
    return TranscriptResult(content_id=content_id, text=text, content_kind="text", backend=backend)


class ScriptedFetcher:
    """Raises `raises` if set, else returns `text`."""

    def __init__(self, raises: Exception | None = None, text: str = _LONG_TEXT):
        self.raises = raises
        self.text = text
        self.calls: list[str] = []

    def fetch(self, url: str, content_id: str) -> TranscriptResult:
        self.calls.append(url)
        if self.raises is not None:
            raise self.raises
        return _text(content_id, self.text)


class ArchivingFetcher(ScriptedFetcher):
    def __init__(self, raises: Exception, archive_error: Exception | None = None):
        super().__init__(raises)
        self.archive_error = archive_error
        self.archive_calls: list[tuple[str, str]] = []

    def fetch_archived(self, url: str, content_id: str, reason: str) -> TranscriptResult:
        self.archive_calls.append((url, reason))
        if self.archive_error is not None:
            raise self.archive_error
        return _text(content_id, f"[Archived copy; the live page {reason}]\n\nold", "wayback")


class RaisingDownloader:
    def __init__(self, error: Exception):
        self.error = error
        self.calls: list[str] = []

    def download(self, url: str, content_id: str) -> DownloadResult:
        self.calls.append(url)
        raise self.error


class SilentTranscriber:
    def transcribe(self, media_path, content_id: str) -> TranscriptResult:
        return TranscriptResult(content_id=content_id, text="", backend="fake")


class RecordingVideoDownloader(FakeDownloader):
    def __init__(self):
        super().__init__()
        self.video_calls: list[str] = []

    def download_video(self, url: str, content_id: str):
        self.video_calls.append(url)
        raise DownloadError("no pictures in this test")


def _only_record(pipeline: WorkerPipeline):
    records = list(pipeline.queue_manager.load_state().values())
    assert len(records) == 1
    return records[0]


def _queue(pipeline: WorkerPipeline, url: str) -> None:
    pipeline.queue_manager.queue_file.write_text(url + "\n", encoding="utf-8")


# --- MediaFound ---


def test_media_found_url_goes_to_the_downloader(tmp_path):
    downloader = FakeDownloader()
    fetcher = ScriptedFetcher(MediaFound(url="https://www.youtube.com/watch?v=abc"))
    pipeline = _pipeline(_settings(tmp_path), downloader=downloader, text_fetcher=fetcher)
    _queue(pipeline, "https://blog.example.com/post")

    summary = pipeline.run_once()

    assert summary.done == 1
    assert downloader.calls == ["https://www.youtube.com/watch?v=abc"]


def test_media_found_result_is_used_without_the_downloader(tmp_path):
    downloader = RaisingDownloader(DownloadError("must not be called"))
    result = DownloadResult(
        content_id="ignored",
        media_type=MediaType.VIDEO,
        media_paths=["/fake/file.mp3"],
        platform="direct",
    )
    fetcher = ScriptedFetcher(MediaFound(result=result))
    pipeline = _pipeline(_settings(tmp_path), downloader=downloader, text_fetcher=fetcher)
    _queue(pipeline, "https://cdn.example.com/talk.mp3")

    summary = pipeline.run_once()

    assert summary.done == 1
    assert downloader.calls == []


def test_media_found_context_reaches_the_transcript(tmp_path):
    enricher = CapturingEnricher()
    result = DownloadResult(
        content_id="x", media_type=MediaType.IMAGE, media_paths=["/fake/1.png"], platform="direct"
    )
    fetcher = ScriptedFetcher(MediaFound(result=result, context="![[scan.pdf]]"))
    pipeline = _pipeline(_settings(tmp_path), text_fetcher=fetcher, enricher=enricher)
    _queue(pipeline, "https://cdn.example.com/scan.pdf")

    pipeline.run_once()

    assert "![[scan.pdf]]" in enricher.transcripts[0].text


def test_media_found_context_merges_with_the_downloaders_caption(tmp_path):
    class Captioned(FakeDownloader):
        def download(self, url, content_id):
            return super().download(url, content_id).model_copy(update={"caption": "from post"})

    enricher = CapturingEnricher()
    fetcher = ScriptedFetcher(MediaFound(url="https://www.youtube.com/watch?v=a", context="lead"))
    pipeline = _pipeline(
        _settings(tmp_path), downloader=Captioned(), text_fetcher=fetcher, enricher=enricher
    )
    _queue(pipeline, "https://blog.example.com/post")

    pipeline.run_once()

    assert "lead" in enricher.transcripts[0].text
    assert "from post" in enricher.transcripts[0].text


def test_media_found_url_that_does_not_validate_never_reaches_a_downloader(tmp_path):
    downloader = FakeDownloader()
    fetcher = ScriptedFetcher(MediaFound(url="https://evil.example.net/video"))
    pipeline = _pipeline(_settings(tmp_path), downloader=downloader, text_fetcher=fetcher)
    _queue(pipeline, "https://blog.example.com/post")

    pipeline.run_once()

    assert downloader.calls == []
    record = _only_record(pipeline)
    assert record.status is not ItemStatus.DONE
    assert "refusing to download unvalidated media" in (record.error or "")


def test_silent_clip_retry_uses_the_found_url_not_the_original(tmp_path):
    downloader = RecordingVideoDownloader()
    fetcher = ScriptedFetcher(MediaFound(url="https://www.youtube.com/watch?v=abc"))
    pipeline = _pipeline(
        _settings(tmp_path),
        downloader=downloader,
        text_fetcher=fetcher,
        transcriber=SilentTranscriber(),
    )
    _queue(pipeline, "https://blog.example.com/post")

    pipeline.run_once()

    assert downloader.video_calls == ["https://www.youtube.com/watch?v=abc"]


def test_silent_direct_file_never_sends_its_host_to_yt_dlp(tmp_path):
    """A direct file's host was never validated against the media allow-list. Handing
    it to yt-dlp would let a submitted link pick any host for it to contact."""
    downloader = RecordingVideoDownloader()
    result = DownloadResult(
        content_id="x", media_type=MediaType.VIDEO, media_paths=["/fake/a.mp3"], platform="direct"
    )
    fetcher = ScriptedFetcher(MediaFound(result=result))
    pipeline = _pipeline(
        _settings(tmp_path),
        downloader=downloader,
        text_fetcher=fetcher,
        transcriber=SilentTranscriber(),
    )
    _queue(pipeline, "https://cdn.example.com/a.mp3")

    pipeline.run_once()

    assert downloader.video_calls == []


def test_silent_clip_retry_still_uses_the_url_for_ordinary_media(tmp_path):
    downloader = RecordingVideoDownloader()
    pipeline = _pipeline(
        _settings(tmp_path), downloader=downloader, transcriber=SilentTranscriber()
    )
    _queue(pipeline, "https://www.youtube.com/watch?v=abc")

    pipeline.run_once()

    assert downloader.video_calls == ["https://www.youtube.com/watch?v=abc"]


# --- A short link that lands on a media host with nothing to download ---


class ShortLinkFetcher(ScriptedFetcher):
    """The short link redirects onto `target`; `target` itself has text."""

    def __init__(self, target: str):
        super().__init__()
        self.target = target

    def fetch(self, url: str, content_id: str) -> TranscriptResult:
        self.calls.append(url)
        if url != self.target:
            raise MediaFound(url=self.target)
        return _text(content_id)


_POST = "https://x.com/someone/status/1"


def test_short_link_to_a_post_with_no_media_keeps_the_posts_text(tmp_path):
    """lnkd.in -> a LinkedIn text post: yt-dlp finds no video. The post's own
    page has the text, so it must be captured, as a direct link to it would be."""
    fetcher = ShortLinkFetcher(_POST)
    pipeline = _pipeline(
        _settings(tmp_path),
        downloader=RaisingDownloader(NoMediaError("No video could be found in this tweet")),
        text_fetcher=fetcher,
    )
    _queue(pipeline, "https://short.example/p/abc")

    summary = pipeline.run_once()

    assert summary.done == 1
    assert fetcher.calls == ["https://short.example/p/abc", _POST]


def test_short_link_plain_download_error_waits_for_the_last_attempt(tmp_path):
    fetcher = ShortLinkFetcher(_POST)
    pipeline = _pipeline(
        _settings(tmp_path, max_attempts=2),
        downloader=RaisingDownloader(DownloadError("Unable to extract video")),
        text_fetcher=fetcher,
    )
    _queue(pipeline, "https://short.example/p/abc")

    pipeline.run_once()

    assert _POST not in fetcher.calls[1:]
    assert _only_record(pipeline).status is ItemStatus.FAILED

    summary = pipeline.run_once()

    assert summary.done == 1
    assert fetcher.calls[-1] == _POST


def test_short_link_refused_download_never_reaches_text_capture(tmp_path):
    fetcher = ShortLinkFetcher(_POST)
    pipeline = _pipeline(
        _settings(tmp_path, max_attempts=1),
        downloader=RaisingDownloader(DownloadError("ERROR: HTTP Error 403: Forbidden")),
        text_fetcher=fetcher,
    )
    _queue(pipeline, "https://short.example/p/abc")

    pipeline.run_once()

    assert fetcher.calls == ["https://short.example/p/abc"]
    assert _only_record(pipeline).status is ItemStatus.FAILED_PERMANENT


def test_short_link_to_a_host_without_text_fallback_keeps_its_error(tmp_path):
    target = "https://www.youtube.com/watch?v=abc"
    fetcher = ShortLinkFetcher(target)
    pipeline = _pipeline(
        _settings(tmp_path, max_attempts=1),
        downloader=RaisingDownloader(NoMediaError("no video")),
        text_fetcher=fetcher,
    )
    _queue(pipeline, "https://short.example/p/abc")

    pipeline.run_once()

    assert fetcher.calls == ["https://short.example/p/abc"]
    assert _only_record(pipeline).status is not ItemStatus.DONE


# --- Media to text ---


def test_no_media_falls_to_text_capture_at_once(tmp_path):
    fetcher = ScriptedFetcher()
    pipeline = _pipeline(
        _settings(tmp_path),
        downloader=RaisingDownloader(NoMediaError("No video could be found in this tweet")),
        text_fetcher=fetcher,
    )
    _queue(pipeline, "https://x.com/someone/status/1")

    summary = pipeline.run_once()

    assert summary.done == 1
    assert fetcher.calls == ["https://x.com/someone/status/1"]
    assert _only_record(pipeline).attempt_count == 0


def test_plain_download_error_waits_for_the_last_attempt(tmp_path):
    fetcher = ScriptedFetcher()
    pipeline = _pipeline(
        _settings(tmp_path, max_attempts=2),
        downloader=RaisingDownloader(DownloadError("HTTP Error 500")),
        text_fetcher=fetcher,
    )
    _queue(pipeline, "https://x.com/someone/status/1")

    pipeline.run_once()

    assert fetcher.calls == []
    assert _only_record(pipeline).status is ItemStatus.FAILED

    summary = pipeline.run_once()

    assert summary.done == 1
    assert fetcher.calls == ["https://x.com/someone/status/1"]


@pytest.mark.parametrize(
    "error",
    [
        DownloadError("ERROR: HTTP Error 403: Forbidden"),
        DownloadError("ERROR: HTTP Error 401: Unauthorized"),
        DownloadError("ERROR: HTTP Error 429: Too Many Requests"),
        DownloadError("redirected to a challenge page"),
        NoMediaError("no video here, log in to see it"),
    ],
)
def test_a_refused_download_never_reaches_text_capture(tmp_path, error):
    fetcher = ScriptedFetcher()
    pipeline = _pipeline(
        _settings(tmp_path, max_attempts=1),
        downloader=RaisingDownloader(error),
        text_fetcher=fetcher,
    )
    _queue(pipeline, "https://x.com/someone/status/1")

    pipeline.run_once()

    assert fetcher.calls == []
    assert _only_record(pipeline).status is ItemStatus.FAILED_PERMANENT


def test_empty_transcript_falls_to_text_capture(tmp_path):
    fetcher = ScriptedFetcher()
    pipeline = _pipeline(
        _settings(tmp_path),
        transcriber=SilentTranscriber(),
        text_fetcher=fetcher,
    )
    _queue(pipeline, "https://x.com/someone/status/1")

    summary = pipeline.run_once()

    assert summary.done == 1
    assert fetcher.calls == ["https://x.com/someone/status/1"]


def test_empty_transcript_elsewhere_stays_terminal(tmp_path):
    fetcher = ScriptedFetcher()
    pipeline = _pipeline(_settings(tmp_path), transcriber=SilentTranscriber(), text_fetcher=fetcher)
    _queue(pipeline, "https://www.youtube.com/watch?v=abc")

    pipeline.run_once()

    assert fetcher.calls == []
    assert _only_record(pipeline).status is ItemStatus.FAILED_PERMANENT


def test_host_outside_the_fallback_list_gets_no_text_capture(tmp_path):
    fetcher = ScriptedFetcher()
    pipeline = _pipeline(
        _settings(tmp_path, max_attempts=1),
        downloader=RaisingDownloader(NoMediaError("Unsupported URL")),
        text_fetcher=fetcher,
    )
    _queue(pipeline, "https://www.youtube.com/watch?v=abc")

    pipeline.run_once()

    assert fetcher.calls == []
    record = _only_record(pipeline)
    assert record.status is ItemStatus.FAILED_PERMANENT
    assert record.error == "Unsupported URL"


def test_post_unavailable_stays_terminal_with_no_text_capture(tmp_path):
    fetcher = ScriptedFetcher()
    pipeline = _pipeline(
        _settings(tmp_path),
        downloader=RaisingDownloader(PostUnavailableError("gone")),
        text_fetcher=fetcher,
    )
    _queue(pipeline, "https://x.com/someone/status/1")

    pipeline.run_once()

    assert fetcher.calls == []
    assert _only_record(pipeline).status is ItemStatus.FAILED_PERMANENT


def test_too_little_text_is_a_failed_fallback(tmp_path):
    pipeline = _pipeline(
        _settings(tmp_path, max_attempts=1),
        downloader=RaisingDownloader(NoMediaError("No video could be found")),
        text_fetcher=ScriptedFetcher(text="Log in to X"),
    )
    _queue(pipeline, "https://x.com/someone/status/1")

    pipeline.run_once()

    record = _only_record(pipeline)
    assert record.status is ItemStatus.FAILED_PERMANENT
    assert "No video could be found" in (record.error or "")
    assert "text capture" in (record.error or "")


def test_fallback_failure_appends_its_reason_to_the_original_error(tmp_path):
    pipeline = _pipeline(
        _settings(tmp_path, max_attempts=1),
        downloader=RaisingDownloader(NoMediaError("No video could be found")),
        text_fetcher=ScriptedFetcher(TextFetchError("page returned HTTP 403")),
    )
    _queue(pipeline, "https://x.com/someone/status/1")

    pipeline.run_once()

    error = _only_record(pipeline).error or ""
    assert "No video could be found" in error
    assert "HTTP 403" in error


def test_media_found_during_the_fallback_counts_as_failed(tmp_path):
    downloader = RaisingDownloader(NoMediaError("No video could be found"))
    pipeline = _pipeline(
        _settings(tmp_path, max_attempts=1),
        downloader=downloader,
        text_fetcher=ScriptedFetcher(MediaFound(url="https://www.youtube.com/watch?v=a")),
    )
    _queue(pipeline, "https://x.com/someone/status/1")

    pipeline.run_once()

    assert downloader.calls == ["https://x.com/someone/status/1"]
    assert _only_record(pipeline).status is ItemStatus.FAILED_PERMANENT


def test_no_media_retries_next_pass_when_attempts_remain_and_fallback_fails(tmp_path):
    pipeline = _pipeline(
        _settings(tmp_path, max_attempts=3),
        downloader=RaisingDownloader(NoMediaError("No video could be found")),
        text_fetcher=ScriptedFetcher(TextFetchError("HTTP 503")),
    )
    _queue(pipeline, "https://x.com/someone/status/1")

    pipeline.run_once()

    record = _only_record(pipeline)
    assert record.status is ItemStatus.FAILED
    assert record.attempt_count == 1


# --- Last-attempt archive ---


def test_archive_is_not_tried_while_attempts_remain(tmp_path):
    fetcher = ArchivingFetcher(ArchiveEligibleError("page returned HTTP 503"))
    pipeline = _pipeline(_settings(tmp_path, max_attempts=2), text_fetcher=fetcher)
    _queue(pipeline, "https://blog.example.com/post")

    pipeline.run_once()

    assert fetcher.archive_calls == []
    assert _only_record(pipeline).status is ItemStatus.FAILED


def test_last_attempt_tries_the_archive(tmp_path):
    fetcher = ArchivingFetcher(ArchiveEligibleError("page returned HTTP 503"))
    pipeline = _pipeline(_settings(tmp_path, max_attempts=2), text_fetcher=fetcher)
    _queue(pipeline, "https://blog.example.com/post")

    pipeline.run_once()
    summary = pipeline.run_once()

    assert summary.done == 1
    assert fetcher.archive_calls == [("https://blog.example.com/post", "returned HTTP 503")]


def test_refused_page_never_reaches_the_archive(tmp_path):
    fetcher = ArchivingFetcher(TextFetchError("page returned HTTP 403"))
    pipeline = _pipeline(_settings(tmp_path, max_attempts=1), text_fetcher=fetcher)
    _queue(pipeline, "https://blog.example.com/post")

    pipeline.run_once()

    assert fetcher.archive_calls == []
    assert _only_record(pipeline).status is ItemStatus.FAILED_PERMANENT


def test_failed_archive_keeps_the_original_error_and_adds_its_reason(tmp_path):
    fetcher = ArchivingFetcher(
        ArchiveEligibleError("page returned HTTP 503"), TextFetchError("no snapshot")
    )
    pipeline = _pipeline(_settings(tmp_path, max_attempts=1), text_fetcher=fetcher)
    _queue(pipeline, "https://blog.example.com/post")

    pipeline.run_once()

    error = _only_record(pipeline).error or ""
    assert "HTTP 503" in error
    assert "no snapshot" in error


def test_fetcher_without_an_archive_method_just_fails(tmp_path):
    fetcher = ScriptedFetcher(ArchiveEligibleError("page returned HTTP 503"))
    pipeline = _pipeline(_settings(tmp_path, max_attempts=1), text_fetcher=fetcher)
    _queue(pipeline, "https://blog.example.com/post")

    pipeline.run_once()

    assert _only_record(pipeline).error == "page returned HTTP 503"
