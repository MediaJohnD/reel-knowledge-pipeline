from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml

from reel_pipeline.config import DownloadConfig, Settings
from reel_pipeline.downloader import (
    DispatchingDownloader,
    DownloadError,
    GalleryDlDownloader,
    NoMediaError,
    PostUnavailableError,
    YtDlpDownloader,
    is_refusal,
)
from reel_pipeline.models import DownloadResult, MediaType


def _install_fake_yt_dlp(monkeypatch, extract_info_impl=None):
    """Fakes the yt_dlp package well enough to exercise YtDlpDownloader.download()
    without a real network call - mirrors the faster_whisper fake in
    tests/test_transcriber.py (lazy-imported module, so sys.modules injection works).
    """
    captured_opts = []

    class FakeDownloadError(Exception):
        pass

    class FakeYoutubeDL:
        def __init__(self, opts):
            captured_opts.append(opts)
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def extract_info(self, url, download=True):
            if extract_info_impl is not None:
                return extract_info_impl(url)
            # Write the file yt-dlp's outtmpl (audio.%(ext)s) would have produced.
            import pathlib

            outtmpl = self.opts["outtmpl"]
            path = pathlib.Path(outtmpl.replace("%(ext)s", "mp3"))
            path.write_bytes(b"fake-audio")
            return {"extractor_key": "Fake", "title": "Fake Title", "duration": 12.0}

    fake_module = types.ModuleType("yt_dlp")
    fake_module.YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
    fake_utils_module = types.ModuleType("yt_dlp.utils")
    fake_utils_module.DownloadError = FakeDownloadError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_module)
    monkeypatch.setitem(sys.modules, "yt_dlp.utils", fake_utils_module)
    return captured_opts


def test_dispatches_instagram_urls_to_gallery_dl(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)
    calls = []
    monkeypatch.setattr(
        GalleryDlDownloader,
        "download",
        lambda self, url, content_id: (
            calls.append(("gallery-dl", url))
            or DownloadResult(content_id=content_id, media_paths=["x"], platform="instagram")
        ),
    )
    monkeypatch.setattr(
        YtDlpDownloader,
        "download",
        lambda self, url, content_id: pytest.fail("yt-dlp should not be used for Instagram"),
    )

    downloader = DispatchingDownloader(settings)
    downloader.download("https://www.instagram.com/reel/abc123/", "cid1")

    assert calls == [("gallery-dl", "https://www.instagram.com/reel/abc123/")]


def test_dispatches_non_instagram_urls_to_yt_dlp(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)
    calls = []
    monkeypatch.setattr(
        YtDlpDownloader,
        "download",
        lambda self, url, content_id: (
            calls.append(("yt-dlp", url))
            or DownloadResult(content_id=content_id, media_paths=["x"], platform="youtube")
        ),
    )
    monkeypatch.setattr(
        GalleryDlDownloader,
        "download",
        lambda self, url, content_id: pytest.fail("gallery-dl should not be used for YouTube"),
    )

    downloader = DispatchingDownloader(settings)
    downloader.download("https://www.youtube.com/watch?v=abc123", "cid2")

    assert calls == [("yt-dlp", "https://www.youtube.com/watch?v=abc123")]


def test_gallery_dl_downloader_raises_clear_error_without_cookies(tmp_path):
    settings = Settings(project_root=tmp_path)
    downloader = GalleryDlDownloader(settings)

    with pytest.raises(RuntimeError, match="REEL_INSTAGRAM_COOKIES"):
        downloader.download("https://www.instagram.com/reel/abc123/", "cid3")


