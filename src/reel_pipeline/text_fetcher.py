"""Text-content ingestion for non-media knowledge sources (GitHub, Notion).

Produces the same TranscriptResult shape transcriber.py and image_describer.py
already produce, so downstream enrichment and note-writing are identical
regardless of source - the worker picks this instead of Downloader based on
StateRecord.content_kind (set at registration time by
validators.classify_url_kind()).

Public links only - no OAuth or API keys. GitHub's public REST API needs none
for public repos; Notion pages are fetched as plain HTTP GET requests against
their public share URL (no JS execution - not browser automation).
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


_NOTION_APP_SHELL_MARKERS = (
    "javascript must be enabled",
    "please enable javascript",
)
# Below this length, extracted text is more likely to be app-shell boilerplate
# (a stray noscript/meta fragment) than real page content - real Notion pages,
# even short ones, run well past this once trafilatura strips markup.
_MIN_PLAUSIBLE_EXTRACTED_LENGTH = 60


class NotionFetcher:
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
                raise TextFetchError(f"failed to fetch Notion page {url!r}: {exc}") from exc
            if response.status_code != 200:
                raise TextFetchError(f"Notion page {url!r} returned HTTP {response.status_code}")
            html = response.text
        finally:
            if owns_client:
                owned_client.close()

        extracted = (trafilatura.extract(html) or "").strip()
        # Many modern Notion pages are a client-rendered app shell with no real
        # content in the server-sent HTML at all - trafilatura then extracts
        # whatever stray text IS server-rendered (e.g. a <noscript> fallback
        # message), which is non-empty but not the page's actual content. The
        # empty-string check alone doesn't catch this; matching known app-shell
        # boilerplate and a minimum plausible length does.
        too_short = len(extracted) < _MIN_PLAUSIBLE_EXTRACTED_LENGTH
        has_app_shell_marker = any(
            marker in extracted.lower() for marker in _NOTION_APP_SHELL_MARKERS
        )
        looks_like_app_shell = not extracted or too_short or has_app_shell_marker
        if looks_like_app_shell:
            raise TextFetchError(
                f"Notion page {url!r} produced no usable text - it's rendered "
                "client-side with no real content in the server HTML (or requires "
                "login), rather than being genuinely empty. This pipeline cannot "
                "fetch it without browser automation, which is against project policy."
            )

        return TranscriptResult(
            content_id=content_id,
            text=extracted,
            content_kind="text",
            language=None,
            backend="notion-fetch",
            duration_seconds=None,
        )


class TextFetcher(Protocol):
    def fetch(self, url: str, content_id: str) -> TranscriptResult: ...


_GITHUB_DOMAINS = ("github.com",)
_NOTION_DOMAINS = ("notion.so", "notion.site")


class DispatchingTextFetcher:
    """Routes each URL to the platform-appropriate concrete fetcher."""

    def __init__(self, settings: Settings):
        self._github = GitHubFetcher(settings)
        self._notion = NotionFetcher(settings)

    def fetch(self, url: str, content_id: str) -> TranscriptResult:
        host = urlparse(url).netloc.split(":")[0].lower()
        if host.startswith("www."):
            host = host[len("www.") :]
        if any(host == d or host.endswith(f".{d}") for d in _GITHUB_DOMAINS):
            return self._github.fetch(url, content_id)
        if any(host == d or host.endswith(f".{d}") for d in _NOTION_DOMAINS):
            return self._notion.fetch(url, content_id)
        raise TextFetchError(f"unrecognized text-capture domain for {url!r}")


def get_text_fetcher(settings: Settings) -> TextFetcher:
    return DispatchingTextFetcher(settings)
