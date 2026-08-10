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

import base64
import re
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

            child_title = _notion_rich_text_to_plain(
                child.get("properties", {}).get("title")
            )
            if child_title and child.get("type") not in _NOTION_FILENAME_TITLE_BLOCK_TYPES:
                lines.append(child_title)
            if child.get("type") in _NOTION_COLLECTION_BLOCK_TYPES:
                # Linked database - row data needs a separate collection-query
                # API call this version doesn't make (see class docstring).
                lines.append(
                    f"[linked database: {child_title or 'untitled'} - contents not included]"
                )

            self._walk_block_children(child, blocks, lines, visited)


_APP_SHELL_MARKERS = (
    "javascript must be enabled",
    "please enable javascript",
)
# Below this length, extracted text is more likely to be app-shell boilerplate
# (a stray noscript/meta fragment) than real page content - real pages,
# even short ones, run well past this once trafilatura strips markup.
_MIN_PLAUSIBLE_EXTRACTED_LENGTH = 60


class GenericHtmlFetcher:
    """Plain HTTP GET + trafilatura text extraction for any public page
    classify_url_kind() routed here as "text" (Notion share links, Google
    Docs' /mobilebasic public view, Airtable share links, blog posts, ...).
    No JS execution - not browser automation.

    Client-rendered app shells with no server-side content (common on Notion,
    possible on the others) raise TextFetchError rather than returning
    boilerplate - see the app-shell heuristic below.
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client

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
        # Many modern pages (Notion, Docs, Airtable) are a client-rendered app
        # shell with no real content in the server-sent HTML at all -
        # trafilatura then extracts whatever stray text IS server-rendered
        # (e.g. a <noscript> fallback message), which is non-empty but not
        # the page's actual content. The empty-string check alone doesn't
        # catch this; matching known app-shell boilerplate and a minimum
        # plausible length does.
        too_short = len(extracted) < _MIN_PLAUSIBLE_EXTRACTED_LENGTH
        has_app_shell_marker = any(
            marker in extracted.lower() for marker in _APP_SHELL_MARKERS
        )
        looks_like_app_shell = not extracted or too_short or has_app_shell_marker
        if looks_like_app_shell:
            raise TextFetchError(
                f"page {url!r} produced no usable text - it's rendered "
                "client-side with no real content in the server HTML (or requires "
                "login), rather than being genuinely empty. This pipeline cannot "
                "fetch it without browser automation, which is against project policy."
            )

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


class DispatchingTextFetcher:
    """Routes each URL to the platform-appropriate concrete fetcher.

    GitHub and Notion each get their own API-backed fetcher (structured data
    via an actual API instead of scraping rendered/raw HTML); every other
    host classify_url_kind() has already routed here as "text" goes through
    the generic HTML fetcher - there's no second domain gate to keep in sync
    with settings (a prior version of this class had one, which was the root
    cause of docs.google.com/airtable.com/findarepo.com/thefounderos.com
    being config-allowed but still rejected as "unrecognized text-capture
    domain"; fixed 2026-08-09, then made moot by 2026-08-10's catch-all
    text-capture policy in CLAUDE.md).
    """

    def __init__(self, settings: Settings):
        self._github = GitHubFetcher(settings)
        self._notion = NotionFetcher(settings)
        self._generic_html = GenericHtmlFetcher(settings)

    def fetch(self, url: str, content_id: str) -> TranscriptResult:
        host = urlparse(url).netloc.split(":")[0].lower()
        if host.startswith("www."):
            host = host[len("www.") :]
        if any(host == d or host.endswith(f".{d}") for d in _GITHUB_DOMAINS):
            return self._github.fetch(url, content_id)
        if any(host == d or host.endswith(f".{d}") for d in _NOTION_DOMAINS):
            return self._notion.fetch(url, content_id)
        return self._generic_html.fetch(url, content_id)


def get_text_fetcher(settings: Settings) -> TextFetcher:
    return DispatchingTextFetcher(settings)
