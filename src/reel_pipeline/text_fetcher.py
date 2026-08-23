"""Text-content ingestion for non-media knowledge sources (GitHub, and any
other URL classify_url_kind() routes here - see its docstring in
validators.py for the catch-all policy).

Produces the same TranscriptResult shape transcriber.py and image_describer.py
already produce, so downstream enrichment and note-writing are identical
regardless of source - the worker picks this instead of Downloader based on
StateRecord.content_kind (set at registration time by
validators.classify_url_kind()).

Public links only - no OAuth or API keys. GitHub's public REST API needs none
for public repos; every other page is fetched as a plain HTTP GET request
against the URL as submitted (no JS execution - not browser automation).
"""

from __future__ import annotations

import asyncio
import base64
import re
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol
from urllib.parse import urlparse

import httpx
import trafilatura

from reel_pipeline.config import Settings
from reel_pipeline.models import TranscriptResult

_GITHUB_API = "https://api.github.com"

_MARKDOWN_LINK_RE = re.compile(r'(!?)\[([^\]]*)\]\(([^)\s]+)((?:\s+"[^"]*")?)\)')


def _absolutize_relative_links(text: str, owner: str, repo: str, ref: str) -> str:
    """Rewrites a README's relative markdown links/images to absolute GitHub
    URLs, using the repo's own default branch as the ref.

    A GitHub README's relative links (`[Contributing](CONTRIBUTING.md)`,
    `![badge](docs/badge.svg)`) point at sibling files *in that repo*, not at
    anything in the Obsidian vault. Left as-is, embedding the raw README text
    verbatim in a vault note makes Obsidian treat them as vault-internal
    wikitargets and silently create blank stub notes for paths like
    `CONTRIBUTING.md` at the vault root (observed in practice - see the
    2026-07-19 vault cleanup). Absolute URLs keep the links genuinely useful
    (they still resolve, just to GitHub instead of nowhere) without that
    side effect.
    """

    def _rewrite(match: re.Match[str]) -> str:
        bang, label, target, title = match.groups()
        if target.startswith(("http://", "https://", "#", "mailto:")):
            return match.group(0)
        clean_target = target.removeprefix("./").removeprefix("/")
        if bang:
            absolute = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{clean_target}"
        else:
            absolute = f"https://github.com/{owner}/{repo}/blob/{ref}/{clean_target}"
        return f"{bang}[{label}]({absolute}{title})"

    return _MARKDOWN_LINK_RE.sub(_rewrite, text)


class TextFetchError(RuntimeError):
    """Raised when fetching or extracting text content fails."""


def _parse_github_path(url: str) -> tuple[str, str, str | None, str | None]:
    """Returns (owner, repo, ref, file_path). ref and file_path are both None for
    a repo-root URL. For a "blob/<ref>/<path>" URL, ref is the single path segment
    right after "blob/" - this doesn't disambiguate refs that themselves contain
    slashes (e.g. "feature/foo"), which GitHub's own URL scheme is ambiguous
    about without an extra API call; the common single-segment branch/tag case
    is handled correctly.
    """
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2:
        raise TextFetchError(f"could not parse a GitHub owner/repo from {url!r}")
    owner, repo = parts[0], parts[1]
    if len(parts) > 3 and parts[2] == "blob":
        ref = parts[3]
        file_path = "/".join(parts[4:])
        return owner, repo, ref, file_path or None
    return owner, repo, None, None