def test_gallery_dl_runs_the_copy_beside_this_python_without_path(tmp_path, monkeypatch):
    # A venv python launched without activation has no Scripts dir on PATH, and a bare
    # "gallery-dl" then failed with WinError 2 and used up one of the item's retries.
    from reel_pipeline.config import Settings as SettingsCls

    bin_dir = tmp_path / "venv-bin"
    bin_dir.mkdir()
    installed = bin_dir / ("gallery-dl.exe" if sys.platform == "win32" else "gallery-dl")
    installed.write_bytes(b"")
    installed.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(bin_dir / "python"))
    monkeypatch.setenv("PATH", "")

    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        out_dir = settings.tmp_dir / "cid-path"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "post.jpg").write_bytes(b"fake-jpg")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    settings = SettingsCls(project_root=tmp_path, instagram_cookies_browser="chrome")
    monkeypatch.setattr(subprocess, "run", fake_run)

    GalleryDlDownloader(settings).download("https://www.instagram.com/p/abc/", "cid-path")

    assert Path(commands[0][0]).resolve() == installed.resolve()


def test_gallery_dl_falls_back_to_path_lookup_when_not_beside_python(tmp_path, monkeypatch):
    from reel_pipeline.downloader import _gallery_dl_executable

    monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))

    assert _gallery_dl_executable() == "gallery-dl"


