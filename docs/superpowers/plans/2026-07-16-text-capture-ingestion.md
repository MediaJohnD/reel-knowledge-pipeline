# Text-Capture Ingestion Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second ingestion path — parallel to the existing video/photo pipeline — that captures GitHub repos/files and public Notion pages as text, reusing the existing enrichment → Obsidian note → skill-artifact chain unchanged.

**Architecture:** A new `text_fetcher.py` module produces the same `TranscriptResult` shape `transcriber.py`/`image_describer.py` already produce. `validators.classify_url_kind()` decides "media" vs "text" per URL at registration time (before any download attempt), `worker.py` branches on the stored `content_kind`, and `Enricher` picks between two prompt templates based on it.

**Tech Stack:** Python 3.12, httpx (already a dependency) for both GitHub's REST API and Notion page fetching, `trafilatura` (new dependency) for Notion HTML→text extraction, `respx` (already a dev dependency) for mocking httpx in tests.

## Global Constraints

- Public links only — no new OAuth or API-key credential surface (per spec, confirmed with the project owner).
- v1 scope is exactly `github.com` and `notion.so`/`notion.site`. Airtable is explicitly excluded (JS-rendered share pages conflict with the no-browser-automation rule) and continues to route to `needs-attention.txt` like any other unsupported domain.
- No browser automation, ever — this applies to Notion fetching as much as to the existing yt-dlp/gallery-dl paths.
- Every new stage that can fail must raise a typed error (`TextFetchError`) and land the item in `failed`/`needs-attention.txt`, exactly like existing failures — never silently enrich from empty/garbage text.
- Follow existing code conventions exactly: `Protocol` + concrete class + `get_*(settings)` factory function (see `downloader.py`, `image_describer.py`); `httpx.Client | None = None` constructor param for testability (see `enricher.py`, `llm_client.py`); `respx.mock` for httpx test mocking (see `test_llm_client.py`, `test_image_describer.py`).

---

## Task 1: Config and model scaffolding

**Files:**
- Modify: `src/reel_pipeline/models.py`
- Modify: `src/reel_pipeline/config.py`
- Modify: `config/settings.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `TranscriptResult.content_kind: Literal["media", "text"] = "media"` (models.py), `StateRecord.content_kind: Literal["media", "text"] = "media"` (models.py), `TextCaptureConfig` class with `allowed_domains: list[str]` (config.py), `Settings.text_capture: TextCaptureConfig` field, `Settings.enrich_text_capture_prompt: Path` property, `PromptsConfig.enrich_text_capture: str = "config/prompts/enrich_text_capture.md"`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_loads_text_capture_defaults_from_settings_yaml():
    settings = load_settings(config_path=DEFAULT_SETTINGS_PATH, env={})

    assert "github.com" in settings.text_capture.allowed_domains
    assert "notion.so" in settings.text_capture.allowed_domains
    assert "notion.site" in settings.text_capture.allowed_domains
    assert settings.enrich_text_capture_prompt.name == "enrich_text_capture.md"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_loads_text_capture_defaults_from_settings_yaml -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'text_capture'`

- [ ] **Step 3: Write minimal implementation**

In `src/reel_pipeline/models.py`, add `Literal` to the imports and add `content_kind` to both models:

```python
from typing import Literal
```

Modify `StateRecord` (add after `status: ItemStatus`):

```python
class StateRecord(BaseModel):
    content_id: str
    url: str
    normalized_url: str
    source: QueueSource
    status: ItemStatus
    content_kind: Literal["media", "text"] = "media"
    added_at: datetime
    updated_at: datetime
    error: str | None = None
    note_path: str | None = None
    skill_path: str | None = None
```

Modify `TranscriptResult` (add `content_kind` field):

```python
class TranscriptResult(BaseModel):
    """Text content extracted from the downloaded media - a literal transcript
    for video/audio (from Transcriber), a vision-model description for image
    posts (from ImageDescriber), or fetched page text for GitHub/Notion links
    (from TextFetcher). Downstream stages (enrichment, note writing) treat
    `.text` identically regardless of source; `content_kind` only changes
    which enrichment prompt is used.
    """

    content_id: str
    text: str
    content_kind: Literal["media", "text"] = "media"
    language: str | None = None
    backend: str
    duration_seconds: float | None = None
```

In `src/reel_pipeline/config.py`, add a `TextCaptureConfig` class after `DownloadConfig`:

```python
class TextCaptureConfig(BaseModel):
    # Domains routed to TextFetcher instead of Downloader - see validators.classify_url_kind().
    # Kept deliberately separate from download.allowed_domains: these aren't media
    # platforms, and mixing the two lists would make classify_url_kind() ambiguous.
    allowed_domains: list[str] = Field(default_factory=list)
```

Add `enrich_text_capture` to `PromptsConfig`:

```python
class PromptsConfig(BaseModel):
    enrich_transcript: str = "config/prompts/enrich_transcript.md"
    enrich_text_capture: str = "config/prompts/enrich_text_capture.md"
    create_skill: str = "config/prompts/create_skill.md"
    describe_image_post: str = "config/prompts/describe_image_post.md"
```

Add `text_capture` field to `Settings` (after `download: DownloadConfig`):

```python
    text_capture: TextCaptureConfig = Field(default_factory=TextCaptureConfig)
```

Add `enrich_text_capture_prompt` property to `Settings` (after `enrich_transcript_prompt`):

```python
    @property
    def enrich_text_capture_prompt(self) -> Path:
        return self.resolve(self.prompts.enrich_text_capture)
```

In `load_settings()`, add `text_capture=TextCaptureConfig(**raw.get("text_capture", {})),` to the `Settings(...)` constructor call, alongside the existing `download=DownloadConfig(...)` line.

In `config/settings.yaml`, add a new section after the existing `download:` block:

```yaml
# Non-media knowledge sources - routed to TextFetcher (fetch + extract text)
# instead of Downloader (yt-dlp/gallery-dl). Public links only - no OAuth/API
# keys. See docs/runbook.md's "GitHub and Notion text capture" section and
# docs/superpowers/specs/2026-07-16-text-capture-ingestion-design.md.
text_capture:
  allowed_domains:
    - github.com
    - notion.so
    - notion.site
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py::test_loads_text_capture_defaults_from_settings_yaml -v`
Expected: PASS

- [ ] **Step 5: Run the full test suite to check nothing broke**

Run: `uv run pytest -q`
Expected: all existing tests still pass (adding fields with defaults is backward-compatible)

- [ ] **Step 6: Commit**

```bash
git add src/reel_pipeline/models.py src/reel_pipeline/config.py config/settings.yaml tests/test_config.py
git commit -m "feat: add text_capture config and content_kind fields for text-capture ingestion"
```

---

## Task 2: URL kind classification

**Files:**
- Modify: `src/reel_pipeline/validators.py`
- Test: `tests/test_validators.py`

**Interfaces:**
- Consumes: `Settings.download.allowed_domains`, `Settings.text_capture.allowed_domains`, `Settings.download.blocked_domains` (all from Task 1/existing).
- Produces: `classify_url_kind(url: str, settings: Settings) -> Literal["media", "text"] | None` — `None` means "not in either allow-list" (caller should fall through to today's existing rejection message).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_validators.py`:

```python
from reel_pipeline.validators import classify_url_kind


def test_classify_url_kind_media_domain(tmp_path):
    settings = make_settings(tmp_path, allowed=["youtube.com"])
    settings.text_capture.allowed_domains = ["github.com"]

    assert classify_url_kind("https://youtube.com/watch?v=abc", settings) == "media"


def test_classify_url_kind_text_domain(tmp_path):
    settings = make_settings(tmp_path, allowed=["youtube.com"])
    settings.text_capture.allowed_domains = ["github.com"]

    assert classify_url_kind("https://github.com/owner/repo", settings) == "text"


def test_classify_url_kind_unmatched_domain_returns_none(tmp_path):
    settings = make_settings(tmp_path, allowed=["youtube.com"])
    settings.text_capture.allowed_domains = ["github.com"]

    assert classify_url_kind("https://airtable.com/base/abc", settings) is None


def test_classify_url_kind_prefers_media_on_overlap(tmp_path):
    """Documented tie-break (see validators.py) for the defensive case where a
    domain is misconfigured into both lists - should not happen in practice
    since the platforms don't overlap, but the precedence must be deterministic.
    """
    settings = make_settings(tmp_path, allowed=["example.com"])
    settings.text_capture.allowed_domains = ["example.com"]

    assert classify_url_kind("https://example.com/x", settings) == "media"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_validators.py -k classify_url_kind -v`
Expected: FAIL with `ImportError: cannot import name 'classify_url_kind'`

- [ ] **Step 3: Write minimal implementation**

In `src/reel_pipeline/validators.py`, add after `validate_url()`:

```python
def _matches_any(host: str, domains: list[str]) -> bool:
    return any(host == d or host.endswith(f".{d}") for d in domains)


def classify_url_kind(url: str, settings: Settings) -> Literal["media", "text"] | None:
    """Which pipeline a URL belongs in, checked before any download/fetch attempt.

    Returns None if the host matches neither allow-list - the caller (QueueManager)
    falls through to today's existing "not in the configured allow-list" rejection.
    The two allow-lists are expected to stay disjoint (no domain in both) since the
    platforms don't overlap in practice; "media" wins as a defensive tie-break if
    that assumption is ever violated, so behavior stays deterministic either way.
    """
    parsed = urlparse(url.strip())
    host = parsed.netloc.split(":")[0].lower()
    if host.startswith("www."):
        host = host[len("www.") :]

    if _matches_any(host, settings.download.allowed_domains):
        return "media"
    if _matches_any(host, settings.text_capture.allowed_domains):
        return "text"
    return None
```

Add `Literal` to the imports at the top of the file:

```python
from typing import Literal
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_validators.py -k classify_url_kind -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/reel_pipeline/validators.py tests/test_validators.py
git commit -m "feat: add classify_url_kind() to route URLs between media and text-capture pipelines"
```

---

## Task 3: QueueManager registration wiring

**Files:**
- Modify: `src/reel_pipeline/queue_manager.py`
- Test: `tests/test_queue_manager.py`

**Interfaces:**
- Consumes: `classify_url_kind(url, settings)` (Task 2), `StateRecord.content_kind` (Task 1).
- Produces: `QueueManager._register()` now sets `content_kind` on every newly-registered `StateRecord`, using `classify_url_kind()` for domains that pass `validate_url()`'s allow-list check (currently only `download.allowed_domains`) or a text-capture-specific validation path.

**Important:** `validate_url()` (Task 2's caller context) currently rejects any URL not in `download.allowed_domains`. This task changes `_register()` to also accept URLs whose `classify_url_kind()` is `"text"`, even though `validate_url()` alone would reject them - `_register()` becomes the single place that combines both checks, so `validate_url()` itself stays focused on shape/blocklist validation plus the media allow-list it already handles.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_queue_manager.py`:

```python
from reel_pipeline.config import TextCaptureConfig


def make_settings_with_text_capture(tmp_path) -> Settings:
    return Settings(
        project_root=tmp_path,
        download=DownloadConfig(allowed_domains=["youtube.com"], blocked_domains=["instagram.com"]),
        text_capture=TextCaptureConfig(allowed_domains=["github.com"]),
    )


def test_github_url_registers_as_pending_with_text_content_kind(tmp_path):
    settings = make_settings_with_text_capture(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://github.com/owner/repo\n", encoding="utf-8")

    registered = qm.sync_queue_file_into_state()

    assert len(registered) == 1
    assert registered[0].status == ItemStatus.PENDING
    assert registered[0].content_kind == "text"


def test_youtube_url_registers_with_media_content_kind(tmp_path):
    settings = make_settings_with_text_capture(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://www.youtube.com/watch?v=abc123\n", encoding="utf-8")

    registered = qm.sync_queue_file_into_state()

    assert registered[0].content_kind == "media"


def test_unmatched_domain_still_rejected_with_text_capture_configured(tmp_path):
    settings = make_settings_with_text_capture(tmp_path)
    qm = QueueManager(settings)
    qm.queue_file.write_text("https://airtable.com/base/abc\n", encoding="utf-8")

    registered = qm.sync_queue_file_into_state()

    assert registered[0].status == ItemStatus.BLOCKED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_queue_manager.py -k "content_kind or unmatched_domain_still" -v`
Expected: FAIL — `test_github_url_registers_as_pending_with_text_content_kind` fails because `github.com` isn't in `download.allowed_domains`, so `validate_url()` rejects it (status BLOCKED, not PENDING).

- [ ] **Step 3: Write minimal implementation**

In `src/reel_pipeline/queue_manager.py`, modify the imports:

```python
from reel_pipeline.validators import classify_url_kind, validate_url
```

Replace the `_register()` method body:

```python
def _register(self, url: str, source: QueueSource, state: dict[str, StateRecord]) -> StateRecord:
    result = validate_url(url, self.settings)
    now = datetime.now(UTC)

    # validate_url() only knows about download.allowed_domains - a URL it
    # rejects for "not in the configured allow-list" may still be a valid
    # text-capture URL, so re-check via classify_url_kind() before treating
    # it as genuinely blocked.
    if not result.ok and not result.blocked:
        kind = classify_url_kind(url, self.settings)
        if kind == "text":
            content_id = result.content_id
            existing = state.get(content_id)
            if existing is not None:
                return existing
            record = StateRecord(
                content_id=content_id,
                url=url,
                normalized_url=result.normalized_url,
                source=source,
                status=ItemStatus.PENDING,
                content_kind="text",
                added_at=now,
                updated_at=now,
            )
            state[content_id] = record
            return record

    if not result.ok:
        content_id = result.content_id or url
        existing = state.get(content_id)
        if existing is None:
            record = StateRecord(
                content_id=content_id,
                url=url,
                normalized_url=result.normalized_url,
                source=source,
                status=ItemStatus.BLOCKED,
                added_at=now,
                updated_at=now,
                error=result.reason,
            )
            state[content_id] = record
            self.append_needs_attention(url, result.reason or "validation failed")
            return record
        return existing

    existing = state.get(result.content_id)
    if existing is not None:
        return existing

    record = StateRecord(
        content_id=result.content_id,
        url=url,
        normalized_url=result.normalized_url,
        source=source,
        status=ItemStatus.PENDING,
        content_kind="media",
        added_at=now,
        updated_at=now,
    )
    state[result.content_id] = record
    return record
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_queue_manager.py -v`
Expected: PASS (all tests, including the 3 new ones)

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass

- [ ] **Step 6: Commit**

```bash
git add src/reel_pipeline/queue_manager.py tests/test_queue_manager.py
git commit -m "feat: register text-capture URLs as pending with content_kind=text"
```

---

## Task 4: GitHub fetching

**Files:**
- Create: `src/reel_pipeline/text_fetcher.py`
- Test: `tests/test_text_fetcher.py`

**Interfaces:**
- Consumes: `Settings` (config.py), `TranscriptResult` (models.py).
- Produces: `TextFetchError(RuntimeError)`, `GitHubFetcher` class with `fetch(self, url: str, content_id: str) -> TranscriptResult`. This task only wires up GitHub; `TextFetcher` (the dispatcher other modules import) is added in Task 6.

- [ ] **Step 1: Write the failing test**

Create `tests/test_text_fetcher.py`:

```python
from __future__ import annotations

import base64

import httpx
import pytest
import respx

from reel_pipeline.config import Settings
from reel_pipeline.text_fetcher import GitHubFetcher, TextFetchError


def _readme_response(content: str) -> dict:
    return {"content": base64.b64encode(content.encode()).decode(), "encoding": "base64"}


@respx.mock
def test_github_fetcher_fetches_repo_root_metadata_and_readme(tmp_path):
    settings = Settings(project_root=tmp_path)
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "owner/repo",
                "description": "A tool that does things.",
                "stargazers_count": 42,
                "language": "Python",
                "topics": ["cli", "tooling"],
            },
        )
    )
    respx.get("https://api.github.com/repos/owner/repo/readme").mock(
        return_value=httpx.Response(200, json=_readme_response("# repo\n\nDoes things."))
    )

    result = GitHubFetcher(settings).fetch("https://github.com/owner/repo", "cid1")

    assert "owner/repo" in result.text
    assert "A tool that does things." in result.text
    assert "Does things." in result.text
    assert result.content_kind == "text"
    assert result.backend == "github-api"


@respx.mock
def test_github_fetcher_fetches_specific_file_not_readme(tmp_path):
    settings = Settings(project_root=tmp_path)
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "owner/repo",
                "description": "A tool that does things.",
                "stargazers_count": 1,
                "language": None,
                "topics": [],
            },
        )
    )
    respx.get("https://api.github.com/repos/owner/repo/contents/docs/guide.md").mock(
        return_value=httpx.Response(200, json=_readme_response("# Guide\n\nSpecific file content."))
    )

    result = GitHubFetcher(settings).fetch(
        "https://github.com/owner/repo/blob/main/docs/guide.md", "cid2"
    )

    assert "Specific file content." in result.text
    assert "owner/repo" in result.text


@respx.mock
def test_github_fetcher_raises_clear_error_on_404(tmp_path):
    settings = Settings(project_root=tmp_path)
    respx.get("https://api.github.com/repos/owner/private-repo").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )

    with pytest.raises(TextFetchError, match="owner/private-repo"):
        GitHubFetcher(settings).fetch("https://github.com/owner/private-repo", "cid3")


@respx.mock
def test_github_fetcher_raises_clear_error_on_rate_limit(tmp_path):
    settings = Settings(project_root=tmp_path)
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"},
            json={"message": "API rate limit exceeded"},
        )
    )

    with pytest.raises(TextFetchError, match="rate limit"):
        GitHubFetcher(settings).fetch("https://github.com/owner/repo", "cid4")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_text_fetcher.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'reel_pipeline.text_fetcher'`

- [ ] **Step 3: Write minimal implementation**

Create `src/reel_pipeline/text_fetcher.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_text_fetcher.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/reel_pipeline/text_fetcher.py tests/test_text_fetcher.py
git commit -m "feat: add GitHubFetcher for repo-root and specific-file text capture"
```

---

## Task 5: Notion fetching (new dependency: trafilatura)

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/reel_pipeline/text_fetcher.py`
- Test: `tests/test_text_fetcher.py`

**Interfaces:**
- Produces: `NotionFetcher` class with `fetch(self, url: str, content_id: str) -> TranscriptResult`, added to `text_fetcher.py`.

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, add `trafilatura` to `dependencies`:

```toml
dependencies = [
    "typer>=0.12",
    "pydantic>=2.6",
    "httpx>=0.27",
    "PyYAML>=6.0",
    "python-dotenv>=1.0",
    "fastapi>=0.111",
    "uvicorn>=0.30",
    "yt-dlp>=2024.8.6",
    "gallery-dl>=1.30",
    "trafilatura>=1.12",
]
```

Run: `uv sync`
Expected: `trafilatura` and its own dependencies install cleanly.

- [ ] **Step 2: Write the failing test**

Add to `tests/test_text_fetcher.py`:

```python
from reel_pipeline.text_fetcher import NotionFetcher


_NOTION_PAGE_HTML = """
<html><body><article>
<h1>Project Notes</h1>
<p>This page documents the onboarding process for new team members.</p>
<p>Step one: clone the repo. Step two: install dependencies.</p>
</article></body></html>
"""

_NOTION_LOGIN_WALL_HTML = """
<html><body><div id="notion-app"></div>
<script>window.__NOTION_LOGIN_REQUIRED__ = true;</script>
</body></html>
"""


@respx.mock
def test_notion_fetcher_extracts_main_text(tmp_path):
    settings = Settings(project_root=tmp_path)
    respx.get("https://www.notion.so/Project-Notes-abc123").mock(
        return_value=httpx.Response(200, text=_NOTION_PAGE_HTML)
    )

    result = NotionFetcher(settings).fetch("https://www.notion.so/Project-Notes-abc123", "cid5")

    assert "onboarding process" in result.text
    assert "clone the repo" in result.text
    assert result.content_kind == "text"
    assert result.backend == "notion-fetch"


@respx.mock
def test_notion_fetcher_raises_clear_error_when_extraction_is_empty(tmp_path):
    settings = Settings(project_root=tmp_path)
    respx.get("https://www.notion.so/Private-Page-xyz").mock(
        return_value=httpx.Response(200, text=_NOTION_LOGIN_WALL_HTML)
    )

    with pytest.raises(TextFetchError, match="not public|empty|login"):
        NotionFetcher(settings).fetch("https://www.notion.so/Private-Page-xyz", "cid6")


@respx.mock
def test_notion_fetcher_raises_clear_error_on_http_failure(tmp_path):
    settings = Settings(project_root=tmp_path)
    respx.get("https://www.notion.so/Missing-Page").mock(return_value=httpx.Response(404))

    with pytest.raises(TextFetchError, match="404"):
        NotionFetcher(settings).fetch("https://www.notion.so/Missing-Page", "cid7")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_text_fetcher.py -k Notion -v`
Expected: FAIL with `ImportError: cannot import name 'NotionFetcher'`

- [ ] **Step 4: Write minimal implementation**

Add to `src/reel_pipeline/text_fetcher.py` (after `GitHubFetcher`):

```python
import trafilatura


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
```

Move the `import trafilatura` line to the top of the file with the other imports (`import base64`, `from urllib.parse import urlparse`, `import httpx`) rather than leaving it inline - shown separately above only to make the diff clear.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_text_fetcher.py -v`
Expected: PASS (7 tests total)

- [ ] **Step 6: Run lint and typecheck**

Run: `uv run ruff check . && uv run pyright`
Expected: no errors (watch for the moved `import trafilatura` placement triggering an import-order lint error - keep it alphabetized with the other third-party imports)

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/reel_pipeline/text_fetcher.py tests/test_text_fetcher.py
git commit -m "feat: add NotionFetcher using trafilatura for public-page text extraction"
```

---

## Task 6: TextFetcher dispatcher

**Files:**
- Modify: `src/reel_pipeline/text_fetcher.py`
- Test: `tests/test_text_fetcher.py`

**Interfaces:**
- Consumes: `GitHubFetcher`, `NotionFetcher` (Tasks 4-5).
- Produces: `TextFetcher` Protocol with `fetch(self, url: str, content_id: str) -> TranscriptResult`, `DispatchingTextFetcher` concrete class, `get_text_fetcher(settings: Settings) -> TextFetcher` factory - mirrors `Downloader`/`get_downloader()` in `downloader.py` exactly.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_text_fetcher.py`:

```python
from reel_pipeline.text_fetcher import DispatchingTextFetcher, get_text_fetcher


def test_dispatching_text_fetcher_routes_github_to_github_fetcher(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)
    calls = []
    monkeypatch.setattr(
        GitHubFetcher,
        "fetch",
        lambda self, url, content_id: (
            calls.append(("github", url)) or TranscriptResultStub(content_id)
        ),
    )
    monkeypatch.setattr(
        NotionFetcher,
        "fetch",
        lambda self, url, content_id: pytest.fail("Notion should not handle github.com"),
    )

    DispatchingTextFetcher(settings).fetch("https://github.com/owner/repo", "cid8")

    assert calls == [("github", "https://github.com/owner/repo")]


def test_dispatching_text_fetcher_routes_notion_to_notion_fetcher(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)
    calls = []
    monkeypatch.setattr(
        NotionFetcher,
        "fetch",
        lambda self, url, content_id: (
            calls.append(("notion", url)) or TranscriptResultStub(content_id)
        ),
    )
    monkeypatch.setattr(
        GitHubFetcher,
        "fetch",
        lambda self, url, content_id: pytest.fail("GitHub should not handle notion.so"),
    )

    DispatchingTextFetcher(settings).fetch("https://www.notion.so/Some-Page-abc", "cid9")

    assert calls == [("notion", "https://www.notion.so/Some-Page-abc")]


def test_dispatching_text_fetcher_raises_on_unrecognized_domain(tmp_path):
    settings = Settings(project_root=tmp_path)

    with pytest.raises(TextFetchError, match="unrecognized"):
        DispatchingTextFetcher(settings).fetch("https://example.com/x", "cid10")


def test_get_text_fetcher_returns_dispatching_instance(tmp_path):
    settings = Settings(project_root=tmp_path)
    assert isinstance(get_text_fetcher(settings), DispatchingTextFetcher)
```

Add this small helper near the top of the test file (after the imports), used only by the dispatcher tests above:

```python
def TranscriptResultStub(content_id):
    from reel_pipeline.models import TranscriptResult

    return TranscriptResult(content_id=content_id, text="stub", backend="stub")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_text_fetcher.py -k dispatch -v`
Expected: FAIL with `ImportError: cannot import name 'DispatchingTextFetcher'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/reel_pipeline/text_fetcher.py` (after `NotionFetcher`, at the end of the file):

```python
from typing import Protocol


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
```

Move the `from typing import Protocol` import to the top of the file with the other stdlib imports (shown separately above only to make the diff clear).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_text_fetcher.py -v`
Expected: PASS (11 tests total)

- [ ] **Step 5: Run lint and typecheck**

Run: `uv run ruff check . && uv run pyright`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/reel_pipeline/text_fetcher.py tests/test_text_fetcher.py
git commit -m "feat: add DispatchingTextFetcher and get_text_fetcher() factory"
```

---

## Task 7: Enrichment prompt selection

**Files:**
- Create: `config/prompts/enrich_text_capture.md`
- Modify: `src/reel_pipeline/enricher.py`
- Test: `tests/test_enricher.py`

**Interfaces:**
- Consumes: `TranscriptResult.content_kind` (Task 1).
- Produces: `Enricher.enrich()` behavior change only - same public signature (`enrich(self, transcript: TranscriptResult, source_url: str) -> EnrichmentResult`), now selects between two already-loaded prompt templates based on `transcript.content_kind`.

- [ ] **Step 1: Write the failing test**

First check whether `tests/test_enricher.py` already exists:

Run: `ls tests/test_enricher.py 2>/dev/null || echo "does not exist"`

If it doesn't exist, create `tests/test_enricher.py` with this content. If it exists, add these two test functions to it (matching its existing imports/fixture style):

```python
from __future__ import annotations

import httpx
import respx

from reel_pipeline.config import Settings
from reel_pipeline.enricher import Enricher
from reel_pipeline.models import TranscriptResult


@respx.mock
def test_enrich_uses_transcript_prompt_for_media_content(tmp_path):
    settings = Settings(project_root=tmp_path, llm={"provider": "ollama"})
    captured = {}

    def capture(request):
        import json

        captured["prompt"] = json.loads(request.content)["prompt"]
        return httpx.Response(
            200,
            json={
                "response": '{"title": "T", "summary": "S", "tags": [], '
                '"tools_mentioned": [], "key_takeaways": [], "high_signal": false, '
                '"skill_candidate_reason": null}'
            },
        )

    respx.post("http://localhost:11434/api/generate").mock(side_effect=capture)

    transcript = TranscriptResult(
        content_id="cid1", text="spoken words here", content_kind="media", backend="fake"
    )
    Enricher(settings).enrich(transcript, "https://youtube.com/watch?v=abc")

    assert "video/reel transcript" in captured["prompt"]


@respx.mock
def test_enrich_uses_text_capture_prompt_for_text_content(tmp_path):
    settings = Settings(project_root=tmp_path, llm={"provider": "ollama"})
    captured = {}

    def capture(request):
        import json

        captured["prompt"] = json.loads(request.content)["prompt"]
        return httpx.Response(
            200,
            json={
                "response": '{"title": "T", "summary": "S", "tags": [], '
                '"tools_mentioned": [], "key_takeaways": [], "high_signal": false, '
                '"skill_candidate_reason": null}'
            },
        )

    respx.post("http://localhost:11434/api/generate").mock(side_effect=capture)

    transcript = TranscriptResult(
        content_id="cid2", text="# repo\n\nA CLI tool.", content_kind="text", backend="github-api"
    )
    Enricher(settings).enrich(transcript, "https://github.com/owner/repo")

    assert "reference" in captured["prompt"].lower() or "README" in captured["prompt"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enricher.py -v`
Expected: FAIL - `test_enrich_uses_text_capture_prompt_for_text_content` fails because the current single-template `Enricher` always renders `enrich_transcript.md`, which contains neither "reference" nor "README"

- [ ] **Step 3: Write the new prompt file**

Create `config/prompts/enrich_text_capture.md`:

```markdown
You are an analyst turning a captured reference document (a GitHub repo/file, or a Notion page) into structured knowledge-base metadata.

Source URL: {{source_url}}

Captured content:
"""
{{transcript}}
"""

This is reference/documentation content, not a spoken transcript - it may be
a README, source file, or a Notion doc. Read it carefully and respond with
**only** a single JSON object (no markdown fences, no commentary before or
after) with exactly these keys:

- `title` (string): a concise, specific title for this content (max ~12 words).
- `summary` (string): a 2-4 sentence summary of what this is and what it does.
- `tags` (array of strings): 3-8 short lowercase-kebab-case topic tags.
- `tools_mentioned` (array of strings): names of any tools, products, apps,
  libraries, or services explicitly mentioned (for a GitHub repo, include the
  repo's own name). Empty array if none.
- `key_takeaways` (array of strings): 3-6 concrete, standalone takeaways a
  reader could act on without opening the source (e.g. what problem this
  solves, how to use it, what makes it notable).
- `high_signal` (boolean): true only if this is a genuinely useful,
  reusable tool, technique, or reference (not a trivial or abandoned
  project) that would be worth turning into a reusable skill.
- `skill_candidate_reason` (string or null): if `high_signal` is true, a
  one-sentence explanation of what reusable capability this content offers
  and why it's worth capturing as a skill. Null if `high_signal` is false.

Rules:
- Output valid JSON only. Do not wrap it in markdown code fences.
- Never invent tools, facts, or takeaways that are not supported by the content.
- If the content is too short or too low-signal to summarize meaningfully,
  still return valid JSON with your best-effort title/summary and an empty or
  minimal `tags`/`key_takeaways`, and set `high_signal` to false.
```

- [ ] **Step 4: Write minimal implementation**

Replace `src/reel_pipeline/enricher.py`'s `Enricher` class:

```python
class Enricher:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self._client = client
        self._transcript_prompt_template = settings.enrich_transcript_prompt.read_text(
            encoding="utf-8"
        )
        self._text_capture_prompt_template = settings.enrich_text_capture_prompt.read_text(
            encoding="utf-8"
        )

    def enrich(self, transcript: TranscriptResult, source_url: str) -> EnrichmentResult:
        template = (
            self._text_capture_prompt_template
            if transcript.content_kind == "text"
            else self._transcript_prompt_template
        )
        prompt = render_template(
            template,
            source_url=source_url,
            transcript=transcript.text,
        )
        try:
            raw_text = call_llm(
                self.settings,
                prompt,
                model=self.settings.enrichment.model,
                max_tokens=self.settings.enrichment.max_tokens,
                client=self._client,
            )
        except LlmCallError as exc:
            raise EnrichmentError(str(exc)) from exc
        data = _extract_json(raw_text)
        return EnrichmentResult(**data)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_enricher.py -v`
Expected: PASS (2 tests, plus any pre-existing tests in the file if it already existed)

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass

- [ ] **Step 7: Commit**

```bash
git add config/prompts/enrich_text_capture.md src/reel_pipeline/enricher.py tests/test_enricher.py
git commit -m "feat: select enrichment prompt by content_kind, add enrich_text_capture.md"
```

---

## Task 8: Worker integration

**Files:**
- Modify: `src/reel_pipeline/worker.py`
- Modify: `tests/test_worker_flow.py`

**Interfaces:**
- Consumes: `TextFetcher`, `get_text_fetcher()` (Task 6), `StateRecord.content_kind` (Task 1).
- Produces: `WorkerPipeline.__init__` gains a required `text_fetcher: TextFetcher` parameter (breaking constructor change - every call site must be updated in this task). `build_worker()` wires `get_text_fetcher(settings)` in.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_worker_flow.py` (near the other `Fake*` classes):

```python
class FakeTextFetcher:
    def __init__(self):
        self.calls = []

    def fetch(self, url: str, content_id: str) -> TranscriptResult:
        self.calls.append(url)
        return TranscriptResult(
            content_id=content_id,
            text="# repo\n\nA CLI tool that automates a repeatable build workflow.",
            content_kind="text",
            backend="github-api",
        )
```

Update `build_pipeline()` to accept and pass a `text_fetcher`:

```python
def build_pipeline(
    settings, downloader, transcriber=None, image_describer=None, text_fetcher=None
) -> WorkerPipeline:
    return WorkerPipeline(
        settings=settings,
        queue_manager=QueueManager(settings),
        downloader=downloader,
        transcriber=transcriber or FakeTranscriber(),
        image_describer=image_describer or FakeImageDescriber(),
        text_fetcher=text_fetcher or FakeTextFetcher(),
        enricher=FakeEnricher(),
        skill_writer=FakeSkillWriter(settings),
    )
```

Add a new test function:

```python
def test_text_capture_item_routes_to_text_fetcher_not_downloader(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        download=DownloadConfig(allowed_domains=["youtube.com"]),
        text_capture=TextCaptureConfig(allowed_domains=["github.com"]),
    )
    # FailingDownloader (defined earlier in this file) raises on any call - if the
    # worker mistakenly routed this text item through Downloader, the item would
    # end up FAILED instead of DONE, which the assertions below would catch.
    text_fetcher = FakeTextFetcher()
    pipeline = build_pipeline(settings, FailingDownloader(), text_fetcher=text_fetcher)
    pipeline.queue_manager.queue_file.write_text(
        "https://github.com/owner/repo\n", encoding="utf-8"
    )

    summary = pipeline.run_once()

    assert summary.done == 1
    assert text_fetcher.calls == ["https://github.com/owner/repo"]
```

Add the necessary import at the top of `tests/test_worker_flow.py`:

```python
from reel_pipeline.config import DownloadConfig, MaintenanceConfig, Settings, TextCaptureConfig
```

(merge `TextCaptureConfig` into the existing `reel_pipeline.config` import line rather than duplicating it)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_worker_flow.py -k text_capture -v`
Expected: FAIL with `TypeError: WorkerPipeline.__init__() got an unexpected keyword argument 'text_fetcher'`

- [ ] **Step 3: Write minimal implementation**

In `src/reel_pipeline/worker.py`, update imports:

```python
from reel_pipeline.text_fetcher import TextFetcher, get_text_fetcher
```

Update `WorkerPipeline.__init__`:

```python
    def __init__(
        self,
        settings: Settings,
        queue_manager: QueueManager,
        downloader: Downloader,
        transcriber: Transcriber,
        image_describer: ImageDescriber,
        text_fetcher: TextFetcher,
        enricher: EnrichmentProvider,
        skill_writer: SkillGenerator,
    ):
        self.settings = settings
        self.queue_manager = queue_manager
        self.downloader = downloader
        self.transcriber = transcriber
        self.image_describer = image_describer
        self.text_fetcher = text_fetcher
        self.enricher = enricher
        self.skill_writer = skill_writer
```

Update `process_item()` - replace the download+transcribe block:

```python
def process_item(self, record: StateRecord) -> StateRecord:
    try:
        if record.content_kind == "text":
            record.status = ItemStatus.DOWNLOADING
            self.queue_manager.update_record(record)
            transcript = self.text_fetcher.fetch(record.url, record.content_id)
            log_context(logger, 20, "text captured", content_id=record.content_id, url=record.url)
            record.status = ItemStatus.TRANSCRIBING
            self.queue_manager.update_record(record)
        else:
            record.status = ItemStatus.DOWNLOADING
            self.queue_manager.update_record(record)
            download_result = self.downloader.download(record.url, record.content_id)
            log_context(logger, 20, "downloaded", content_id=record.content_id, url=record.url)

            record.status = ItemStatus.TRANSCRIBING
            self.queue_manager.update_record(record)
            media_paths = [Path(p) for p in download_result.media_paths]
            if download_result.media_type is MediaType.IMAGE:
                transcript = self.image_describer.describe(media_paths, record.content_id)
            else:
                transcript = self._transcribe_media_paths(media_paths, record.content_id)
            log_context(
                logger,
                20,
                "transcribed",
                content_id=record.content_id,
                media_type=download_result.media_type.value,
            )

        record.status = ItemStatus.ENRICHING
        self.queue_manager.update_record(record)
        enrichment = self.enricher.enrich(transcript, record.url)
        log_context(
            logger,
            20,
            "enriched",
            content_id=record.content_id,
            high_signal=enrichment.high_signal,
        )

        record.status = ItemStatus.WRITING_NOTE
        self.queue_manager.update_record(record)
        content_item = ContentItem(
            content_id=record.content_id,
            source_url=record.url,
            created_at=datetime.now(UTC),
            transcript=transcript,
            enrichment=enrichment,
        )
        note_path = write_note(self.settings, content_item)
        skill_path = self.skill_writer.generate(content_item)

        previous_note_path = record.note_path
        previous_skill_path = record.skill_path
        record.status = ItemStatus.DONE
        record.note_path = str(note_path)
        record.skill_path = str(skill_path) if skill_path else None
        record.error = None
        log_context(
            logger,
            20,
            "note written",
            content_id=record.content_id,
            note_path=str(note_path),
        )
        self._cleanup_tmp_dir(record.content_id)
        self._cleanup_stale_note(previous_note_path, record.note_path)
        self._cleanup_stale_skill(previous_skill_path, record.skill_path)

    except Exception as exc:  # noqa: BLE001 - any stage failure must be recorded, not raised
        new_error = str(exc)
        previous_error = record.error
        record.status = ItemStatus.FAILED
        record.error = new_error
        if new_error != previous_error:
            reason = f"processing failed: {new_error}"
            self.queue_manager.append_needs_attention(record.url, reason)
        log_context(logger, 40, "processing failed", content_id=record.content_id, error=new_error)

    self.queue_manager.update_record(record)
    return record
```

(Everything from `record.status = ItemStatus.ENRICHING` onward, and the whole `except` block, are unchanged from the current file - only the `try:` block's opening is restructured into the `if record.content_kind == "text":` branch.)

Update `build_worker()`:

```python
def build_worker(settings: Settings) -> WorkerPipeline:
    """Wire up the real (non-fake) implementations for CLI/webhook use."""
    return WorkerPipeline(
        settings=settings,
        queue_manager=QueueManager(settings),
        downloader=get_downloader(settings),
        transcriber=get_transcriber(settings),
        image_describer=get_image_describer(settings),
        text_fetcher=get_text_fetcher(settings),
        enricher=Enricher(settings),
        skill_writer=SkillWriter(settings),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_worker_flow.py -v`
Expected: PASS (all tests, including the new one)

- [ ] **Step 5: Run the full test suite, lint, and typecheck**

Run: `uv run pytest -q && uv run ruff check . && uv run pyright`
Expected: all pass, 0 errors

- [ ] **Step 6: Commit**

```bash
git add src/reel_pipeline/worker.py tests/test_worker_flow.py
git commit -m "feat: route content_kind=text items through TextFetcher instead of Downloader"
```

---

## Task 9: Documentation

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/runbook.md`
- Modify: `docs/acceptance-tests.md`
- Modify: `.env.example`

**Interfaces:** None (documentation only).

- [ ] **Step 1: Update CLAUDE.md**

In the "Risk posture" section, add after the `drive.google.com` bullet:

```markdown
- Text-capture ingestion (GitHub repos/files, public Notion pages) is enabled as of
  2026-07-16, scoped to `github.com`, `notion.so`, and `notion.site` in
  `text_capture.allowed_domains` - a separate allow-list from `download.allowed_domains`,
  since these aren't media platforms and go through `text_fetcher.py` (GitHub's public
  REST API, plain HTTP GET + `trafilatura` extraction for Notion) instead of
  yt-dlp/gallery-dl. Public links only - no OAuth or API keys, matching the project's
  no-account-automation posture. Airtable was explicitly evaluated and excluded: its
  public share views require JS execution to render real content, which conflicts with
  the no-browser-automation rule above. See
  `docs/superpowers/specs/2026-07-16-text-capture-ingestion-design.md`.
```

- [ ] **Step 2: Update README.md**

In the intro paragraph, update the "No browser automation..." line to mention text capture:

```markdown
No browser automation. No hardcoded secrets. No social-platform scraping by
default - YouTube/TikTok/X/Vimeo work anonymously; Instagram, Facebook,
LinkedIn, and Google Drive (video files only) are each a deliberate,
cookie-authenticated opt-in (see `docs/runbook.md`). GitHub and public Notion
pages are captured as text via a separate, public-links-only path (no
credentials needed). See [`docs/architecture.md`](docs/architecture.md) for
```

In "Safety posture", add a new bullet after the Instagram/Facebook/LinkedIn/Drive one:

```markdown
- GitHub repos/files and public Notion pages are captured as text (not downloaded as media) via a separate `text_capture.allowed_domains` list and `text_fetcher.py` - public links only, no credentials of any kind. Airtable was evaluated and excluded (its share views need JS execution to render, conflicting with the no-browser-automation rule).
```

- [ ] **Step 3: Update docs/architecture.md**

Add a new row to the module table (after the `image_describer.py` row):

```markdown
| `text_fetcher.py` | Text-content capture for non-media sources - GitHub (public REST API: repo metadata + README, or a specific file for a `blob/` URL) and public Notion pages (plain HTTP GET + `trafilatura` extraction, no JS execution). Produces the same `TranscriptResult` shape `transcriber.py`/`image_describer.py` do |
```

Update the data-flow diagram's branch comment (the `-> branch on media_type:` block) to note the earlier text/media split:

```markdown
    -> classify_url_kind() decided text vs. media at registration time (validators.py):
         text  -> TextFetcher.fetch(url)                     -> TranscriptResult
         media -> Downloader.download(url) -> DownloadResult -> branch on media_type:
                    VIDEO -> Transcriber.transcribe(media_paths[0])      -> TranscriptResult
                    IMAGE -> ImageDescriber.describe(media_paths)        -> TranscriptResult (vision-model description)
```

In "Safety guardrails", add a new bullet:

```markdown
- GitHub and public Notion pages are captured as text via a separate,
  public-links-only path (`text_capture.allowed_domains`, disjoint from
  `download.allowed_domains`) - no OAuth, no API keys, no browser automation.
  Airtable was evaluated and excluded: its public share views are JS-rendered
  React apps, which would require actual browser automation to scrape - a
  hard no per this file's first guardrail above. See
  `docs/superpowers/specs/2026-07-16-text-capture-ingestion-design.md`.
```

- [ ] **Step 4: Update docs/runbook.md**

Add a new section, mirroring the structure of the existing "Instagram setup" / "Facebook and LinkedIn setup" sections (find those sections first to match heading level and tone):

Run: `grep -n "^## " docs/runbook.md`

Then add a new `## GitHub and Notion text capture` section after the Facebook/LinkedIn one, containing:

```markdown
## GitHub and Notion text capture

GitHub repo/file links and public Notion pages are captured as text instead
of downloaded as media - no setup needed, since both use public-only access:

- **GitHub**: uses GitHub's public REST API, unauthenticated (60 requests/hour,
  2 calls per URL - comfortably enough for personal sharing volume). Works for
  any public repo, either the repo root or a link to one specific file
  (`.../blob/<branch>/<path>`).
- **Notion**: fetches the public share-page URL directly and extracts the main
  text content. Only works for pages actually shared as "public" (Notion's
  "Share to web" toggle) - a private/workspace-only page will fail with a
  clear "not public" error rather than silently producing an empty note.

Neither requires any environment variable or credential. Airtable links are
not supported (see `docs/architecture.md`'s Safety guardrails section for why)
and continue to route to `needs-attention.txt`.
```

- [ ] **Step 5: Update docs/acceptance-tests.md**

Add a new numbered section (following the existing pattern - check the highest current number first):

Run: `grep -n "^## " docs/acceptance-tests.md | tail -3`

Add a new section using the next available number:

```markdown
## 12. Text-capture ingestion (GitHub, Notion)

- [ ] A `github.com/owner/repo` URL is classified as `content_kind=text` and
      routed to `TextFetcher`, never `Downloader`.
      Automated: `test_text_capture_item_routes_to_text_fetcher_not_downloader`.
- [ ] A GitHub repo-root URL captures metadata + README; a
      `.../blob/<ref>/<path>` URL captures that specific file's content instead.
      Automated: `test_github_fetcher_fetches_repo_root_metadata_and_readme`,
      `test_github_fetcher_fetches_specific_file_not_readme`.
- [ ] A private/nonexistent GitHub repo fails with a clear error, not a crash.
      Automated: `test_github_fetcher_raises_clear_error_on_404`.
- [ ] A public Notion page's main text content is extracted correctly.
      Automated: `test_notion_fetcher_extracts_main_text`.
- [ ] A non-public Notion page (login wall) fails with a clear error rather
      than producing a near-empty note.
      Automated: `test_notion_fetcher_raises_clear_error_when_extraction_is_empty`.
- [ ] Enrichment selects `enrich_text_capture.md` for `content_kind=text` items
      and `enrich_transcript.md` for everything else.
      Automated: `test_enrich_uses_text_capture_prompt_for_text_content`,
      `test_enrich_uses_transcript_prompt_for_media_content`.
- [ ] A real public GitHub repo and a real public Notion page each produce a
      working Obsidian note end-to-end. **(manual)**
```

- [ ] **Step 6: Update .env.example**

Add a comment near the bottom, after the yt-dlp cookies section, documenting the deliberate absence of new secrets:

```bash

# GitHub and Notion text capture (github.com, notion.so/notion.site links)
# need no configuration here - both are public-links-only, no API keys or
# OAuth. See docs/runbook.md's "GitHub and Notion text capture" section.
```

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md README.md docs/architecture.md docs/runbook.md docs/acceptance-tests.md .env.example
git commit -m "docs: document the text-capture ingestion path (GitHub, Notion)"
```

---

## Final verification (after all tasks)

- [ ] Run the full project checklist from `CLAUDE.md`:

```bash
uv lock --check
uv run ruff check .
uv run pyright
uv run pytest -q
uv audit
```

Expected: all clean, matching the standard established for every prior platform addition in this project.

- [ ] Manually verify with a real GitHub URL and a real public Notion page (append to `data/inbox/queue.txt`, run `uv run python -m reel_pipeline.cli run-once`, confirm a note lands in the vault) - this is the one thing automated tests can't cover, per `docs/acceptance-tests.md`'s existing convention for `**(manual)**` items.