class GitHubFetcher:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client

    def fetch(self, url: str, content_id: str) -> TranscriptResult:
        owner, repo, ref, file_path = _parse_github_path(url)
        owned_client = self._client or httpx.Client(timeout=30.0)
        owns_client = self._client is None
        try:
            metadata = self._get_json(owned_client, f"{_GITHUB_API}/repos/{owner}/{repo}")
            if file_path:
                contents_url = f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{file_path}"
                if ref:
                    contents_url += f"?ref={ref}"
                content = self._get_file_content(owned_client, contents_url)
                heading = f"# {owner}/{repo} - {file_path}"
            else:
                content = self._get_file_content(
                    owned_client, f"{_GITHUB_API}/repos/{owner}/{repo}/readme"
                )
                heading = f"# {owner}/{repo}"
        finally:
            if owns_client:
                owned_client.close()

        default_branch = metadata.get("default_branch") or "main"
        content = _absolutize_relative_links(content, owner, repo, default_branch)

        parts = [
            heading,
            metadata.get("description") or "",
            f"Stars: {metadata.get('stargazers_count', 0)} | "
            f"Language: {metadata.get('language') or 'unknown'} | "
            f"Topics: {', '.join(metadata.get('topics') or [])}",
            "",
            content,
        ]
        text = "\n".join(p for p in parts if p is not None).strip()

        return TranscriptResult(
            content_id=content_id,
            text=text,
            content_kind="text",
            language=None,
            backend="github-api",
            duration_seconds=None,
        )

    def _get_json(self, client: httpx.Client, url: str) -> dict:
        try:
            response = client.get(url, headers={"Accept": "application/vnd.github+json"})
        except httpx.HTTPError as exc:
            raise TextFetchError(f"GitHub API request to {url!r} failed: {exc}") from exc
        if response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0":
            reset = response.headers.get("x-ratelimit-reset", "unknown")
            raise TextFetchError(
                f"GitHub API rate limit exceeded fetching {url!r}; resets at epoch {reset}"
            )
        if response.status_code == 404:
            raise TextFetchError(f"GitHub returned 404 for {url!r} - private or nonexistent")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TextFetchError(f"GitHub API request to {url!r} failed: {exc}") from exc
        return response.json()

    def _get_file_content(self, client: httpx.Client, url: str) -> str:
        data = self._get_json(client, url)
        encoded = data.get("content", "")
        if data.get("encoding") == "base64" and encoded:
            return base64.b64decode(encoded).decode("utf-8", errors="replace")
        return encoded


_NOTION_API_BASE = "https://app.notion.com/api/v3"
# Matches a bare 32-hex-char run or an already-dashed UUID anywhere in a
# Notion URL's path - every notion.so/notion.site/notion.com share link ends
# in "...Some-Title-<id>" in one of these two forms.
_NOTION_PAGE_ID_RE = re.compile(
    r"([0-9a-f]{8})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{12})",
    re.IGNORECASE,
)
# Rich-text "mention" placeholder characters (inline page link, inline date,
# etc.) - the mention's real content isn't in the title array's text slot at
# all, so these render as noise if kept; dropped rather than resolved, same
# "good enough for LLM summarization" bar as the rest of this extraction.
_NOTION_MENTION_PLACEHOLDERS = ("‣", "⁍")  # "‣", "⁍"
_NOTION_COLLECTION_BLOCK_TYPES = ("collection_view", "collection_view_page")
# These block types store a filename (not prose) in "properties.title" -
# e.g. an image block's title is its filename ("123.jpg"), not a caption.
# Skipped as noise rather than included verbatim.
_NOTION_FILENAME_TITLE_BLOCK_TYPES = ("image", "file", "video", "audio", "pdf")


def _notion_page_id_from_url(url: str) -> str | None:
    match = _NOTION_PAGE_ID_RE.search(url)
    if not match:
        return None
    return "-".join(match.groups()).lower()


def _notion_rich_text_to_plain(rich_text: object) -> str:
    """Flattens a Notion "rich text" property value - a list of
    [text, optional_formatting] pairs - into plain text, dropping mention
    placeholder characters (see _NOTION_MENTION_PLACEHOLDERS).
    """
    if not isinstance(rich_text, list):
        return ""
    parts: list[str] = []
    for item in rich_text:
        if not item:
            continue
        text = item[0]
        if isinstance(text, str) and text not in _NOTION_MENTION_PLACEHOLDERS:
            parts.append(text)
    return "".join(parts)


