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

A link can turn out to be media after all: a redirect onto an allow-listed host, a
direct audio/video/image file, a thin page around a video, an HLS stream, or a
scanned PDF. Those raise MediaFound and the worker carries on from there.
"""

from __future__ import annotations

import asyncio
import base64
import html as html_lib
import itertools
import logging
import re
import shutil
import socket
import socketserver
import sys
import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import httpcore
import httpx
import trafilatura

from reel_pipeline.config import Settings
from reel_pipeline.document_text import (
    binary_text,
    classify_response,
    document_suffix,
    feed_text,
    pdf_images,
    pretty_json,
    strip_xml_tags,
    url_suffix,
)
from reel_pipeline.logging_setup import get_logger, log_context
from reel_pipeline.models import (
    AUDIO_SUFFIXES,
    IMAGE_SUFFIXES,
    VIDEO_SUFFIXES,
    DownloadResult,
    MediaType,
    TranscriptResult,
)
from reel_pipeline.validators import (
    classify_url_kind,
    is_non_public_ip,
    is_private_or_local_host,
    validate_url,
)

logger = get_logger(__name__)

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


def _refuse_private_target(request: httpx.Request) -> None:
    """httpx request hook, so it runs on every redirect hop, not just the first
    URL: a public page must not be able to redirect a fetch onto this machine or
    its network (validators.classify_url_kind only ever sees the submitted URL).
    """
    if is_private_or_local_host(request.url.host):
        raise TextFetchError(f"refusing to fetch private or local address {request.url.host!r}")


def _checked_public_addresses(host: str, port: int) -> list[str]:
    """Resolves once. Callers connect to these addresses, never to the name, so an
    answer that changes after this lookup (DNS rebinding) can't reach this machine.
    """
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if any(is_non_public_ip(address) for address in addresses):
        raise TextFetchError(f"refusing to fetch private or local address {host!r}")
    return addresses


class _PublicOnlyBackend(httpcore.SyncBackend):
    """Resolves the name once, checks every address, and connects to a checked IP.

    The request hook above resolves on its own, so a DNS answer that changes between
    that lookup and the connection (rebinding) would otherwise reach this machine.
    TLS is unaffected: httpcore takes SNI and the certificate name from the URL.
    """

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addresses = _checked_public_addresses(host, port)
        except (OSError, UnicodeError) as exc:
            raise httpcore.ConnectError(f"could not resolve {host!r}: {exc}") from exc
        last_error = httpcore.ConnectError(f"no addresses for {host!r}")
        for address in addresses:
            try:
                return super().connect_tcp(address, port, timeout, local_address, socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise last_error


class _PublicOnlyTransport(httpx.HTTPTransport):
    def __init__(self) -> None:
        super().__init__()
        # HTTPTransport has no parameter for this. Replacing the private pool is
        # covered by test_guarded_client_refuses_dns_rebinding_to_loopback, which
        # fails against a real loopback server if a new httpx stops honoring it.
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(), network_backend=_PublicOnlyBackend()
        )


def _guarded_client() -> httpx.Client:
    # Passing a transport also makes httpx ignore HTTP_PROXY and friends, which is
    # wanted here: a proxy would resolve the name itself, past the check.
    return httpx.Client(
        timeout=30.0,
        follow_redirects=True,
        transport=_PublicOnlyTransport(),
        event_hooks={"request": [_refuse_private_target]},
    )


class TextFetchError(RuntimeError):
    """Raised when fetching or extracting text content fails."""


class ArchiveEligibleError(TextFetchError):
    """The site failed (a 5xx, or it couldn't be reached), rather than saying no.

    Worth a Wayback lookup, but only once the retries are used up: a 5xx is often
    gone by the next attempt. The worker decides, on the last attempt.
    """


class _PageRefused(TextFetchError):
    """A challenge or login wall. The site said no, so no fallback may follow it."""


class MediaFound(Exception):  # noqa: N818 - a signal to the worker, not a failure
    """This link is media. The worker continues with one of these:

    - `result`: already downloaded here (a direct file, an HLS stream, a scanned PDF).
    - `url`: an allow-listed page for the normal downloader.

    `context` is text to keep beside the transcript, as a post's caption is.
    """

    def __init__(
        self,
        *,
        result: DownloadResult | None = None,
        url: str | None = None,
        context: str = "",
    ):
        super().__init__(url or "media already downloaded")
        self.result = result
        self.url = url
        self.context = context


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
_NOTION_MAX_CHUNKS = 10


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

    A long page arrives in chunks of 100 blocks. The response's `cursor` is passed
    back to get the next one (shape confirmed against a live public page,
    2026-11-01), up to _NOTION_MAX_CHUNKS. If that cap is hit or a later chunk fails,
    the note keeps what was read and says so.

    Known limitations (not solved now, documented rather than silently wrong):
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
            blocks, chunks_read, complete = self._load_all_chunks(owned_client, page_id)
        finally:
            if owns_client:
                owned_client.close()

        root = _unwrap_notion_block(blocks.get(page_id))
        if root is None:
            raise TextFetchError(
                f"Notion page {url!r} not found or not public - it may be "
                "private, deleted, or require login"
            )

        title = _notion_rich_text_to_plain(root.get("properties", {}).get("title"))
        lines = [f"# {title}" if title else "# (untitled Notion page)"]
        self._walk_block_children(root, blocks, lines, visited={page_id})
        if not complete:
            lines.append(f"[Only the first {chunks_read} chunks of this page were read]")
        text = "\n".join(line for line in lines if line).strip()

        return TranscriptResult(
            content_id=content_id,
            text=text,
            content_kind="text",
            language=None,
            backend="notion-api",
            duration_seconds=None,
        )

    def _load_all_chunks(self, client: httpx.Client, page_id: str) -> tuple[dict, int, bool]:
        """Returns (blocks, chunks read, whether that was the whole page)."""
        blocks: dict = {}
        cursor: dict = {"stack": []}
        for chunk_number in range(_NOTION_MAX_CHUNKS):
            try:
                response = self._load_page_chunk(client, page_id, chunk_number, cursor)
            except (TextFetchError, ValueError) as exc:
                if chunk_number == 0:
                    raise
                log_context(
                    logger,
                    logging.WARNING,
                    "later Notion chunk failed, keeping what was read",
                    page_id=page_id,
                    error=str(exc),
                )
                return blocks, chunk_number, False
            blocks.update(response.get("recordMap", {}).get("block", {}))
            cursor = response.get("cursor") or {}
            if not cursor.get("stack"):
                return blocks, chunk_number + 1, True
        return blocks, _NOTION_MAX_CHUNKS, False

    def _load_page_chunk(
        self, client: httpx.Client, page_id: str, chunk_number: int, cursor: dict
    ) -> dict:
        body = {
            "pageId": page_id,
            "limit": 100,
            "chunkNumber": chunk_number,
            "cursor": cursor,
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
                continue  # past what was read, if the chunk cap or a failure ended it early

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
# Every share-link shape seen so far: /drive/folders/<id>, /drive/mobile/folders/<id>,
# /drive/u/0/folders/<id>, and the older folderview?id=<id>.
_DRIVE_FOLDER_ID_RE = re.compile(r"(?:/folders/|/folderview\?(?:[^#]*&)?id=)([\w-]{6,})")
_DRIVE_FOLDER_ENTRY_RE = re.compile(
    r'<div class="flip-entry" id="entry-[\w-]+".*?<a href="([^"]+)".*?'
    r'<div class="flip-entry-title">(.*?)</div>',
    re.DOTALL,
)
_DRIVE_VIRUS_SCAN_MARKERS = ("virus scan warning", "too large for google to scan")
# A shared folder is a handful of files by intent; this only stops a huge one
# from pulling hundreds of downloads into a single note.
_DRIVE_FOLDER_MAX_FILES = 25


def _drive_file_id_from_url(url: str) -> str | None:
    match = _DRIVE_FILE_ID_RE.search(url)
    return match.group(1) if match else None


def _drive_folder_id_from_url(url: str) -> str | None:
    match = _DRIVE_FOLDER_ID_RE.search(url)
    return match.group(1) if match else None


class DriveFetcher:
    """Fetches a publicly-shared Google Drive file (not a video - see
    validators._classify_drive_url_kind()) via Drive's unauthenticated
    direct-download endpoint and treats the bytes as text if they decode as
    plain text/markdown, or as HTML (trafilatura-extracted) if they look
    like an HTML export. No OAuth, no API key, no browser - a plain GET,
    same trust profile as GenericHtmlFetcher.

    Binary files (PDF, PPTX, DOCX, ...) are saved into the vault's
    `attachments/` folder and embedded in the note, with best-effort text
    extraction (document_text.binary_text). Drive's "can't scan this file for
    viruses" interstitial, served instead of the raw bytes for files too large for
    that scan (roughly >25MB), carries a form whose hidden fields are the real
    download link. That one follow-up GET is made if the form points at Google and
    names this file; otherwise, or if the interstitial comes back, it raises rather
    than becoming note content.

    A public folder URL lists its top level through Drive's own
    `embeddedfolderview` page (same plain GET) and reads each file into one
    combined note. A file that fails is noted and skipped; subfolders and native
    Docs/Sheets are linked by name, not followed.
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client

    def fetch(self, url: str, content_id: str) -> TranscriptResult:
        folder_id = _drive_folder_id_from_url(url)
        file_id = _drive_file_id_from_url(url)
        if folder_id is None and file_id is None:
            raise TextFetchError(f"could not find a Drive file id in {url!r}")

        owned_client = self._client or _guarded_client()
        owns_client = self._client is None
        try:
            if folder_id is not None:
                return self._fetch_folder(owned_client, url, folder_id, content_id)
            return self._fetch_file(owned_client, url, file_id or "", content_id)
        finally:
            if owns_client:
                owned_client.close()

    def _fetch_folder(
        self, client: httpx.Client, url: str, folder_id: str, content_id: str
    ) -> TranscriptResult:
        try:
            response = client.get(f"https://drive.google.com/embeddedfolderview?id={folder_id}")
        except httpx.HTTPError as exc:
            raise TextFetchError(f"failed to list Drive folder {url!r}: {exc}") from exc
        if response.status_code != 200:
            raise TextFetchError(f"Drive folder {url!r} returned HTTP {response.status_code}")

        entries = [
            (href, html_lib.unescape(title).strip())
            for href, title in _DRIVE_FOLDER_ENTRY_RE.findall(response.text)
        ]
        files = [
            (file_id, title)
            for href, title in entries
            if "/file/d/" in href and (file_id := _drive_file_id_from_url(href))
        ]
        if not files:
            raise TextFetchError(
                f"Drive folder {url!r} shows no files (private, empty, or only subfolders)"
            )

        sections: list[str] = []
        fetched = 0
        for file_id, title in files[:_DRIVE_FOLDER_MAX_FILES]:
            file_url = f"https://drive.google.com/file/d/{file_id}/view"
            try:
                result = self._fetch_file(client, file_url, file_id, content_id)
            except TextFetchError as exc:
                sections.append(f"## {title}\n\n(Could not read this file: {exc})")
                continue
            except MediaFound as found:
                # A scanned PDF. Its embed is still worth a section, but one combined
                # note can't also become a set of images to describe.
                shutil.rmtree(self.settings.tmp_dir / content_id, ignore_errors=True)
                sections.append(f"## {title}\n\n{found.context}")
                fetched += 1
                continue
            sections.append(f"## {title}\n\n{result.text}")
            fetched += 1
        if fetched == 0:
            raise TextFetchError(f"none of the {len(files)} files in Drive folder {url!r} read")

        for href, title in entries:
            if "/file/d/" not in href:
                sections.append(f"## {title}\n\n(Not read, open it in Drive: {href})")
        if len(files) > _DRIVE_FOLDER_MAX_FILES:
            sections.append(
                f"(Only the first {_DRIVE_FOLDER_MAX_FILES} of {len(files)} files were read.)"
            )

        return TranscriptResult(
            content_id=content_id,
            text="\n\n".join(sections),
            content_kind="text",
            language=None,
            backend="drive-folder",
            duration_seconds=None,
        )

    def _fetch_file(
        self, client: httpx.Client, url: str, file_id: str, content_id: str
    ) -> TranscriptResult:
        limit_mb = self.settings.text_capture.max_document_mb
        download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        try:
            fetched = _fetch_capped(client, download_url, limit_mb)
            if fetched.status_code == 200 and _is_drive_virus_scan_page(fetched.content):
                fetched = self._follow_virus_scan_form(client, url, file_id, fetched)
        except httpx.HTTPError as exc:
            raise TextFetchError(f"failed to fetch Drive file {url!r}: {exc}") from exc

        if fetched.status_code != 200:
            raise TextFetchError(f"Drive file {url!r} returned HTTP {fetched.status_code}")

        try:
            text = fetched.content.decode("utf-8")
        except UnicodeDecodeError:
            return attach_to_vault(
                self.settings,
                fetched.content,
                _disposition_name(fetched.headers),
                content_id,
                "drive-attachment",
                fetched.headers.get("content-type", ""),
                default_stem=file_id,
            )

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

    def _follow_virus_scan_form(
        self, client: httpx.Client, url: str, file_id: str, interstitial: _Fetched
    ) -> _Fetched:
        too_large = TextFetchError(f"Drive file {url!r} is too large to download without a click")
        confirm_url = _drive_confirm_url(interstitial.text, file_id)
        if confirm_url is None:
            raise too_large
        fetched = _fetch_capped(client, confirm_url, self.settings.text_capture.max_document_mb)
        if fetched.status_code == 200 and _is_drive_virus_scan_page(fetched.content):
            raise too_large
        return fetched


def _is_drive_virus_scan_page(content: bytes) -> bool:
    head = content[:65536].decode("utf-8", "replace").strip().lower()
    return head.startswith(("<!doctype html", "<html")) and any(
        marker in head for marker in _DRIVE_VIRUS_SCAN_MARKERS
    )


def _drive_confirm_url(markup: str, file_id: str) -> str | None:
    """The real download link from the interstitial's form, or None if the form isn't
    the one expected: it must point at Google over https and name this same file.
    """
    form = _DRIVE_FORM_RE.search(markup)
    if form is None:
        return None
    action = _tag_attrs(form[1]).get("action", "")
    parts = urlsplit(action)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "google.com" or host.endswith(".google.com")):
        return None
    fields: list[tuple[str, str]] = []
    for tag in _INPUT_TAG_RE.findall(form[0]):
        attributes = _tag_attrs(tag)
        if attributes.get("name"):
            fields.append((attributes["name"], attributes.get("value", "")))
    values = dict(fields)
    if values.get("id") != file_id or "confirm" not in values:
        return None
    return f"{action}?{urlencode(fields)}"


