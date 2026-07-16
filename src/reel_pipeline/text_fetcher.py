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
from urllib.parse import urlparse

import httpx
import trafilatura

from reel_pipeline.config import Settings
from reel_pipeline.models import TranscriptResult

_GITHUB_API = "https://api.github.com"


class TextFetchError(RuntimeError):
    """Raised when fetching or extracting text content fails."""


def _parse_github_path(url: str) -> tuple[str, str, str | None]:
    """Returns (owner, repo, file_path). file_path is None for a repo-root URL,
    or the path portion after "blob/<ref>/" for a specific-file URL.
    """
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2:
        raise TextFetchError(f"could not parse a GitHub owner/repo from {url!r}")
    owner, repo = parts[0], parts[1]
    if len(parts) > 3 and parts[2] == "blob":
        file_path = "/".join(parts[4:])
        return owner, repo, file_path or None
    return owner, repo, None


class GitHubFetcher:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client

    def fetch(self, url: str, content_id: str) -> TranscriptResult:
        owner, repo, file_path = _parse_github_path(url)
        owned_client = self._client or httpx.Client(timeout=30.0)
        owns_client = self._client is None
        try:
            metadata = self._get_json(owned_client, f"{_GITHUB_API}/repos/{owner}/{repo}")
            if file_path:
                content = self._get_file_content(
                    owned_client, f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{file_path}"
                )
                heading = f"# {owner}/{repo} - {file_path}"
            else:
                content = self._get_file_content(
                    owned_client, f"{_GITHUB_API}/repos/{owner}/{repo}/readme"
                )
                heading = f"# {owner}/{repo}"
        finally:
            if owns_client:
                owned_client.close()

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
                raise TextFetchError(
                    f"Notion page {url!r} returned HTTP {response.status_code}"
                )
            html = response.text
        finally:
            if owns_client:
                owned_client.close()

        extracted = trafilatura.extract(html)
        if not extracted or not extracted.strip():
            raise TextFetchError(
                f"Notion page {url!r} produced no extractable text - it's likely "
                "not actually public (login wall), rather than empty"
            )

        return TranscriptResult(
            content_id=content_id,
            text=extracted.strip(),
            content_kind="text",
            language=None,
            backend="notion-fetch",
            duration_seconds=None,
        )