def test_gallery_dl_downloader_detects_video_over_images(tmp_path, monkeypatch):
    from reel_pipeline.config import Settings as SettingsCls

    settings = SettingsCls(project_root=tmp_path, instagram_cookies_browser="chrome")
    content_id = "cid-video"
    out_dir = settings.tmp_dir / content_id

    def fake_run(command, **kwargs):
        if Path(command[0]).stem == "gallery-dl":
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "post_1.jpg").write_bytes(b"fake-jpg")
            (out_dir / "post.mp4").write_bytes(b"fake-mp4")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout="0\n", stderr="")
        pytest.fail(f"unexpected subprocess call in this test: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    downloader = GalleryDlDownloader(settings)
    result = downloader.download("https://www.instagram.com/reel/abc/", content_id)

    assert result.media_type is MediaType.VIDEO
    assert result.media_paths == [str(out_dir / "post.mp4")]


def test_gallery_dl_downloader_detects_image_carousel(tmp_path, monkeypatch):
    from reel_pipeline.config import Settings as SettingsCls

    settings = SettingsCls(project_root=tmp_path, instagram_cookies_browser="chrome")
    content_id = "cid-carousel"
    out_dir = settings.tmp_dir / content_id

    def fake_run(command, **kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "post_1.jpg").write_bytes(b"fake-jpg-1")
        (out_dir / "post_2.jpg").write_bytes(b"fake-jpg-2")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GalleryDlDownloader(settings).download("https://www.instagram.com/p/abc/", content_id)

    assert result.media_type is MediaType.IMAGE
    assert result.media_paths == [
        str(out_dir / "post_1.jpg"),
        str(out_dir / "post_2.jpg"),
    ]


def test_gallery_dl_downloader_routes_silent_video_to_image_path(tmp_path, monkeypatch):
    """A "photo" post Instagram actually serves as a silent .mp4 (a
    "motion photo") used to be classified as MediaType.VIDEO purely by file
    extension, sending it to Transcriber, which then fails outright since
    faster-whisper has nothing to transcribe. It should be treated as an
    image instead: a frame extracted and routed through the vision path.
    """
    settings = Settings(project_root=tmp_path, instagram_cookies_browser="chrome")
    content_id = "cid-silent-video"
    out_dir = settings.tmp_dir / content_id
    frame_path = out_dir / "post.frame.jpg"

    def fake_run(command, **kwargs):
        if Path(command[0]).stem == "gallery-dl":
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "post.mp4").write_bytes(b"fake-silent-mp4")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == "ffmpeg":
            frame_path.write_bytes(b"fake-extracted-frame")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        pytest.fail(f"unexpected subprocess call in this test: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GalleryDlDownloader(settings).download("https://www.instagram.com/p/abc/", content_id)

    assert result.media_type is MediaType.IMAGE
    assert result.media_paths == [str(frame_path)]


def test_gallery_dl_downloader_ignores_silent_video_when_a_real_video_exists(tmp_path, monkeypatch):
    """A carousel with both a real (audio) video and a silent one keeps the
    binary VIDEO-vs-IMAGE split today's design already has - the whole post
    is VIDEO, using only the real video path. See _video_has_audio_stream's
    docstring for why a silent "video" shouldn't count as one on its own.
    """
    settings = Settings(project_root=tmp_path, instagram_cookies_browser="chrome")
    content_id = "cid-mixed-video"
    out_dir = settings.tmp_dir / content_id

    def fake_run(command, **kwargs):
        if Path(command[0]).stem == "gallery-dl":
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "post_1.mp4").write_bytes(b"fake-real-mp4")
            (out_dir / "post_2.mp4").write_bytes(b"fake-silent-mp4")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == "ffprobe":
            has_audio = "post_1.mp4" in command[-1]
            return subprocess.CompletedProcess(
                command, 0, stdout="0\n" if has_audio else "", stderr=""
            )
        if command[0] == "ffmpeg":
            frame_path = out_dir / "post_2.frame.jpg"
            frame_path.write_bytes(b"fake-frame")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        pytest.fail(f"unexpected subprocess call in this test: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GalleryDlDownloader(settings).download("https://www.instagram.com/p/abc/", content_id)

    assert result.media_type is MediaType.VIDEO
    assert result.media_paths == [str(out_dir / "post_1.mp4")]


def test_gallery_dl_downloader_keeps_all_videos_in_multi_video_carousel(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path, instagram_cookies_browser="chrome")
    content_id = "cid-multi-video"
    out_dir = settings.tmp_dir / content_id

    def fake_run(command, **kwargs):
        if Path(command[0]).stem == "gallery-dl":
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "post_1.mp4").write_bytes(b"fake-mp4-1")
            (out_dir / "post_2.mp4").write_bytes(b"fake-mp4-2")
            (out_dir / "post_3.mp4").write_bytes(b"fake-mp4-3")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout="0\n", stderr="")
        pytest.fail(f"unexpected subprocess call in this test: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GalleryDlDownloader(settings).download("https://www.instagram.com/p/abc/", content_id)

    assert result.media_type is MediaType.VIDEO
    assert result.media_paths == [
        str(out_dir / "post_1.mp4"),
        str(out_dir / "post_2.mp4"),
        str(out_dir / "post_3.mp4"),
    ]


@pytest.mark.parametrize(
    ("img_index", "expected_range"),
    [("7", "7"), ("1", "1"), ("0", "1")],
)
def test_gallery_dl_downloader_passes_range_for_img_index(
    tmp_path, monkeypatch, img_index, expected_range
):
    settings = Settings(project_root=tmp_path, instagram_cookies_browser="chrome")
    content_id = "cid-img-index"
    out_dir = settings.tmp_dir / content_id
    captured_command = []

    def fake_run(command, **kwargs):
        if Path(command[0]).stem == "gallery-dl":
            captured_command.extend(command)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "post_8.mp4").write_bytes(b"fake-mp4")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout="0\n", stderr="")
        pytest.fail(f"unexpected subprocess call in this test: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    GalleryDlDownloader(settings).download(
        f"https://www.instagram.com/p/abc/?img_index={img_index}", content_id
    )

    # img_index and gallery-dl's --range are both 1-based. The last slide of a carousel
    # carries img_index == slide count, so an off-by-one here finds no files at all.
    assert "--range" in captured_command
    assert captured_command[captured_command.index("--range") + 1] == expected_range