def _unwrap_notion_block(node: object) -> dict | None:
    """Notion's API doubly-nests block values as {"value": {"value": {...}}}
    for some blocks (an API change made after the fact - see
    https://github.com/NotionX/react-notion-x/issues/682); generically
    descend through however many "value" layers are present.
    """
    while isinstance(node, dict) and "value" in node:
        node = node["value"]
    if not isinstance(node, dict) or "id" not in node:
        return None
    return node


class NotionFetcher:
    """Fetches a public Notion page's text via Notion's own internal JSON API
    (the same API calls Notion's web client makes) instead of a browser -
    plain HTTP POST + JSON parsing, no JS execution, no rendering, no
    credentials. Endpoint/payload/response shapes confirmed by live testing
    against real public and nonexistent pages (see
    docs/superpowers/specs/2026-08-10-notion-api-text-capture-design.md).

    This is unofficial/undocumented and could change or break without
    notice - accepted tradeoff, same spirit as GitHubFetcher using GitHub's
    official API where one exists, but Notion has no public read API for
    pages the requester doesn't own.

    v1 known limitations (not solved now, documented rather than silently
    wrong):
    - No pagination: only the first 100-block chunk is fetched, so very
      large pages may be missing tail content.
    - Linked databases (collection_view/collection_view_page blocks) are
      noted by title but their rows aren't fetched - that needs a separate
      collection-query API call this version doesn't make.
    - Bare custom-subdomain root URLs with no id in the path at all (e.g.
      "https://someworkspace.notion.site/" linking to that workspace's home
      page rather than one specific page) aren't resolvable - only URLs with
      an embedded page id work. Every real-world share link observed during
      this feature's development had one; a link to a site's bare root does
      not, and needs a domain-to-page-id resolution step this version
      doesn't implement.
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client

    def fetch(self, url: str, content_id: str) -> TranscriptResult:
        page_id = _notion_page_id_from_url(url)
        if page_id is None:
            raise TextFetchError(
                f"could not find a Notion page id in {url!r} - a bare "
                "custom-subdomain root link (no id in the path) isn't "
                "supported; share the specific page link instead"
            )

        owned_client = self._client or httpx.Client(timeout=30.0)
        owns_client = self._client is None
        try:
            response = self._load_page_chunk(owned_client, page_id)
        finally:
            if owns_client:
                owned_client.close()

        blocks: dict = response.get("recordMap", {}).get("block", {})
        root = _unwrap_notion_block(blocks.get(page_id))
        if root is None:
            raise TextFetchError(
                f"Notion page {url!r} not found or not public - it may be "
                "private, deleted, or require login"
            )

        title = _notion_rich_text_to_plain(root.get("properties", {}).get("title"))
        lines = [f"# {title}" if title else "# (untitled Notion page)"]
        self._walk_block_children(root, blocks, lines, visited={page_id})
        text = "\n".join(line for line in lines if line).strip()

        return TranscriptResult(
            content_id=content_id,
            text=text,
            content_kind="text",
            language=None,
            backend="notion-api",
            duration_seconds=None,
        )

    def _load_page_chunk(self, client: httpx.Client, page_id: str) -> dict:
        body = {
            "pageId": page_id,
            "limit": 100,
            "chunkNumber": 0,
            "cursor": {"stack": []},
            "verticalColumns": False,
        }
        try:
            response = client.post(
                f"{_NOTION_API_BASE}/loadPageChunk",
                json=body,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise TextFetchError(f"Notion API request failed for {page_id!r}: {exc}") from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TextFetchError(f"Notion API request failed for {page_id!r}: {exc}") from exc
        return response.json()

    def _walk_block_children(
        self, block: dict, blocks: dict, lines: list[str], visited: set[str]
    ) -> None:
        for child_id in block.get("content") or []:
            if child_id in visited:
                continue  # defensive: guards against a cyclic content reference
            visited.add(child_id)
            child = _unwrap_notion_block(blocks.get(child_id))
            if child is None:
                continue  # not in this chunk - the v1 pagination gap above

            child_title = _notion_rich_text_to_plain(child.get("properties", {}).get("title"))
            if child_title and child.get("type") not in _NOTION_FILENAME_TITLE_BLOCK_TYPES:
                lines.append(child_title)
            if child.get("type") in _NOTION_COLLECTION_BLOCK_TYPES:
                # Linked database - row data needs a separate collection-query
                # API call this version doesn't make (see class docstring).
                lines.append(
                    f"[linked database: {child_title or 'untitled'} - contents not included]"
                )

            self._walk_block_children(child, blocks, lines, visited)


_DRIVE_FILE_ID_RE = re.compile(r"(?:/file/d/|[?&]id=)([\w-]{6,})")


def _drive_file_id_from_url(url: str) -> str | None:
    match = _DRIVE_FILE_ID_RE.search(url)
    return match.group(1) if match else None


class DriveFetcher:
    """Fetches a publicly-shared Google Drive file (not a video - see
    validators._classify_drive_url_kind()) via Drive's unauthenticated
    direct-download endpoint and treats the bytes as text if they decode as
    plain text/markdown, or as HTML (trafilatura-extracted) if they look
    like an HTML export. No OAuth, no API key, no browser - a plain GET,
    same trust profile as GenericHtmlFetcher.

    v1 known limitation, documented rather than silently wrong: binary
    document formats (PDF, PPTX, DOCX, ...) aren't parsed - they fail with a
    clear "binary format not supported" error rather than garbage text or a
    silent empty note. Also not handled: Drive's "can't scan this file for
    viruses" interstitial page that appears for files too large for that
    scan (roughly >25MB) instead of the raw bytes - out of scope for the
    "handful of shared items" volume this project is built for.
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client

    def fetch(self, url: str, content_id: str) -> TranscriptResult:
        file_id = _drive_file_id_from_url(url)
        if file_id is None:
            raise TextFetchError(f"could not find a Drive file id in {url!r}")

        owned_client = self._client or httpx.Client(timeout=30.0, follow_redirects=True)
        owns_client = self._client is None
        try:
            try:
                response = owned_client.get(
                    f"https://drive.google.com/uc?export=download&id={file_id}"
                )
            except httpx.HTTPError as exc:
                raise TextFetchError(f"failed to fetch Drive file {url!r}: {exc}") from exc
        finally:
            if owns_client:
                owned_client.close()

        if response.status_code != 200:
            raise TextFetchError(f"Drive file {url!r} returned HTTP {response.status_code}")

        try:
            text = response.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TextFetchError(
                f"Drive file {url!r} is a binary format (PDF/PPTX/DOCX/etc.) - "
                "not supported yet, this pipeline only reads plain text/markdown/HTML "
                "Drive files"
            ) from exc

        stripped = text.strip()
        if stripped.lower().startswith(("<!doctype html", "<html")):
            extracted = (trafilatura.extract(text) or "").strip()
            if not extracted:
                raise TextFetchError(f"Drive file {url!r} produced no usable text")
            text = extracted
        else:
            text = stripped

        return TranscriptResult(
            content_id=content_id,
            text=text,
            content_kind="text",
            language=None,
            backend="drive-download",
            duration_seconds=None,
        )