# Bytes per read when streaming a response.
_STREAM_CHUNK_BYTES = 65536
_TAG_BODY = r"""(?:[^>"']|"[^"]*"|'[^']*')*"""
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")
_DRIVE_FORM_RE = re.compile(rf"(<form\b{_TAG_BODY}>).*?</form>", re.DOTALL | re.IGNORECASE)
_INPUT_TAG_RE = re.compile(rf"<input\b{_TAG_BODY}>", re.IGNORECASE)


def _tag_attrs(tag: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for name, raw in _ATTR_RE.findall(tag):
        value = raw[1:-1] if raw[:1] in ('"', "'") else raw
        attributes.setdefault(name.lower(), html_lib.unescape(value))
    return attributes


def archive_reason(exc: Exception) -> str:
    """How a failed fetch reads in an archived note's label."""
    status = re.search(r"HTTP (\d{3})", str(exc))
    return f"returned HTTP {status[1]}" if status else "could not be reached"


def _open_stream(client: httpx.Client, url: str) -> httpx.Response:
    """GET without reading the body, so size can be checked as it arrives. The caller
    closes it. A 403 gets one retry as the descriptive bot UA, and nothing beyond that.
    """
    request = client.build_request("GET", url, headers={"User-Agent": _BROWSER_UA})
    response = client.send(request, stream=True)
    if response.status_code == 403:
        response.close()
        request = client.build_request("GET", url, headers={"User-Agent": _BOT_UA})
        response = client.send(request, stream=True)
    return response


def _archived_chunks(response: httpx.Response, url: str) -> Iterator[bytes]:
    """An `id_` snapshot replays the original page's Content-Encoding over bytes that may
    not be compressed, so httpx's decoder can't be trusted here. Go by the bytes instead.
    """
    raw = response.iter_raw(_STREAM_CHUNK_BYTES)
    first = next(raw, b"")
    if not first.startswith(b"\x1f\x8b"):
        yield first
        yield from raw
        return
    gunzip = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        for chunk in itertools.chain([first], raw):
            if data := gunzip.decompress(chunk):
                yield data
    except zlib.error as exc:
        raise TextFetchError(f"archived copy {url!r} is corrupt: {exc}") from exc


def _too_large(url: str, limit_mb: int) -> TextFetchError:
    return TextFetchError(f"{url!r} is larger than the {limit_mb} MB limit")


def _raise_if_declared_too_large(response: httpx.Response, url: str, limit_mb: int) -> None:
    length = response.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit_mb * 1024 * 1024:
        raise _too_large(url, limit_mb)


def _read_capped(
    response: httpx.Response, head: bytes, chunks: Iterator[bytes], url: str, limit_mb: int
) -> bytes:
    _raise_if_declared_too_large(response, url, limit_mb)
    limit = limit_mb * 1024 * 1024
    buffer = bytearray()
    for chunk in itertools.chain([head], chunks):
        buffer += chunk
        if len(buffer) > limit:
            raise _too_large(url, limit_mb)
    return bytes(buffer)


def _save_stream(
    response: httpx.Response,
    head: bytes,
    chunks: Iterator[bytes],
    target: Path,
    url: str,
    limit_mb: int,
) -> None:
    _raise_if_declared_too_large(response, url, limit_mb)
    limit = limit_mb * 1024 * 1024
    written = 0
    try:
        with target.open("wb") as sink:
            for chunk in itertools.chain([head], chunks):
                written += len(chunk)
                if written > limit:
                    raise _too_large(url, limit_mb)
                sink.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


@dataclass
class _Fetched:
    status_code: int
    headers: httpx.Headers
    content: bytes
    url: str
    encoding: str

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding, "replace")