def test_gallery_dl_downloader_omits_range_without_img_index(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path, instagram_cookies_browser="chrome")
    content_id = "cid-no-index"
    out_dir = settings.tmp_dir / content_id
    captured_command = []

    def fake_run(command, **kwargs):
        if Path(command[0]).stem == "gallery-dl":
            captured_command.extend(command)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "post_1.mp4").write_bytes(b"fake-mp4")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, stdout="0\n", stderr="")
        pytest.fail(f"unexpected subprocess call in this test: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    GalleryDlDownloader(settings).download("https://www.instagram.com/p/abc/", content_id)

    assert "--range" not in captured_command


def test_gallery_dl_downloader_raises_when_no_media_found(tmp_path, monkeypatch):
    from reel_pipeline.config import Settings as SettingsCls
    from reel_pipeline.downloader import DownloadError

    settings = SettingsCls(project_root=tmp_path, instagram_cookies_browser="chrome")
    content_id = "cid-empty"

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(DownloadError, match="no video or image files"):
        GalleryDlDownloader(settings).download("https://www.instagram.com/p/abc/", content_id)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.facebook.com/watch/?v=123456",
        "https://fb.watch/abc123/",
        "https://www.linkedin.com/posts/someone_activity-123",
    ],
)
def test_dispatches_facebook_and_linkedin_urls_to_yt_dlp(tmp_path, monkeypatch, url):
    settings = Settings(project_root=tmp_path)
    calls = []
    monkeypatch.setattr(
        YtDlpDownloader,
        "download",
        lambda self, url, content_id: (
            calls.append(("yt-dlp", url))
            or DownloadResult(content_id=content_id, media_paths=["x"], platform="facebook")
        ),
    )
    monkeypatch.setattr(
        GalleryDlDownloader,
        "download",
        lambda self, url, content_id: pytest.fail("gallery-dl should not handle Facebook/LinkedIn"),
    )

    DispatchingDownloader(settings).download(url, "cid-fb-li")

    assert calls == [("yt-dlp", url)]


def test_yt_dlp_downloader_omits_cookies_when_not_configured(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)
    captured_opts = _install_fake_yt_dlp(monkeypatch)

    YtDlpDownloader(settings).download("https://www.youtube.com/watch?v=abc", "cid1")

    assert "cookiefile" not in captured_opts[0]
    assert "cookiesfrombrowser" not in captured_opts[0]


def test_yt_dlp_downloader_passes_cookie_file_when_configured(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path, ytdlp_cookies_file="C:/fake/cookies.txt")
    captured_opts = _install_fake_yt_dlp(monkeypatch)

    YtDlpDownloader(settings).download("https://www.facebook.com/watch/?v=1", "cid2")

    assert captured_opts[0]["cookiefile"] == "C:/fake/cookies.txt"
    assert "cookiesfrombrowser" not in captured_opts[0]


def test_yt_dlp_downloader_passes_cookies_from_browser_when_configured(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path, ytdlp_cookies_browser="chrome")
    captured_opts = _install_fake_yt_dlp(monkeypatch)

    YtDlpDownloader(settings).download("https://www.linkedin.com/posts/abc", "cid3")

    assert captured_opts[0]["cookiesfrombrowser"] == ("chrome",)
    assert "cookiefile" not in captured_opts[0]


def test_yt_dlp_downloader_clears_stale_files_from_a_previous_failed_attempt(tmp_path, monkeypatch):
    """Regression test: a retry after a failed download attempt used to write
    into the same out_dir without clearing it first, so a stale partial file
    from the earlier failed attempt could be picked up as if it were this
    attempt's output.
    """
    settings = Settings(project_root=tmp_path)
    out_dir = settings.tmp_dir / "content123"
    out_dir.mkdir(parents=True)
    stale_file = out_dir / "audio.wav"  # wrong extension - simulates a partial leftover
    stale_file.write_bytes(b"stale partial data from a failed attempt")

    def extract_info_impl(url):
        # Simulate yt-dlp producing this attempt's real output file.
        (out_dir / "audio.mp3").write_bytes(b"real audio from this attempt")
        return {"extractor_key": "Fake", "title": "Fake Title", "duration": 12.0}

    _install_fake_yt_dlp(monkeypatch, extract_info_impl=extract_info_impl)

    result = YtDlpDownloader(settings).download("https://www.youtube.com/watch?v=x", "content123")

    assert not stale_file.exists()
    assert result.media_paths == [str(out_dir / "audio.mp3")]


