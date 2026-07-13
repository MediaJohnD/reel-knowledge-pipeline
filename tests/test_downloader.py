from __future__ import annotations

import subprocess

import pytest

from reel_pipeline.config import Settings
from reel_pipeline.downloader import DispatchingDownloader, GalleryDlDownloader, YtDlpDownloader
from reel_pipeline.models import DownloadResult, MediaType


def test_dispatches_instagram_urls_to_gallery_dl(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)
    calls = []
    monkeypatch.setattr(
        GalleryDlDownloader,
        "download",
        lambda self, url, content_id: calls.append(("gallery-dl", url)) or DownloadResult(
            content_id=content_id, media_paths=["x"], platform="instagram"
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
        lambda self, url, content_id: calls.append(("yt-dlp", url)) or DownloadResult(
            content_id=content_id, media_paths=["x"], platform="youtube"
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

    result = GalleryDlDownloader(settings).download(
        "https://www.instagram.com/p/abc/", content_id
    )

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