def _fetch_capped(
    client: httpx.Client, url: str, limit_mb: int, *, browser: bool = False
) -> _Fetched:
    """A whole response in memory, refused past the cap. Only a 200's body is read."""
    if browser:
        response = _open_stream(client, url)
    else:
        response = client.send(client.build_request("GET", url), stream=True)
    try:
        content = b""
        if response.status_code == 200:
            chunks = response.iter_bytes(_STREAM_CHUNK_BYTES)
            content = _read_capped(response, next(chunks, b""), chunks, url, limit_mb)
        return _Fetched(
            response.status_code,
            response.headers,
            content,
            str(response.url),
            response.encoding or "utf-8",
        )
    finally:
        response.close()


def _disposition_name(headers: httpx.Headers) -> str:
    value = headers.get("content-disposition", "")
    encoded = re.search(r"filename\*\s*=\s*([^;]+)", value, re.IGNORECASE)
    if encoded:
        return unquote(encoded[1].strip().strip('"').split("'", 2)[-1]).strip()
    plain = re.search(r'filename\s*=\s*("[^"]*"|[^;]+)', value, re.IGNORECASE)
    return plain[1].strip().strip('"').strip() if plain else ""


_UNSAFE_NAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\[\]#^]')
_WINDOWS_RESERVED_RE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])", re.IGNORECASE)
_MAX_NAME_CHARS = 120


def _safe_name(name: str) -> str:
    """A filename a server suggested, made safe for the vault: no directories, nothing
    Windows refuses, and nothing that would break the `![[name]]` embed.
    """
    base = re.split(r"[/\\]", name)[-1]
    cleaned = _UNSAFE_NAME_CHARS_RE.sub("", base).strip(" .")
    stem, suffix = Path(cleaned).stem, Path(cleaned).suffix
    if _WINDOWS_RESERVED_RE.fullmatch(stem):
        cleaned = f"file-{cleaned}"
        stem = f"file-{stem}"
    if len(cleaned) > _MAX_NAME_CHARS:
        cleaned = stem[: _MAX_NAME_CHARS - len(suffix)] + suffix
    return cleaned


def attach_to_vault(
    settings: Settings,
    data: bytes,
    name: str,
    content_id: str,
    backend: str,
    content_type: str = "",
    *,
    default_stem: str | None = None,
) -> TranscriptResult:
    """Saves a binary file into the vault's attachments folder and embeds it, so
    Obsidian renders the original, with what text can be extracted for enrichment.

    A PDF with no text but with page images is a scan: it raises MediaFound so the
    images get described, with the embed carried along as the caption.
    """
    safe = _safe_name(name) or default_stem or content_id
    if not Path(safe).suffix:
        safe += document_suffix(data[:8], content_type) or ".bin"
    dest = settings.vault_dir / "attachments" / safe
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.read_bytes() != data:
        # A different file already holds this name; an earlier note embeds it.
        safe = f"{content_id}-{safe}"
        dest = dest.with_name(safe)
    dest.write_bytes(data)

    extracted = binary_text(data, safe)
    if not extracted and Path(safe).suffix.lower() == ".pdf":
        images = pdf_images(data)
        if images:
            out_dir = settings.tmp_dir / content_id
            shutil.rmtree(out_dir, ignore_errors=True)
            out_dir.mkdir(parents=True)
            paths: list[str] = []
            for filename, body in images:
                target = out_dir / filename
                target.write_bytes(body)
                paths.append(str(target))
            raise MediaFound(
                result=DownloadResult(
                    content_id=content_id,
                    media_type=MediaType.IMAGE,
                    media_paths=paths,
                    platform="direct",
                ),
                context=f"![[{safe}]]",
            )

    text = f"![[{safe}]]\n\n" + (extracted or f"(Attached file {safe}; no text extracted.)")
    return TranscriptResult(
        content_id=content_id,
        text=text,
        content_kind="text",
        language=None,
        backend=backend,
        duration_seconds=None,
    )