def test_gallery_dl_caption_reads_description_from_metadata_json(tmp_path):
    import json

    from reel_pipeline.downloader import _gallery_dl_caption

    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "a.jpg.json").write_text(json.dumps({"description": "see link"}), encoding="utf-8")
    assert _gallery_dl_caption(sorted(tmp_path.iterdir())) == "see link"
    assert _gallery_dl_caption([tmp_path / "a.jpg"]) is None


_MEDIA_INFO_400 = (
    "[instagram][error] HttpError: '400 Bad Request' for "
    "'https://www.instagram.com/api/v1/media/123/info/'"
)


def _gallery_dl_400(tmp_path, monkeypatch, page_body, stderr=_MEDIA_INFO_400):
    """gallery-dl exits 1 with stderr naming only the status; the API's own message is
    only in the page dump the second, --write-pages run leaves in its working directory."""
    settings = Settings(project_root=tmp_path, instagram_cookies_browser="chrome")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "--write-pages" in command and page_body is not None:
            Path(kwargs["cwd"], "instagram.com_api_v1_media.txt").write_text(
                page_body, encoding="utf-8"
            )
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return GalleryDlDownloader(settings), calls


def test_gallery_dl_raises_post_unavailable_when_instagram_says_the_media_is_gone(
    tmp_path, monkeypatch
):
    downloader, calls = _gallery_dl_400(
        tmp_path, monkeypatch, '{"message":"Media not found or unavailable","status":"fail"}'
    )

    with pytest.raises(PostUnavailableError, match="Media not found or unavailable"):
        downloader.download("https://www.instagram.com/p/gone/", "cid-gone")

    assert len(calls) == 2
    assert "--simulate" in calls[1]


@pytest.mark.parametrize(
    "page_body",
    ['{"message":"checkpoint_required","status":"fail"}', '{"message":"useragent mismatch"}', None],
)
def test_gallery_dl_keeps_other_400s_retryable(tmp_path, monkeypatch, page_body):
    """Instagram answers 400 for account trouble too. Calling those terminal would
    permanently fail every reel over one expired session."""
    downloader, _ = _gallery_dl_400(tmp_path, monkeypatch, page_body)

    with pytest.raises(DownloadError) as excinfo:
        downloader.download("https://www.instagram.com/p/x/", "cid-x")

    assert not isinstance(excinfo.value, PostUnavailableError)


def test_gallery_dl_does_not_probe_on_other_failures(tmp_path, monkeypatch):
    downloader, calls = _gallery_dl_400(
        tmp_path, monkeypatch, None, stderr="[instagram][error] HttpError: '429 Too Many Requests'"
    )

    with pytest.raises(DownloadError):
        downloader.download("https://www.instagram.com/p/x/", "cid-x")

    assert len(calls) == 1


def _raise_yt_dlp(message):
    def impl(url):
        raise sys.modules["yt_dlp.utils"].DownloadError(message)

    return impl


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: Unsupported URL: https://x.com/a/status/1",
        "ERROR: [twitter] 1: No video could be found in this tweet",
        "ERROR: There's no media in this post",
        "ERROR: Nothing to download",
        "ERROR: Requested format is not available",
    ],
)
def test_yt_dlp_says_no_media_raises_no_media_error(tmp_path, monkeypatch, message):
    _install_fake_yt_dlp(monkeypatch, extract_info_impl=_raise_yt_dlp(message))
    downloader = YtDlpDownloader(Settings(project_root=tmp_path))

    with pytest.raises(NoMediaError):
        downloader.download("https://x.com/a/status/1", "cid-no-media")


def test_yt_dlp_network_failure_stays_a_plain_download_error(tmp_path, monkeypatch):
    _install_fake_yt_dlp(monkeypatch, extract_info_impl=_raise_yt_dlp("HTTP Error 500"))
    downloader = YtDlpDownloader(Settings(project_root=tmp_path))

    with pytest.raises(DownloadError) as caught:
        downloader.download("https://x.com/a/status/1", "cid-plain")

    assert not isinstance(caught.value, NoMediaError)