_APP_SHELL_MARKERS = (
    "javascript must be enabled",
    "please enable javascript",
)
# Below this length, extracted text is more likely to be app-shell boilerplate
# (a stray noscript/meta fragment) than real page content - real pages,
# even short ones, run well past this once trafilatura strips markup.
_MIN_PLAUSIBLE_EXTRACTED_LENGTH = 60
# Checked against the *rendered* page only (post-JS) - a plain-GET app shell
# never gets this far. Generic enough to catch Notion's/most SPAs' own login
# wall without hardcoding one product's copy.
_LOGIN_WALL_MARKERS = (
    "log in to continue",
    "sign in to continue",
    "log in to view this page",
    "please log in",
    "please sign in",
)
# A Cloudflare-style (or similar) bot-detection challenge page - observed live
# against roadmap.notion.site, whose challenge text ("Verification
# successful. Waiting for roadmap.notion.site to respond / Enable JavaScript
# and cookies to continue") is long enough to clear the app-shell length
# check and phrased generically enough to slip past the login-wall markers,
# so it would otherwise be captured and written to the vault as if it were
# real page content. Per this fallback's explicit no-anti-detection stance
# (see RenderedHtmlFetcher's docstring), a challenge means the site is
# actively saying no to headless access - respected, not bypassed.
_BOT_CHALLENGE_MARKERS = (
    "checking your browser",
    "just a moment",
    "verification successful",
    "enable javascript and cookies to continue",
    "attention required",
)
# A real challenge/login-wall page IS this message, at the very top - a long
# legitimate article happening to contain an incidental phrase like "just a
# moment" or "please log in" somewhere in its body shouldn't be misfired on.
# Scanning only the opening slice keeps the true-positive case (found live
# against roadmap.notion.site) while cutting that false-positive risk (found
# by review, 2026-08-10).
_CHALLENGE_CHECK_WINDOW = 600