_APP_SHELL_MARKERS = (
    "javascript must be enabled",
    "please enable javascript",
    "javascript isn't enabled",  # Google Docs/Sheets/Slides
)
# A shared Docs/Slides editor URL serves only a JS app shell; the document's
# own public plain-text export endpoint serves the content (2026-09-28).
# httpx's default "python-httpx/x" UA gets a flat 403 from Skool and some
# Cloudflare-fronted blogs that serve a browser UA fine (2026-10-05).
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
# Wikimedia 403s any generic UA from a non-browser client (browser and httpx
# alike) and wants a descriptive bot UA instead (2026-10-08); tried on a 403.
_BOT_UA = "reel-pipeline/0.1 (personal knowledge tool; +https://github.com/MediaJohnD/reel-knowledge-pipeline)"
_GOOGLE_DOC_RE = re.compile(
    r"https?://docs\.google\.com/(document|presentation|spreadsheets)/(?:u/\d+/)?d/([\w-]+)"
)
# Below this length, extracted text is more likely to be app-shell boilerplate
# (a stray noscript/meta fragment) than real page content - real pages,
# even short ones, run well past this once trafilatura strips markup.
_MIN_PLAUSIBLE_EXTRACTED_LENGTH = 60
# A page's own failure message is not its content. mozilla.org's "A required part of
# this site couldn't load" became a note (2026-11-01).
_THIN_ERROR_PAGE_CHARS = 400
_ERROR_PAGE_MARKERS = (
    "couldn't load",
    "could not be loaded",
    "could not be displayed",
    "failed to load",
    "something went wrong",
    "application error",
    "an error occurred",
    "an error has occurred",
    "page not found",
    "browser is not supported",
    "unsupported browser",
)
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
    "blocked by network security",
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
    lowered = extracted.lower().replace("\u2019", "'")
    has_app_shell_marker = any(marker in lowered for marker in _APP_SHELL_MARKERS)
    # Only a thin page: an article can say "something went wrong" and mean it.
    is_error_page = len(extracted) < _THIN_ERROR_PAGE_CHARS and any(
        marker in lowered for marker in _ERROR_PAGE_MARKERS
    )
    return not extracted or too_short or has_app_shell_marker or is_error_page


def _raise_if_challenge_or_login_wall(extracted: str, url: str) -> None:
    """Shared by GenericHtmlFetcher and RenderedHtmlFetcher - a bot-detection
    challenge or login wall can arrive via either path (e.g. a challenge page
    served as a plain HTTP 200 that plain-GET already sees fine), not just
    the rendered one (found by review, 2026-08-10 - the original version only
    checked this on the rendered path).
    """
    prefix = extracted[:_CHALLENGE_CHECK_WINDOW].lower()
    if any(marker in prefix for marker in _BOT_CHALLENGE_MARKERS):
        raise _PageRefused(
            f"page {url!r} is behind a bot-detection challenge (e.g. "
            "Cloudflare) that headless Chromium didn't pass - this pipeline "
            "doesn't attempt to bypass bot detection (no fingerprint "
            "spoofing/anti-detection tooling), per project policy"
        )
    if any(marker in prefix for marker in _LOGIN_WALL_MARKERS):
        raise _PageRefused(
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

    # A process-wide swap held for the whole browser session and
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


# WebRTC doesn't use the proxy: without this a page's STUN packets went straight to
# 127.0.0.2 in a real Chromium test.
_CHROMIUM_NO_DIRECT_WEBRTC = "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"
_PROXY_HOP_HEADERS = (b"connection:", b"proxy-connection:", b"proxy-authorization:")
_PROXY_MAX_HEAD_LINES = 200
_PROXY_CONNECT_TIMEOUT_SECONDS = 15


class _PublicOnlyProxyHandler(socketserver.StreamRequestHandler):
    """One browser connection: CONNECT (https, wss, ws) or an absolute-URL http request."""

    server: _PublicOnlyProxy

    def handle(self) -> None:
        head = self._read_head()
        if not head:
            return
        parts = head[0].split()
        if len(parts) != 3:
            self._reply(b"400 Bad Request")
            return
        method, target, version = parts
        tunnel = method == b"CONNECT"
        try:
            text = target.decode("ascii")
            parsed = urlsplit(f"//{text}" if tunnel else text)
            host, port = parsed.hostname, parsed.port or (443 if tunnel else 80)
        except (UnicodeError, ValueError):
            self._reply(b"400 Bad Request")
            return
        if not host or not (tunnel or parsed.scheme == "http"):
            self._reply(b"400 Bad Request")
            return

        try:
            upstream = self._connect(host, port)
        except TextFetchError:
            self.server.refused.add(host)
            self._reply(b"403 Forbidden")
            return
        except (OSError, UnicodeError):
            self._reply(b"502 Bad Gateway")
            return

        with upstream:
            if tunnel:
                self._reply(b"200 Connection established")
            else:
                origin_form = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                request_line = b" ".join((method, origin_form.encode("ascii"), version))
                kept = [
                    line for line in head[1:] if not line.lower().startswith(_PROXY_HOP_HEADERS)
                ]
                # Without this the connection could stay open for the browser's next
                # request, maybe for another host, to the address checked for this one.
                upstream.sendall(
                    b"\r\n".join([request_line, *kept, b"Connection: close", b"", b""])
                )
            self._relay(upstream)

    def _read_head(self) -> list[bytes]:
        lines: list[bytes] = []
        for _ in range(_PROXY_MAX_HEAD_LINES):
            line = self.rfile.readline(65537).rstrip(b"\r\n")
            if not line:
                return lines
            lines.append(line)
        return []

    def _reply(self, status: bytes) -> None:
        self.wfile.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\n\r\n")

    def _connect(self, host: str, port: int) -> socket.socket:
        last_error = OSError(f"no addresses for {host!r}")
        for address in _checked_public_addresses(host, port):
            try:
                upstream = socket.create_connection(
                    (address, port), timeout=_PROXY_CONNECT_TIMEOUT_SECONDS
                )
                upstream.settimeout(None)
                return upstream
            except OSError as exc:
                last_error = exc
        raise last_error

    def _relay(self, upstream: socket.socket) -> None:
        def _downstream() -> None:
            try:
                while data := upstream.recv(65536):
                    self.connection.sendall(data)
            except OSError:
                pass
            finally:
                _shutdown(self.connection)

        thread = threading.Thread(target=_downstream, daemon=True)
        thread.start()
        try:
            # rfile, not the socket: it may already hold bytes read past the head.
            while data := self.rfile.read1(65536):
                upstream.sendall(data)
        except OSError:
            pass
        finally:
            _shutdown(upstream)
        thread.join()


def _shutdown(connection: socket.socket) -> None:
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _PublicOnlyProxy(socketserver.ThreadingTCPServer):
    """Local forward proxy for the headless browser, so names are resolved here, by
    the same check as _PublicOnlyBackend, instead of by Chromium's own resolver. The
    route handler can only look a name up separately, and never sees WebSockets.
    Certificates are still verified by Chromium: https is an opaque CONNECT tunnel.
    """

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _PublicOnlyProxyHandler)
        self.refused: set[str] = set()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def handle_error(self, request, client_address) -> None:
        # The browser resetting a connection is routine, and it sees the failure itself.
        pass


@contextmanager
def _running_public_only_proxy() -> Iterator[_PublicOnlyProxy]:
    with _PublicOnlyProxy() as proxy:
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            yield proxy
        finally:
            proxy.shutdown()
            thread.join()


