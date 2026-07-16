"""Media download module.

Dispatches by platform: Instagram goes through gallery-dl (better-maintained
Instagram extractor than yt-dlp - see the research note in the Obsidian vault
under 20-Resources/Tools/), everything else (YouTube, Facebook, LinkedIn,
TikTok, X/Twitter, Vimeo) through yt-dlp. URL-level guardrails (blocked
domains, allow-list) are enforced upstream in validators.validate_url /
queue_manager - this module assumes it is only ever called with a URL that
already passed validation, but still surfaces downloader failures clearly
rather than swallowing them.

Instagram requires an authenticated session to download reliably; see
Settings.require_instagram_cookies() and docs/runbook.md for how to configure
REEL_INSTAGRAM_COOKIES_FILE / REEL_INSTAGRAM_COOKIES_BROWSER. Facebook
generally requires the same, via the separate, optional
REEL_YTDLP_COOKIES_FILE / REEL_YTDLP_COOKIES_BROWSER (see
Settings.optional_ytdlp_cookies()) - optional because public YouTube already
works anonymously.

LinkedIn is a narrower case than "generally works with cookies": yt-dlp
2026.07.04 (the pinned version) removed LinkedIn login support entirely
(upstream commit a5e0f87, issue #17039 - "Remove broken login support"), so
REEL_YTDLP_COOKIES_FILE/_BROWSER do nothing for LinkedIn regardless of
config. Separately, yt-dlp's LinkedInIE only recognizes
`linkedin.com/posts/...-<id>-<hash>` and
`linkedin.com/feed/update/urn:li:activity:<id>` URLs (confirmed against its
source) - `urn:li:groupPost:...` URLs (LinkedIn Group posts) aren't matched
by any LinkedIn-specific pattern at all and fall through to yt-dlp's generic
webpage extractor, which 404s. A DownloadError on a LinkedIn URL is very
likely one of these two verified gaps, not a config problem - check whether
the URL is a group post (`urn:li:groupPost:`) before assuming cookies would
help.

(2026-07-13 currency check: yt-dlp 2026.07.04 reworked its Instagram extractor
and added cookie-invalidation detection; gallery-dl 1.32.6 shipped no
Instagram-specific changes in the same window. Neither changes the gallery-dl-
for-Instagram recommendation above - yt-dlp here only ever handles bestaudio
for non-Instagram URLs.)
"""

from __future__ import annotations

import subprocess
from typing import Protocol
from urllib.parse import parse_qs, urlparse

from reel_pipeline.config import Settings
from reel_pipeline.models import DownloadResult, MediaType

_INSTAGRAM_DOMAINS = ("instagram.com",)


class Downloader(Protocol):
    def download(self, url: str, content_id: str) -> DownloadResult: ...


class DownloadError(RuntimeError):
    """Raised when a downloader backend fails to produce a usable media file."""