def _looks_like_app_shell(extracted: str) -> bool:
    too_short = len(extracted) < _MIN_PLAUSIBLE_EXTRACTED_LENGTH
    has_app_shell_marker = any(marker in extracted.lower() for marker in _APP_SHELL_MARKERS)
    return not extracted or too_short or has_app_shell_marker


def _raise_if_challenge_or_login_wall(extracted: str, url: str) -> None:
    """Shared by GenericHtmlFetcher and RenderedHtmlFetcher - a bot-detection
    challenge or login wall can arrive via either path (e.g. a challenge page
    served as a plain HTTP 200 that plain-GET already sees fine), not just
    the rendered one (found by review, 2026-08-10 - the original version only
    checked this on the rendered path).
    """
    prefix = extracted[:_CHALLENGE_CHECK_WINDOW].lower()
    if any(marker in prefix for marker in _BOT_CHALLENGE_MARKERS):
        raise TextFetchError(
            f"page {url!r} is behind a bot-detection challenge (e.g. "
            "Cloudflare) that headless Chromium didn't pass - this pipeline "
            "doesn't attempt to bypass bot detection (no fingerprint "
            "spoofing/anti-detection tooling), per project policy"
        )
    if any(marker in prefix for marker in _LOGIN_WALL_MARKERS):
        raise TextFetchError(
            f"page {url!r} requires login - no credentials are used by this pipeline"
        )


_RENDER_POLICY_LOCK = threading.Lock()


@contextmanager
def _subprocess_capable_event_loop_policy() -> Iterator[None]:
    """Scope a subprocess-capable asyncio policy to a Playwright call.

    Playwright launches its driver with asyncio.create_subprocess_exec, which
    Windows' SelectorEventLoop cannot do - it raises a bare, message-less
    NotImplementedError. `serve-webhook` installs the selector policy
    process-wide on purpose (cli.py, to silence ProactorEventLoop disconnect
    spam), which left the render fallback dead under the webhook server while
    it kept working fine under `run-once` - the asymmetry that made this look
    like a page-specific failure for a week.

    Swapping the policy here rather than dropping it there keeps that
    deliberate spam fix intact. It has to be the *policy*: Playwright builds
    its loop with asyncio.new_event_loop(), which reads the policy and ignores
    asyncio.set_event_loop(), so a thread-local loop doesn't help.

    # ponytail: a process-wide swap held for the whole browser session and
    # serialized by a lock - fine at "handful of pages per run" scale. If
    # renders ever need to run concurrently, move Playwright into its own
    # process instead of widening this window.
    """
    proactor_policy = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if sys.platform != "win32" or proactor_policy is None:
        yield
        return
    with _RENDER_POLICY_LOCK:
        previous = asyncio.get_event_loop_policy()
        if isinstance(previous, proactor_policy):
            yield
            return
        asyncio.set_event_loop_policy(proactor_policy())
        try:
            yield
        finally:
            asyncio.set_event_loop_policy(previous)