def _raise_if_refused_by_proxy(url: str, proxy: _PublicOnlyProxy) -> None:
    # A refused http page renders as an empty 403, which would otherwise be reported
    # as "no usable text", and a refused tunnel only as ERR_TUNNEL_CONNECTION_FAILED.
    host = urlparse(url).hostname
    if host in proxy.refused:
        raise TextFetchError(f"refusing to fetch private or local address {host!r}")


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
    - Every connection goes through _PublicOnlyProxy, so the browser can't
      reach this machine or its network, even by DNS rebinding. WebRTC is
      limited to the proxy too, which leaves it no way out.
    - One browser instance launched and closed per fetch call - no
      persistent process, no shared state between items.
      # Per-call launch is O(items needing fallback) browser
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
            with (
                _running_public_only_proxy() as proxy,
                _subprocess_capable_event_loop_policy(),
                sync_playwright() as playwright,
            ):
                try:
                    # Chromium skips a proxy for loopback unless told otherwise.
                    # Playwright adds this rule itself, but an environment
                    # variable can turn that off, so it is set explicitly.
                    browser = playwright.chromium.launch(
                        proxy={"server": proxy.url, "bypass": "<-loopback>"},
                        args=[_CHROMIUM_NO_DIRECT_WEBRTC],
                    )
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

                    private_hosts: dict[str, bool] = {}

                    def _block_private_requests(route) -> None:
                        # Same rule as _refuse_private_target, for redirects, script
                        # navigation, and subresources inside the browser. data: and
                        # blob: URLs have no host and go nowhere. Rebinding is caught
                        # by the proxy, which also sees the WebSockets this can't.
                        host = urlparse(route.request.url).hostname or ""
                        if host and host not in private_hosts:
                            private_hosts[host] = is_private_or_local_host(host)
                        if private_hosts.get(host):
                            route.abort()
                        else:
                            route.continue_()

                    page.route("**/*", _block_private_requests)
                    # "networkidle" is unreliable for real SPAs (Notion never
                    # goes network-idle - background websockets/polling keep
                    # firing - confirmed live: it hung to the full timeout on
                    # a page that actually loaded in ~3s). "domcontentloaded"
                    # plus a short bounded settle wait for post-load JS to
                    # finish rendering is what real testing showed works.
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    except PlaywrightError:
                        _raise_if_refused_by_proxy(url, proxy)
                        raise
                    _raise_if_refused_by_proxy(page.url, proxy)
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