class YtDlpDownloader:
    """Downloads best-effort audio for a URL into settings.tmp_dir/<content_id>/."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def download(self, url: str, content_id: str) -> DownloadResult:
        # Imported lazily: keeps unit tests fast and yt-dlp's untyped internals out of
        # the module's static-analysis surface until this code path actually runs.
        import yt_dlp  # type: ignore[import-untyped]
        from yt_dlp.utils import DownloadError as YtDlpDownloadError

        out_dir = self.settings.tmp_dir / content_id
        out_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(out_dir / "audio.%(ext)s")

        ydl_opts: dict = {
            "format": "bestaudio/best",
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }

        # Optional: public YouTube works anonymously, but Facebook generally requires a
        # logged-in session the same way Instagram does. LinkedIn is the exception - see
        # this module's docstring: yt-dlp 2026.07.04 dropped LinkedIn login support, so
        # this option is inert there regardless of config. If configured, yt-dlp uses it
        # for every domain it handles; if not, yt-dlp
        # attempts anonymous access and raises its own clear "log in required" error
        # for content that needs it.
        cookies = self.settings.optional_ytdlp_cookies()
        if cookies is not None:
            cookie_kind, cookie_value = cookies
            if cookie_kind == "file":
                ydl_opts["cookiefile"] = cookie_value
            else:
                ydl_opts["cookiesfrombrowser"] = (cookie_value,)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # pyright: ignore[reportArgumentType]
                info = ydl.extract_info(url, download=True)
        except YtDlpDownloadError as exc:
            raise DownloadError(f"yt-dlp failed to download {url!r}: {exc}") from exc

        media_path = out_dir / "audio.mp3"
        if not media_path.exists():
            candidates = sorted(out_dir.glob("audio.*"))
            if not candidates:
                raise DownloadError(f"yt-dlp produced no output file for {url!r}")
            media_path = candidates[0]

        return DownloadResult(
            content_id=content_id,
            media_type=MediaType.VIDEO,
            media_paths=[str(media_path)],
            platform=(info or {}).get("extractor_key", "unknown"),
            source_title=(info or {}).get("title"),
            duration_seconds=(info or {}).get("duration"),
        )


_VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv")
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


def _parse_img_index(url: str) -> int | None:
    """Instagram URLs that deep-link into one item of a carousel carry a 0-based
    "?img_index=N" query param - e.g. a URL pointing at the 8th of 9 clips. Returns
    None if absent or unparseable, meaning "the whole post, no specific item".
    """
    values = parse_qs(urlparse(url).query).get("img_index")
    if not values:
        return None
    try:
        return int(values[0])
    except ValueError:
        return None


class GalleryDlDownloader:
    """Downloads Instagram media via the gallery-dl CLI, using a cookies file or
    browser cookie jar from the account owner's own logged-in session (never a
    hardcoded credential - see Settings.require_instagram_cookies()).
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def download(self, url: str, content_id: str) -> DownloadResult:
        cookie_kind, cookie_value = self.settings.require_instagram_cookies()

        out_dir = self.settings.tmp_dir / content_id
        out_dir.mkdir(parents=True, exist_ok=True)

        command = ["gallery-dl", "--dest", str(out_dir), "--quiet"]
        if cookie_kind == "file":
            command += ["--cookies", cookie_value]
        else:
            command += ["--cookies-from-browser", cookie_value]

        # gallery-dl's --range is 1-based; combined with its own
        # extractor.instagram.order-files=asc default (post-display order), this
        # addresses the same item a "?img_index=N" URL was deep-linking to, instead
        # of downloading (and then silently discarding) the whole carousel.
        img_index = _parse_img_index(url)
        if img_index is not None:
            command += ["--range", str(img_index + 1)]

        command.append(url)

        try:
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell, url already validated upstream
                command, capture_output=True, text=True, timeout=300
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DownloadError(f"gallery-dl failed to run for {url!r}: {exc}") from exc

        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise DownloadError(f"gallery-dl failed to download {url!r}: {detail}")

        all_files = sorted(p for p in out_dir.rglob("*") if p.is_file())
        videos = [p for p in all_files if p.suffix.lower() in _VIDEO_SUFFIXES]
        if videos:
            # Keep every downloaded video, not just the first - a multi-video carousel
            # (no img_index, or a range that still matched several files) used to have
            # all but videos[0] silently discarded here with no error or log entry.
            # worker.py transcribes each and combines them, mirroring how a multi-image
            # carousel already gets combined into one description.
            return DownloadResult(
                content_id=content_id,
                media_type=MediaType.VIDEO,
                media_paths=[str(p) for p in videos],
                platform="instagram",
            )

        images = [p for p in all_files if p.suffix.lower() in _IMAGE_SUFFIXES]
        if images:
            # A photo post or multi-image carousel, in gallery-dl's download order.
            return DownloadResult(
                content_id=content_id,
                media_type=MediaType.IMAGE,
                media_paths=[str(p) for p in images],
                platform="instagram",
            )

        raise DownloadError(f"gallery-dl produced no video or image files for {url!r}")


def _registrable_domain(netloc: str) -> str:
    host = netloc.split(":")[0].lower()
    return host[4:] if host.startswith("www.") else host


class DispatchingDownloader:
    """Routes each URL to the platform-appropriate concrete downloader."""

    def __init__(self, settings: Settings):
        self._yt_dlp = YtDlpDownloader(settings)
        self._gallery_dl = GalleryDlDownloader(settings)

    def download(self, url: str, content_id: str) -> DownloadResult:
        domain = _registrable_domain(urlparse(url).netloc)
        if any(domain == d or domain.endswith(f".{d}") for d in _INSTAGRAM_DOMAINS):
            return self._gallery_dl.download(url, content_id)
        return self._yt_dlp.download(url, content_id)


def get_downloader(settings: Settings) -> Downloader:
    return DispatchingDownloader(settings)