class RenderedHtmlFetcher:
    """Read-only headless-Chromium fallback for GenericHtmlFetcher, used only
    when the plain-GET path already produced an app shell (see
    GenericHtmlFetcher.fetch). Loads the URL, waits for it to settle, and
    extracts text from the *rendered* DOM the same way GenericHtmlFetcher
    extracts from raw HTML.

    Deliberately narrow, per
    docs/superpowers/specs/2026-08-10-js-rendered-text-capture-design.md:
    - No fingerprint spoofing/anti-detection - stock headless Chromium,
      default automation-flagged user agent. A site that blocks headless
      browsers is respected, not worked around.
    - No credentials - no cookies, no login, no session reuse from the
      yt-dlp/gallery-dl cookie config. A login-gated page still fails.
    - No interaction - navigate, wait, read. No clicking, scrolling, or
      filling anything.
    - One browser instance launched and closed per fetch call - no
      persistent process, no shared state between items.
      # ponytail: per-call launch is O(items needing fallback) browser
      # startups; fine at "handful of pages per run" scale, upgrade to a
      # shared browser context if that stops being true.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    def fetch(self, url: str, content_id: str) -> TranscriptResult:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright

        timeout_ms = self.settings.text_capture.render_fallback.timeout_seconds * 1000
        try:
            with _subprocess_capable_event_loop_policy(), sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch()
                except PlaywrightError as exc:
                    if "Executable doesn't exist" in str(exc):
                        raise TextFetchError(
                            "Chromium not installed for the render fallback - run "
                            "`uv run playwright install chromium`"
                        ) from exc
                    raise TextFetchError(
                        f"failed to launch headless Chromium for {url!r}: {exc}"
                    ) from exc
                try:
                    page = browser.new_page()
                    # "networkidle" is unreliable for real SPAs (Notion never
                    # goes network-idle - background websockets/polling keep
                    # firing - confirmed live: it hung to the full timeout on
                    # a page that actually loaded in ~3s). "domcontentloaded"
                    # plus a short bounded settle wait for post-load JS to
                    # finish rendering is what real testing showed works.
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    # Bounded by timeout_ms too - a small timeout_seconds
                    # config shouldn't be able to add an unbounded extra 3s
                    # on top of the navigation timeout it's supposed to cap.
                    page.wait_for_timeout(min(3000, timeout_ms))
                    html = page.content()
                finally:
                    browser.close()
        except PlaywrightTimeoutError as exc:
            raise TextFetchError(
                f"rendering {url!r} exceeded "
                f"{self.settings.text_capture.render_fallback.timeout_seconds}s - "
                "the page may be slow or may never finish loading"
            ) from exc
        except NotImplementedError as exc:
            # asyncio raises this bare and message-less when the running loop
            # can't spawn subprocesses, so without translating it the failure
            # reaches needs-attention.txt as literally "NotImplementedError()"
            # and the real traceback only exists in the log (2026-08-23).
            raise TextFetchError(
                f"failed to render {url!r}: this process's asyncio event loop "
                "cannot spawn the browser driver subprocess. That is an "
                "environment problem, not a problem with the page - the render "
                "fallback needs a subprocess-capable event loop policy."
            ) from exc
        except PlaywrightError as exc:
            raise TextFetchError(f"failed to render {url!r}: {exc}") from exc

        extracted = (trafilatura.extract(html) or "").strip()
        _raise_if_challenge_or_login_wall(extracted, url)
        if _looks_like_app_shell(extracted):
            raise TextFetchError(
                f"page {url!r} produced no usable text even after rendering - "
                "the page's JS itself may have failed, or content genuinely "
                "didn't load in time"
            )

        return TranscriptResult(
            content_id=content_id,
            text=extracted,
            content_kind="text",
            language=None,
            backend="playwright-render",
            duration_seconds=None,
        )


class GenericHtmlFetcher:
    """Plain HTTP GET + trafilatura text extraction for any public page
    classify_url_kind() routed here as "text" (Notion share links, Google
    Docs' /mobilebasic public view, Airtable share links, blog posts, ...).

    Client-rendered app shells with no server-side content (common on Notion,
    possible on the others) fall back to RenderedHtmlFetcher (a read-only
    headless-Chromium render) rather than failing outright, unless
    text_capture.render_fallback.enabled is false.
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client
        self._rendered = RenderedHtmlFetcher(settings)

    def fetch(self, url: str, content_id: str) -> TranscriptResult:
        owned_client = self._client or httpx.Client(timeout=30.0, follow_redirects=True)
        owns_client = self._client is None
        try:
            try:
                response = owned_client.get(url)
            except httpx.HTTPError as exc:
                raise TextFetchError(f"failed to fetch page {url!r}: {exc}") from exc
            if response.status_code != 200:
                raise TextFetchError(f"page {url!r} returned HTTP {response.status_code}")
            html = response.text
        finally:
            if owns_client:
                owned_client.close()

        extracted = (trafilatura.extract(html) or "").strip()
        # A challenge/login-wall page can arrive as a plain HTTP 200 the
        # plain-GET path already sees fine - checked before the app-shell
        # escalation decision below, not only on the rendered path (found by
        # review, 2026-08-10).
        _raise_if_challenge_or_login_wall(extracted, url)
        # Many modern pages (Notion, Docs, Airtable) are a client-rendered app
        # shell with no real content in the server-sent HTML at all -
        # trafilatura then extracts whatever stray text IS server-rendered
        # (e.g. a <noscript> fallback message), which is non-empty but not
        # the page's actual content. The empty-string check alone doesn't
        # catch this; matching known app-shell boilerplate and a minimum
        # plausible length does.
        if _looks_like_app_shell(extracted):
            if not self.settings.text_capture.render_fallback.enabled:
                raise TextFetchError(
                    f"page {url!r} produced no usable text - it's rendered "
                    "client-side with no real content in the server HTML (or requires "
                    "login), rather than being genuinely empty. The browser-render "
                    "fallback is disabled (text_capture.render_fallback.enabled: false)."
                )
            return self._rendered.fetch(url, content_id)

        return TranscriptResult(
            content_id=content_id,
            text=extracted,
            content_kind="text",
            language=None,
            backend="html-fetch",
            duration_seconds=None,
        )


class TextFetcher(Protocol):
    def fetch(self, url: str, content_id: str) -> TranscriptResult: ...


_GITHUB_DOMAINS = ("github.com",)
_NOTION_DOMAINS = ("notion.so", "notion.site", "notion.com")
_DRIVE_DOMAINS = ("drive.google.com",)


class DispatchingTextFetcher:
    """Routes each URL to the platform-appropriate concrete fetcher.

    GitHub, Notion, and Drive each get their own API/download-backed fetcher
    (structured data via an actual API or direct-download endpoint instead of
    scraping rendered/raw HTML); every other host classify_url_kind() has
    already routed here as "text" goes through the generic HTML fetcher -
    there's no second domain gate to keep in sync with settings (a prior
    version of this class had one, which was the root cause of
    docs.google.com/airtable.com/findarepo.com/thefounderos.com being
    config-allowed but still rejected as "unrecognized text-capture domain";
    fixed 2026-08-09, then made moot by 2026-08-10's catch-all text-capture
    policy in CLAUDE.md).
    """

    def __init__(self, settings: Settings):
        self._github = GitHubFetcher(settings)
        self._notion = NotionFetcher(settings)
        self._drive = DriveFetcher(settings)
        self._generic_html = GenericHtmlFetcher(settings)

    def fetch(self, url: str, content_id: str) -> TranscriptResult:
        host = urlparse(url).netloc.split(":")[0].lower()
        if host.startswith("www."):
            host = host[len("www.") :]
        if any(host == d or host.endswith(f".{d}") for d in _GITHUB_DOMAINS):
            return self._github.fetch(url, content_id)
        if any(host == d or host.endswith(f".{d}") for d in _NOTION_DOMAINS):
            return self._notion.fetch(url, content_id)
        if any(host == d or host.endswith(f".{d}") for d in _DRIVE_DOMAINS):
            return self._drive.fetch(url, content_id)
        return self._generic_html.fetch(url, content_id)


def get_text_fetcher(settings: Settings) -> TextFetcher:
    return DispatchingTextFetcher(settings)