# Below this much extracted text, a page may just be a frame around a video.
_THIN_PAGE_CHARS = 400
_LOGIN_PATH_SEGMENTS = ("login", "signin", "sign-in", "auth", "oauth", "sso")
_META_TAG_RE = re.compile(rf"<meta\b{_TAG_BODY}>", re.IGNORECASE)
_MEDIA_TAG_RE = re.compile(rf"<(video|source|audio|iframe)\b{_TAG_BODY}>", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_EMBED_META_NAMES = ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream")
_DESCRIPTION_META_NAMES = ("og:description", "description", "twitter:description")
_M3U8_ABSOLUTE_RE = re.compile(r"""https?://[^\s"'<>\\]+?\.m3u8(?:\?[^\s"'<>\\]*)?""")
_M3U8_RELATIVE_RE = re.compile(r"""["'](/?[^"'\s<>\\]+?\.m3u8(?:\?[^"'\s<>\\]*)?)["']""")
_MAX_M3U8_CANDIDATES = 3
_METADATA_LABEL = "[Only the page's title and description were available]"
# Query parameter names that mark a link as private, so it never goes to the archive.
# "auth" but not "author"; "sig" and "key" only as whole words, so not "monkey" or "design".
_PRIVATE_PARAM_RE = re.compile(
    r"token(?!i[sz])|secret|signature|password|passwd|passcode|pwd|credential|jwt|hmac|auth(?!or)"
    r"|apikey|accesskey|privatekey|resourcekey|sessionid"
    r"|(?:^|[^a-z0-9])(?:sig|key|session)(?:$|[^a-z])"
)
_CAMEL_CASE_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WAYBACK_LOOKUP = "https://archive.org/wayback/available"


def _looks_private(url: str) -> bool:
    """A login in the link, or a parameter name above in its query or its fragment, where
    OAuth redirects and single-page apps put theirs."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        return True
    for text in (parts.query, parts.fragment.rpartition("?")[2]):
        for param, _ in parse_qsl(text, keep_blank_values=True):
            if _PRIVATE_PARAM_RE.search(_CAMEL_CASE_RE.sub("_", param).lower()):
                return True
    return False


_HLS_KEY_RE = re.compile(r"^#EXT-X-(?:SESSION-)?KEY:(.*)$", re.MULTILINE)
_HLS_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
# A playlist is untrusted input, so it can't ask for an unbounded number of requests.
_HLS_MAX_SEGMENTS = 20000

_MEDIA_TYPE_SUFFIXES = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/flac": ".flac",
    "audio/aac": ".aac",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/gif": ".gif",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
}
_MEDIA_KIND_SUFFIXES = {
    "video": (VIDEO_SUFFIXES, ".mp4"),
    "audio": (AUDIO_SUFFIXES, ".mp3"),
    "image": (IMAGE_SUFFIXES, ".jpg"),
}
# Everything a bad embed can raise. It just means trying the next candidate. MediaFound
# isn't here, because that is the success case.
_EMBED_FAILURES = (TextFetchError, httpx.HTTPError, httpx.InvalidURL, ValueError)
# On an allow-listed host these are still no video: Dailymotion serves plain clients an
# iframe of its own favicon.
_STATIC_FILE_SUFFIXES = (*IMAGE_SUFFIXES, ".ico", ".svg", ".css", ".js", ".woff", ".woff2")


def _media_suffix(kind: str, name: str, url: str, content_type: str) -> str:
    allowed, default = _MEDIA_KIND_SUFFIXES[kind]
    for candidate in (Path(name).suffix.lower(), url_suffix(url)):
        if candidate in allowed:
            return candidate
    from_type = _MEDIA_TYPE_SUFFIXES.get(content_type.split(";")[0].strip().lower(), "")
    return from_type if from_type in allowed else default


def _embed_candidates(markup: str, page_url: str) -> list[tuple[str, bool]]:
    """(absolute url, is an iframe), best first, without duplicates or the page itself."""
    metas: dict[str, str] = {}
    for tag in _META_TAG_RE.findall(markup):
        attributes = _tag_attrs(tag)
        key = (attributes.get("property") or attributes.get("name") or "").lower()
        if key in _EMBED_META_NAMES and attributes.get("content"):
            metas.setdefault(key, attributes["content"])
    sources: dict[str, list[str]] = {"video": [], "source": [], "audio": [], "iframe": []}
    for match in _MEDIA_TAG_RE.finditer(markup):
        src = _tag_attrs(match[0]).get("src")
        if src:
            sources[match[1].lower()].append(src)

    ordered = [(metas[name], False) for name in _EMBED_META_NAMES if name in metas]
    ordered += [(src, False) for tag in ("video", "source", "audio") for src in sources[tag]]
    ordered += [(src, True) for src in sources["iframe"]]

    seen = {page_url}
    candidates: list[tuple[str, bool]] = []
    for raw, is_frame in ordered:
        try:
            absolute = urljoin(page_url, raw.strip())
        except ValueError:
            continue
        if absolute in seen or urlsplit(absolute).scheme not in ("http", "https"):
            continue
        seen.add(absolute)
        candidates.append((absolute, is_frame))
    return candidates


def _m3u8_candidates(markup: str, page_url: str) -> list[str]:
    # Inline JSON escapes its slashes.
    text = markup.replace("\\/", "/")
    found = _M3U8_ABSOLUTE_RE.findall(text) + _M3U8_RELATIVE_RE.findall(text)
    unique = dict.fromkeys(urljoin(page_url, html_lib.unescape(url)) for url in found)
    return list(unique)[:_MAX_M3U8_CANDIDATES]


def _page_metadata(markup: str) -> str | None:
    """The title and description a plain GET saw, or None if there's no real description
    or it's a challenge or login page talking about itself.
    """
    title = _TITLE_RE.search(markup)
    title_text = html_lib.unescape(title[1]).strip() if title else ""
    descriptions: dict[str, str] = {}
    for tag in _META_TAG_RE.findall(markup):
        attributes = _tag_attrs(tag)
        key = (attributes.get("property") or attributes.get("name") or "").lower()
        if key in _DESCRIPTION_META_NAMES and attributes.get("content"):
            descriptions.setdefault(key, attributes["content"].strip())
    description = next(
        (descriptions[name] for name in _DESCRIPTION_META_NAMES if name in descriptions), ""
    )
    if len(description) < _MIN_PLAUSIBLE_EXTRACTED_LENGTH:
        return None
    combined = f"{title_text} {description}".lower()
    markers = (*_BOT_CHALLENGE_MARKERS, *_LOGIN_WALL_MARKERS, *_APP_SHELL_MARKERS)
    if any(marker in combined for marker in markers):
        return None
    heading = f"# {title_text}\n\n" if title_text else ""
    return f"{_METADATA_LABEL}\n\n{heading}{description}"


def _hls_attrs(line: str) -> dict[str, str]:
    return {name: value.strip('"') for name, value in _HLS_ATTR_RE.findall(line)}


def _refuse_encrypted_hls(playlist: str, url: str) -> None:
    for match in _HLS_KEY_RE.finditer(playlist):
        if _hls_attrs(match[1]).get("METHOD", "").upper() != "NONE":
            raise TextFetchError(
                f"HLS stream {url!r} is encrypted - this pipeline doesn't decrypt streams"
            )


def _hls_rendition(playlist: str, base_url: str) -> str:
    """The playlist to download from a master: the lowest-bandwidth variant, or that
    variant's separate audio track if it has one, since a demuxed video has no sound
    to transcribe.
    """
    variants: list[tuple[int, dict[str, str], str]] = []
    pending: dict[str, str] | None = None
    audio: list[dict[str, str]] = []
    for raw in playlist.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = _hls_attrs(line.partition(":")[2])
        elif line.startswith("#EXT-X-MEDIA:"):
            attributes = _hls_attrs(line.partition(":")[2])
            if attributes.get("TYPE") == "AUDIO" and attributes.get("URI"):
                audio.append(attributes)
        elif line and not line.startswith("#") and pending is not None:
            bandwidth = pending.get("BANDWIDTH", "")
            rate = int(bandwidth) if bandwidth.isdigit() else sys.maxsize
            variants.append((rate, pending, line))
            pending = None
    if not variants:
        raise TextFetchError(f"HLS master playlist {base_url!r} lists no streams")

    _, chosen, uri = min(variants, key=lambda variant: variant[0])
    group = [track for track in audio if track.get("GROUP-ID") == chosen.get("AUDIO")]
    if group:
        default = next((t for t in group if t.get("DEFAULT", "").upper() == "YES"), group[0])
        uri = default["URI"]
    return urljoin(base_url, uri)


def _hls_parts(playlist: str, base_url: str) -> tuple[list[str], bool]:
    """Everything to concatenate, in order, and whether it opens with an fMP4 init part.

    With byte ranges, the segments are slices of one file that already holds the init
    part and the whole stream, so each file is fetched once instead of slice by slice.
    """
    parts: list[str] = []
    current_map: str | None = None
    byte_ranges = False
    for raw in playlist.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-BYTERANGE"):
            byte_ranges = True
        elif line.startswith("#EXT-X-MAP:"):
            attributes = _hls_attrs(line.partition(":")[2])
            if not attributes.get("URI"):
                raise TextFetchError(f"HLS playlist {base_url!r} has an unusable init segment")
            byte_ranges = byte_ranges or "BYTERANGE" in attributes
            map_url = urljoin(base_url, attributes["URI"])
            if map_url != current_map:
                parts.append(map_url)
                current_map = map_url
        elif line and not line.startswith("#"):
            parts.append(urljoin(base_url, line))
    if byte_ranges:
        parts = list(dict.fromkeys(parts))
    if len(parts) > _HLS_MAX_SEGMENTS:
        raise TextFetchError(f"HLS playlist {base_url!r} lists too many segments")
    return parts, current_map is not None


class GenericHtmlFetcher:
    """Streaming GET for any public link classify_url_kind() routed here as "text"
    (blog posts, Google Docs and Sheets exports, feeds, data files, documents, ...).
    What the response turns out to be decides what happens to it:

    - HTML goes through trafilatura. A thin page gets searched for a video, and a
      client-rendered app shell falls back to RenderedHtmlFetcher (a read-only
      headless-Chromium render, unless text_capture.render_fallback.enabled is false),
      then to the page's title and description. Neither runs after a challenge page.
    - Text, JSON and feeds become text. Documents are attached to the vault.
    - Audio, video, images and HLS streams are saved and handed on through MediaFound.
    - A redirect onto an allow-listed media host is handed on through MediaFound too.

    A 404 or 410 tries the Wayback Machine at once. A 5xx or a connection failure raises
    ArchiveEligibleError, and the worker tries the archive only when retries run out. A
    401, 403 or 429 is the site saying no, so it stays an error: no render, no archive.
    Every request goes through _guarded_client(), and none carries cookies.
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client
        self._rendered = RenderedHtmlFetcher(settings)

    def fetch(self, url: str, content_id: str) -> TranscriptResult:
        owned_client = self._client or _guarded_client()
        owns_client = self._client is None
        try:
            google_doc = _GOOGLE_DOC_RE.match(url)
            if google_doc:
                return self._fetch_google_export(owned_client, url, google_doc, content_id)
            return self._fetch_page(owned_client, url, content_id)
        finally:
            if owns_client:
                owned_client.close()

    def fetch_archived(self, url: str, content_id: str, reason: str) -> TranscriptResult:
        """The Wayback copy of a page, for the worker when retries have run out."""
        owned_client = self._client or _guarded_client()
        owns_client = self._client is None
        try:
            return self._fetch_archived(owned_client, url, content_id, reason)
        finally:
            if owns_client:
                owned_client.close()

    def _result(self, content_id: str, text: str, backend: str) -> TranscriptResult:
        return TranscriptResult(
            content_id=content_id,
            text=text,
            content_kind="text",
            language=None,
            backend=backend,
            duration_seconds=None,
        )

    def _fetch_google_export(
        self, client: httpx.Client, url: str, google_doc: re.Match[str], content_id: str
    ) -> TranscriptResult:
        doc_type, doc_id = google_doc[1], google_doc[2]
        if doc_type == "spreadsheets":
            export_url = f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format=csv"
            gid = re.search(r"[?&#]gid=(\d+)", url)
            if gid:
                export_url += f"&gid={gid[1]}"
            expected_type = "text/csv"
        else:
            export_url = f"https://docs.google.com/{doc_type}/d/{doc_id}/export?format=txt"
            expected_type = "text/plain"
        try:
            fetched = _fetch_capped(
                client, export_url, self.settings.text_capture.max_document_mb, browser=True
            )
        except httpx.HTTPError as exc:
            raise TextFetchError(f"failed to fetch page {url!r}: {exc}") from exc
        if fetched.status_code != 200:
            raise TextFetchError(f"page {url!r} returned HTTP {fetched.status_code}")
        # A private doc redirects to Google's sign-in HTML instead.
        text = fetched.text.lstrip("﻿").strip()
        if not fetched.headers.get("content-type", "").startswith(expected_type) or not text:
            raise TextFetchError(f"Google doc {url!r} isn't public - no text export")
        return self._result(content_id, text, "gdocs-export")

    def _fetch_page(
        self, client: httpx.Client, url: str, content_id: str, *, archived: bool = False
    ) -> TranscriptResult:
        unreachable = TextFetchError if archived else ArchiveEligibleError
        try:
            response = _open_stream(client, url)
        except httpx.InvalidURL as exc:
            raise TextFetchError(f"failed to fetch page {url!r}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise unreachable(f"failed to fetch page {url!r}: {exc}") from exc
        try:
            if response.status_code == 200:
                try:
                    return self._route_response(client, response, url, content_id, archived)
                except httpx.HTTPError as exc:  # the connection dropped mid-body
                    raise unreachable(f"failed to fetch page {url!r}: {exc}") from exc
            status = response.status_code
        finally:
            response.close()

        message = f"page {url!r} returned HTTP {status}"
        if archived:
            raise TextFetchError(message)
        if status in (404, 410):
            try:
                return self._fetch_archived(client, url, content_id, f"returned HTTP {status}")
            except TextFetchError as exc:
                raise TextFetchError(f"{message}; no usable archived copy ({exc})") from exc
        if status >= 500:
            raise ArchiveEligibleError(message)
        raise TextFetchError(message)

    def _route_response(
        self,
        client: httpx.Client,
        response: httpx.Response,
        url: str,
        content_id: str,
        archived: bool,
    ) -> TranscriptResult:
        chunks = (
            _archived_chunks(response, url)
            if archived
            else response.iter_bytes(_STREAM_CHUNK_BYTES)
        )
        head = next(chunks, b"")
        final_url = str(response.url)
        content_type = response.headers.get("content-type", "")
        name = _disposition_name(response.headers)
        if not archived and self._redirected_to_media(url, final_url):
            raise MediaFound(url=final_url)

        kind = classify_response(head, content_type, final_url, name)
        document_limit = self.settings.text_capture.max_document_mb
        if kind in _MEDIA_KIND_SUFFIXES:
            self._save_direct_media(response, head, chunks, url, name, kind, content_id)
        data = _read_capped(response, head, chunks, url, document_limit)
        if kind in ("doc", "attach"):
            return attach_to_vault(
                self.settings,
                data,
                name or Path(unquote(urlparse(final_url).path)).name,
                content_id,
                "file-attachment",
                content_type,
            )
        text = data.decode(response.encoding or "utf-8", "replace").lstrip("﻿")
        if kind == "hls":
            self._hls_from_text(client, text, final_url, content_id)
        if kind == "html":
            return self._from_html(client, text, url, final_url, content_id, archived)

        if kind == "json":
            text = pretty_json(text)
        elif kind == "feed":
            text = feed_text(text) or strip_xml_tags(text)
        text = text.strip()
        if archived:
            _raise_if_challenge_or_login_wall(text, url)
        if not text:
            raise TextFetchError(f"page {url!r} produced no usable text")
        return self._result(content_id, text, "text-fetch")

    def _is_media_url(self, url: str) -> bool:
        if not self.settings.download.allowed_domains:
            return False
        return (
            validate_url(url, self.settings).ok and classify_url_kind(url, self.settings) == "media"
        )

    def _redirected_to_media(self, url: str, final_url: str) -> bool:
        if final_url == url:
            return False
        segments = [s.lower() for s in urlparse(final_url).path.split("/") if s]
        if not segments or any(s in _LOGIN_PATH_SEGMENTS for s in segments):
            return False  # a site's front page or sign-in, which is no video
        return self._is_media_url(final_url) and not self._is_media_url(url)

    def _fresh_media_dir(self, content_id: str) -> Path:
        out_dir = self.settings.tmp_dir / content_id
        shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True)
        return out_dir

    def _save_direct_media(
        self,
        response: httpx.Response,
        head: bytes,
        chunks: Iterator[bytes],
        url: str,
        name: str,
        kind: str,
        content_id: str,
    ) -> None:
        content_type = response.headers.get("content-type", "")
        suffix = _media_suffix(kind, name, str(response.url), content_type)
        target = self._fresh_media_dir(content_id) / f"file{suffix}"
        limit_mb = self.settings.text_capture.max_media_mb
        _save_stream(response, head, chunks, target, url, limit_mb)
        raise MediaFound(
            result=DownloadResult(
                content_id=content_id,
                media_type=MediaType.IMAGE if kind == "image" else MediaType.VIDEO,
                media_paths=[str(target)],
                platform="direct",
            )
        )

    def _from_html(
        self,
        client: httpx.Client,
        markup: str,
        url: str,
        final_url: str,
        content_id: str,
        archived: bool,
    ) -> TranscriptResult:
        extracted = (trafilatura.extract(markup) or "").strip()
        # A challenge/login-wall page can arrive as a plain HTTP 200 the
        # plain-GET path already sees fine - checked before the app-shell
        # escalation decision below, not only on the rendered path (found by
        # review, 2026-08-10).
        _raise_if_challenge_or_login_wall(extracted, url)
        if len(extracted) < _THIN_PAGE_CHARS:
            try:
                self._find_embedded_media(client, markup, final_url, content_id, hop=True)
            except MediaFound as found:
                found.context = found.context or extracted
                raise
        # Many modern pages (Notion, Docs, Airtable) are a client-rendered app
        # shell with no real content in the server-sent HTML at all -
        # trafilatura then extracts whatever stray text IS server-rendered
        # (e.g. a <noscript> fallback message), which is non-empty but not
        # the page's actual content. The empty-string check alone doesn't
        # catch this; matching known app-shell boilerplate and a minimum
        # plausible length does.
        if _looks_like_app_shell(extracted):
            return self._from_app_shell(markup, url, content_id, archived)
        return self._result(content_id, extracted, "html-fetch")

    def _from_app_shell(
        self, markup: str, url: str, content_id: str, archived: bool
    ) -> TranscriptResult:
        metadata = _page_metadata(markup)
        if archived or not self.settings.text_capture.render_fallback.enabled:
            if metadata:
                return self._result(content_id, metadata, "page-metadata")
            if archived:
                raise TextFetchError(f"archived copy {url!r} produced no usable text")
            raise TextFetchError(
                f"page {url!r} produced no usable text - it's rendered "
                "client-side with no real content in the server HTML (or requires "
                "login), rather than being genuinely empty. The browser-render "
                "fallback is disabled (text_capture.render_fallback.enabled: false)."
            )
        try:
            return self._rendered.fetch(url, content_id)
        except _PageRefused:
            raise  # the site said no, so nothing else gets tried
        except TextFetchError:
            if metadata:
                return self._result(content_id, metadata, "page-metadata")
            raise

    def _find_embedded_media(
        self, client: httpx.Client, markup: str, page_url: str, content_id: str, *, hop: bool
    ) -> None:
        """Raises MediaFound for the first embed that works. Returns if none does."""
        for candidate, is_frame in _embed_candidates(markup, page_url):
            try:
                self._try_embed(client, candidate, is_frame, content_id, hop)
            except _EMBED_FAILURES as exc:
                log_context(logger, logging.INFO, "embed not usable", url=candidate, error=str(exc))
        for playlist in _m3u8_candidates(markup, page_url):
            try:
                self._download_hls(client, playlist, content_id)
            except _EMBED_FAILURES as exc:
                log_context(logger, logging.INFO, "stream not usable", url=playlist, error=str(exc))

    def _try_embed(
        self, client: httpx.Client, candidate: str, is_frame: bool, content_id: str, hop: bool
    ) -> None:
        if self._is_media_url(candidate) and url_suffix(candidate) not in _STATIC_FILE_SUFFIXES:
            raise MediaFound(url=candidate)
        if is_frame:
            return  # an iframe to an unlisted host could be anything
        suffix = url_suffix(candidate)
        if suffix == ".m3u8":
            self._download_hls(client, candidate, content_id)
        elif hop or suffix in (*VIDEO_SUFFIXES, *AUDIO_SUFFIXES):
            self._follow_embed(client, candidate, content_id, hop)

    def _follow_embed(self, client: httpx.Client, url: str, content_id: str, hop: bool) -> None:
        response = _open_stream(client, url)
        try:
            if response.status_code != 200:
                raise TextFetchError(f"embed {url!r} returned HTTP {response.status_code}")
            chunks = response.iter_bytes(_STREAM_CHUNK_BYTES)
            head = next(chunks, b"")
            final_url = str(response.url)
            name = _disposition_name(response.headers)
            content_type = response.headers.get("content-type", "")
            kind = classify_response(head, content_type, final_url, name)
            if kind in ("audio", "video"):
                self._save_direct_media(response, head, chunks, url, name, kind, content_id)
            limit_mb = self.settings.text_capture.max_document_mb
            if kind == "hls":
                data = _read_capped(response, head, chunks, url, limit_mb)
                text = data.decode(response.encoding or "utf-8", "replace")
                self._hls_from_text(client, text, final_url, content_id)
            if kind == "html" and hop:
                data = _read_capped(response, head, chunks, url, limit_mb)
                markup = data.decode(response.encoding or "utf-8", "replace")
                self._find_embedded_media(client, markup, final_url, content_id, hop=False)
                return
            raise TextFetchError(f"embed {url!r} is {kind}, not a media file")
        finally:
            response.close()

    def _fetch_playlist(self, client: httpx.Client, url: str) -> _Fetched:
        fetched = _fetch_capped(
            client, url, self.settings.text_capture.max_document_mb, browser=True
        )
        if fetched.status_code != 200:
            raise TextFetchError(f"HLS playlist {url!r} returned HTTP {fetched.status_code}")
        return fetched

    def _download_hls(self, client: httpx.Client, url: str, content_id: str) -> None:
        fetched = self._fetch_playlist(client, url)
        self._hls_from_text(client, fetched.text, fetched.url, content_id)

    def _hls_from_text(
        self, client: httpx.Client, playlist: str, playlist_url: str, content_id: str
    ) -> None:
        """Downloads the stream and raises MediaFound. Never returns: a stream that can't
        be read raises TextFetchError. An encrypted one is refused, not decrypted.
        """
        _refuse_encrypted_hls(playlist, playlist_url)
        if "#EXT-X-STREAM-INF" in playlist:
            try:
                fetched = self._fetch_playlist(client, _hls_rendition(playlist, playlist_url))
            except httpx.HTTPError as exc:
                raise TextFetchError(f"failed to fetch HLS stream: {exc}") from exc
            playlist, playlist_url = fetched.text, fetched.url
            _refuse_encrypted_hls(playlist, playlist_url)

        parts, has_init = _hls_parts(playlist, playlist_url)
        if not parts:
            raise TextFetchError(f"HLS playlist {playlist_url!r} lists no segments")
        first_suffix = url_suffix(parts[0])
        if has_init:
            suffix = ".mp4"
        elif first_suffix in (*VIDEO_SUFFIXES, *AUDIO_SUFFIXES):
            suffix = first_suffix
        else:
            suffix = ".ts"
        target = self._fresh_media_dir(content_id) / f"file{suffix}"
        self._save_hls_parts(client, parts, target, playlist_url)
        raise MediaFound(
            result=DownloadResult(
                content_id=content_id,
                media_type=MediaType.VIDEO,
                media_paths=[str(target)],
                platform="hls",
            )
        )

    def _save_hls_parts(
        self, client: httpx.Client, parts: list[str], target: Path, playlist_url: str
    ) -> None:
        limit_mb = self.settings.text_capture.max_media_mb
        limit = limit_mb * 1024 * 1024
        written = 0
        try:
            with target.open("wb") as sink:
                for part in parts:
                    response = _open_stream(client, part)
                    try:
                        if response.status_code != 200:
                            raise TextFetchError(
                                f"HLS segment {part!r} returned HTTP {response.status_code}"
                            )
                        for chunk in response.iter_bytes(_STREAM_CHUNK_BYTES):
                            written += len(chunk)
                            if written > limit:
                                raise _too_large(playlist_url, limit_mb)
                            sink.write(chunk)
                    finally:
                        response.close()
            if written == 0:
                raise TextFetchError(f"HLS stream {playlist_url!r} was empty")
        except httpx.HTTPError as exc:
            target.unlink(missing_ok=True)
            raise TextFetchError(f"failed to fetch HLS stream {playlist_url!r}: {exc}") from exc
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def _wayback_snapshot(self, client: httpx.Client, url: str) -> tuple[str, str]:
        """(snapshot URL, 14-digit timestamp). Private-looking links never leave here."""
        if _looks_private(url):
            raise TextFetchError("the link looks private, so it wasn't sent to the archive")
        try:
            response = client.get(
                _WAYBACK_LOOKUP, params={"url": url}, headers={"User-Agent": _BOT_UA}
            )
            if response.status_code != 200:
                raise TextFetchError(f"archive lookup returned HTTP {response.status_code}")
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TextFetchError(f"archive lookup failed: {exc}") from exc
        snapshots = payload.get("archived_snapshots") if isinstance(payload, dict) else None
        closest = snapshots.get("closest") if isinstance(snapshots, dict) else None
        if not isinstance(closest, dict) or not closest.get("available"):
            raise TextFetchError("the archive has no snapshot")
        timestamp = str(closest.get("timestamp", ""))
        if str(closest.get("status")) != "200" or not re.fullmatch(r"\d{14}", timestamp):
            raise TextFetchError("the archive has no usable snapshot")
        return f"https://web.archive.org/web/{timestamp}id_/{url}", timestamp

    def _fetch_archived(
        self, client: httpx.Client, url: str, content_id: str, reason: str
    ) -> TranscriptResult:
        snapshot_url, timestamp = self._wayback_snapshot(client, url)
        date = f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}"
        label = f"[Archived copy from {date}; the live page {reason}]"
        try:
            result = self._fetch_page(client, snapshot_url, content_id, archived=True)
        except MediaFound as found:
            found.context = f"{label}\n\n{found.context}" if found.context else label
            raise
        return self._result(content_id, f"{label}\n\n{result.text}", "wayback")


