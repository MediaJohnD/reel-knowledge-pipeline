from __future__ import annotations

import subprocess
import sys
import types

import pytest

from reel_pipeline.config import Settings
from reel_pipeline.downloader import DispatchingDownloader, GalleryDlDownloader, YtDlpDownloader
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


def test_gallery_dl_downloader_detects_video_over_images(tmp_path, monkeypatch):
    from reel_pipeline.config import Settings as SettingsCls

    settings = SettingsCls(project_root=tmp_path, instagram_cookies_browser="chrome")
    content_id = "cid-video"
    out_dir = settings.tmp_dir / content_id

    def fake_run(command, **kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "post_1.jpg").write_bytes(b"fake-jpg")
        (out_dir / "post.mp4").write_bytes(b"fake-mp4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

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


def test_gallery_dl_downloader_keeps_all_videos_in_multi_video_carousel(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path, instagram_cookies_browser="chrome")
    content_id = "cid-multi-video"
    out_dir = settings.tmp_dir / content_id

    def fake_run(command, **kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "post_1.mp4").write_bytes(b"fake-mp4-1")
        (out_dir / "post_2.mp4").write_bytes(b"fake-mp4-2")
        (out_dir / "post_3.mp4").write_bytes(b"fake-mp4-3")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GalleryDlDownloader(settings).download("https://www.instagram.com/p/abc/", content_id)

    assert result.media_type is MediaType.VIDEO
    assert result.media_paths == [
        str(out_dir / "post_1.mp4"),
        str(out_dir / "post_2.mp4"),
        str(out_dir / "post_3.mp4"),
    ]


def test_gallery_dl_downloader_passes_range_for_img_index(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path, instagram_cookies_browser="chrome")
    content_id = "cid-img-index"
    out_dir = settings.tmp_dir / content_id
    captured_command = []

    def fake_run(command, **kwargs):
        captured_command.extend(command)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "post_8.mp4").write_bytes(b"fake-mp4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    GalleryDlDownloader(settings).download(
        "https://www.instagram.com/p/abc/?img_index=7", content_id
    )

    # img_index is 0-based, gallery-dl's --range is 1-based.
    assert "--range" in captured_command
    assert captured_command[captured_command.index("--range") + 1] == "8"


def test_gallery_dl_downloader_omits_range_without_img_index(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path, instagram_cookies_browser="chrome")
    content_id = "cid-no-index"
    out_dir = settings.tmp_dir / content_id
    captured_command = []

    def fake_run(command, **kwargs):
        captured_command.extend(command)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "post_1.mp4").write_bytes(b"fake-mp4")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

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