def test_gallery_dl_empty_range_retries_the_whole_post(tmp_path, monkeypatch):
    """Live case Dd3xE-qDS2N: img_index=9 on a post gallery-dl sees fewer items in. The
    range matched nothing, exit 0, and the post looked empty when it wasn't."""
    settings = Settings(project_root=tmp_path, instagram_cookies_browser="chrome")
    out_dir = settings.tmp_dir / "cid-range"
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if "--range" not in command:
            (out_dir / "post_1.jpg").write_bytes(b"fake-jpg")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    url = "https://www.instagram.com/p/Dd3xE-qDS2N/?img_index=9"
    result = GalleryDlDownloader(settings).download(url, "cid-range")

    assert [("--range" in c) for c in commands] == [True, False]
    assert result.media_paths == [str(out_dir / "post_1.jpg")]


def test_gallery_dl_empty_without_range_does_not_retry(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path, instagram_cookies_browser="chrome")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(DownloadError, match="no video or image"):
        GalleryDlDownloader(settings).download("https://www.instagram.com/p/abc/", "cid-empty")

    assert len(commands) == 1


def test_gallery_dl_failure_falls_to_yt_dlp_once(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)
    calls = []

    def gallery(self, url, content_id):
        raise DownloadError("gallery-dl failed: extractor found nothing usable")

    def yt_dlp(self, url, content_id):
        calls.append(url)
        return DownloadResult(content_id=content_id, media_paths=["x"], platform="Instagram")

    monkeypatch.setattr(GalleryDlDownloader, "download", gallery)
    monkeypatch.setattr(YtDlpDownloader, "download", yt_dlp)

    url = "https://www.instagram.com/reel/abc/"
    result = DispatchingDownloader(settings).download(url, "cid-ladder")

    assert calls == [url]
    assert result.platform == "Instagram"


def test_both_downloaders_failing_reports_both(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)

    def gallery(self, url, content_id):
        raise DownloadError("gallery-dl said one thing")

    def yt_dlp(self, url, content_id):
        raise NoMediaError("yt-dlp said another")

    monkeypatch.setattr(GalleryDlDownloader, "download", gallery)
    monkeypatch.setattr(YtDlpDownloader, "download", yt_dlp)

    with pytest.raises(DownloadError) as caught:
        DispatchingDownloader(settings).download("https://www.instagram.com/p/abc/", "cid-both")

    assert "gallery-dl said one thing" in str(caught.value)
    assert "yt-dlp said another" in str(caught.value)


@pytest.mark.parametrize(
    "detail",
    [
        "HTTP 429 from instagram",
        "Rate limit reached, slow down",
        "rate-limited",
        "Too many requests",
        "challenge_required",
        "redirect to /challenge/",
        "checkpoint_required",
        "Account checkpoint required",
        "login required",
        "Redirected to /accounts/login/",
        "please log in",
        "you need to be logged in",
        "sign in to confirm",
        "HTTP 403",
        "HTTP 401",
        "HTTP Error 403: Forbidden",
        "403 Forbidden",
        "Unauthorized",
    ],
)
def test_gallery_dl_refusals_do_not_fall_to_yt_dlp(tmp_path, monkeypatch, detail):
    """Being rate limited or challenged is an answer to respect, not a reason to come
    at the same account again from a second tool."""
    settings = Settings(project_root=tmp_path)

    def gallery(self, url, content_id):
        raise DownloadError(f"gallery-dl failed to download {url!r}: {detail}")

    monkeypatch.setattr(GalleryDlDownloader, "download", gallery)
    monkeypatch.setattr(
        YtDlpDownloader,
        "download",
        lambda self, url, content_id: pytest.fail("yt-dlp must not run after a refusal"),
    )

    with pytest.raises(DownloadError, match="gallery-dl failed"):
        DispatchingDownloader(settings).download("https://www.instagram.com/p/abc/", "cid-r")


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/p/403/",
        "https://www.instagram.com/rate/login/challenge",
        "https://x.com/someone/status/429",
    ],
)
def test_a_url_inside_the_error_is_not_read_as_a_refusal(url):
    error = DownloadError(f"gallery-dl failed to download {url!r}: no extractor")
    assert not is_refusal(error, url)