class TextFetcher(Protocol):
    def fetch(self, url: str, content_id: str) -> TranscriptResult: ...


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

    GitHub means github.com exactly: gist.github.com and docs.github.com are ordinary
    pages the repo API can't read. When the GitHub or Notion fetcher fails, the generic
    fetcher gets a try before the error is final. Drive doesn't, because a generic GET
    of a Drive link only ever returns Drive's own viewer page.
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
        if host == "github.com":
            return self._with_generic_fallback(self._github, url, content_id)
        if any(host == d or host.endswith(f".{d}") for d in _NOTION_DOMAINS):
            return self._with_generic_fallback(self._notion, url, content_id)
        if any(host == d or host.endswith(f".{d}") for d in _DRIVE_DOMAINS):
            return self._drive.fetch(url, content_id)
        return self._generic_html.fetch(url, content_id)

    def fetch_archived(self, url: str, content_id: str, reason: str) -> TranscriptResult:
        return self._generic_html.fetch_archived(url, content_id, reason)

    def _with_generic_fallback(
        self, fetcher: TextFetcher, url: str, content_id: str
    ) -> TranscriptResult:
        try:
            return fetcher.fetch(url, content_id)
        except TextFetchError as first:
            try:
                return self._generic_html.fetch(url, content_id)
            except TextFetchError as second:
                raise type(second)(f"{first}; generic fetch also failed: {second}") from second


def get_text_fetcher(settings: Settings) -> TextFetcher:
    return DispatchingTextFetcher(settings)