@pytest.mark.parametrize(
    "detail",
    [
        "ffmpeg: unsupported frame rate 240",
        "low bit rate",
        "Requested format 401 is not available",
        "format 403 has no audio",
        "frame 429 is damaged",
    ],
)
def test_a_media_problem_is_not_read_as_a_refusal(detail):
    url = "https://www.instagram.com/p/abc/"
    assert not is_refusal(DownloadError(f"gallery-dl failed: {detail}"), url)


def test_a_redirect_to_a_login_page_in_the_error_is_still_a_refusal():
    url = "https://www.instagram.com/p/abc/"
    error = DownloadError(f"{url!r} redirected to https://www.instagram.com/accounts/login/")
    assert is_refusal(error, url)


def test_generate_is_not_read_as_rate_limited(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)

    def gallery(self, url, content_id):
        raise DownloadError("gallery-dl could not generate a file name")

    monkeypatch.setattr(GalleryDlDownloader, "download", gallery)
    monkeypatch.setattr(
        YtDlpDownloader,
        "download",
        lambda self, url, content_id: DownloadResult(
            content_id=content_id, media_paths=["x"], platform="Instagram"
        ),
    )

    result = DispatchingDownloader(settings).download("https://www.instagram.com/p/abc/", "cid-gen")

    assert result.platform == "Instagram"


def test_post_gone_does_not_fall_to_yt_dlp(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)

    def gallery(self, url, content_id):
        raise PostUnavailableError("gone")

    monkeypatch.setattr(GalleryDlDownloader, "download", gallery)
    monkeypatch.setattr(
        YtDlpDownloader,
        "download",
        lambda self, url, content_id: pytest.fail("a deleted post has nothing to retry"),
    )

    with pytest.raises(PostUnavailableError):
        DispatchingDownloader(settings).download("https://www.instagram.com/p/abc/", "cid-gone")


def test_gallery_dl_sees_the_wider_file_types(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path, instagram_cookies_browser="chrome")
    out_dir = settings.tmp_dir / "cid-avif"

    def fake_run(command, **kwargs):
        (out_dir / "post_1.avif").write_bytes(b"fake")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GalleryDlDownloader(settings).download("https://www.instagram.com/p/a/", "cid-avif")

    assert result.media_type is MediaType.IMAGE


@pytest.mark.parametrize(
    "url",
    [
        "https://www.loom.com/share/abc",
        "https://www.reddit.com/r/x/comments/1/y",
        "https://v.redd.it/abc",
        "https://soundcloud.com/a/b",
        "https://www.twitch.tv/videos/1",
        "https://rumble.com/v1-a.html",
        "https://www.dailymotion.com/video/x1",
        "https://www.ted.com/talks/a",
        "https://streamable.com/a",
        "https://fast.wistia.com/medias/a",
        "https://fast.wistia.net/embed/iframe/a",
    ],
)
def test_yt_dlp_cookies_never_reach_an_anonymous_host(tmp_path, monkeypatch, url):
    shipped = yaml.safe_load(Path("config/settings.yaml").read_text(encoding="utf-8"))
    settings = Settings(
        project_root=tmp_path,
        ytdlp_cookies_file="C:/fake/cookies.txt",
        download=DownloadConfig(anonymous_domains=shipped["download"]["anonymous_domains"]),
    )
    captured_opts = _install_fake_yt_dlp(monkeypatch)

    YtDlpDownloader(settings).download(url, "cid-anon")

    assert "cookiefile" not in captured_opts[0]
    assert "cookiesfrombrowser" not in captured_opts[0]
