from __future__ import annotations

import asyncio
import base64
import http.server
import socket
import sys
import threading

import httpcore
import httpx
import pytest
import respx

from reel_pipeline.config import RenderFallbackConfig, Settings, TextCaptureConfig
from reel_pipeline.text_fetcher import (
    _CHROMIUM_NO_DIRECT_WEBRTC,
    DispatchingTextFetcher,
    DriveFetcher,
    GenericHtmlFetcher,
    GitHubFetcher,
    NotionFetcher,
    RenderedHtmlFetcher,
    TextFetchError,
    _guarded_client,
    _running_public_only_proxy,
    get_text_fetcher,
)


def _settings_with_render_fallback(tmp_path, *, enabled):
    return Settings(
        project_root=tmp_path,
        text_capture=TextCaptureConfig(render_fallback=RenderFallbackConfig(enabled=enabled)),
    )


class _FakePage:
    def __init__(self, html="", *, timeout=False, error=None):
        self._html = html
        self._timeout = timeout
        self._error = error
        self.route_handler = None
        self.url = "https://example.com/app"

    def route(self, pattern, handler):
        self.route_handler = handler

    def goto(self, url, wait_until=None, timeout=None):
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        if self._timeout:
            raise PlaywrightTimeoutError("Timeout exceeded")
        if self._error:
            raise PlaywrightError(self._error)

    def wait_for_timeout(self, ms):
        pass

    def content(self):
        return self._html


class _FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False
        self.launch_kwargs = {}

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, browser=None, launch_error=None):
        self._browser = browser
        self._launch_error = launch_error

    def launch(self, **kwargs):
        if self._launch_error is not None:
            raise self._launch_error
        assert self._browser is not None
        self._browser.launch_kwargs = kwargs
        return self._browser


class _FakePlaywrightHandle:
    def __init__(self, chromium):
        self.chromium = chromium


class _FakeSyncPlaywright:
    def __init__(self, chromium):
        self._chromium = chromium

    def __enter__(self):
        return _FakePlaywrightHandle(self._chromium)

    def __exit__(self, exc_type, exc, tb):
        return False


def _install_fake_playwright(
    monkeypatch, *, html="", timeout=False, goto_error=None, launch_error=None
):
    import playwright.sync_api as playwright_api

    page = _FakePage(html, timeout=timeout, error=goto_error)
    browser = _FakeBrowser(page)
    chromium = _FakeChromium(browser=browser, launch_error=launch_error)
    monkeypatch.setattr(playwright_api, "sync_playwright", lambda: _FakeSyncPlaywright(chromium))
    return browser


def _notion_block(block_id, block_type, title=None, content=None):
    value = {"id": block_id, "type": block_type}
    if title is not None:
        value["properties"] = {"title": title}
    if content is not None:
        value["content"] = content
    return {"value": {"value": value}}


def _notion_load_page_chunk_response(blocks: dict) -> dict:
    return {"cursor": {"stack": []}, "recordMap": {"block": blocks}}


def _readme_response(content: str) -> dict:
    return {"content": base64.b64encode(content.encode()).decode(), "encoding": "base64"}


def TranscriptResultStub(content_id):
    from reel_pipeline.models import TranscriptResult

    return TranscriptResult(content_id=content_id, text="stub", backend="stub")


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
def test_github_fetcher_absolutizes_relative_readme_links(tmp_path):
    # Regression test: a README's relative links/images point at sibling files
    # in that repo, not at anything in the Obsidian vault. Left relative, the
    # note-writer embeds them verbatim and Obsidian creates blank stub notes
    # for paths like CONTRIBUTING.md at the vault root (observed in practice).
    settings = Settings(project_root=tmp_path)
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "owner/repo",
                "description": "desc",
                "stargazers_count": 1,
                "language": "Python",
                "topics": [],
                "default_branch": "develop",
            },
        )
    )
    readme = (
        "# repo\n\n"
        "See the [Contributing Guide](CONTRIBUTING.md) and [LICENSE](LICENSE).\n"
        "![badge](./docs/badge.svg)\n"
        "Already absolute: [docs](https://example.com/docs)\n"
        "Anchor only: [section](#usage)\n"
    )
    respx.get("https://api.github.com/repos/owner/repo/readme").mock(
        return_value=httpx.Response(200, json=_readme_response(readme))
    )

    result = GitHubFetcher(settings).fetch("https://github.com/owner/repo", "cid-links")

    assert "(https://github.com/owner/repo/blob/develop/CONTRIBUTING.md)" in result.text
    assert "(https://github.com/owner/repo/blob/develop/LICENSE)" in result.text
    assert "(https://raw.githubusercontent.com/owner/repo/develop/docs/badge.svg)" in result.text
    assert "(https://example.com/docs)" in result.text
    assert "(#usage)" in result.text


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
def test_github_fetcher_fetches_file_at_requested_ref_not_default_branch(tmp_path):
    """Regression test: the ref segment of a "blob/<ref>/<path>" URL used to be
    discarded, so a URL pinned to a tag/branch silently fetched the file from
    the repo's default branch instead.
    """
    settings = Settings(project_root=tmp_path)
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "owner/repo",
                "description": "A tool that does things.",
                "default_branch": "main",
                "stargazers_count": 1,
                "language": None,
                "topics": [],
            },
        )
    )
    respx.get(
        "https://api.github.com/repos/owner/repo/contents/docs/guide.md", params={"ref": "v2.0"}
    ).mock(return_value=httpx.Response(200, json=_readme_response("# Guide\n\nv2.0 content.")))

    result = GitHubFetcher(settings).fetch(
        "https://github.com/owner/repo/blob/v2.0/docs/guide.md", "cid-ref"
    )

    assert "v2.0 content." in result.text


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

# A real-world case found via manual verification (see docs/superpowers/plans -
# roadmap.notion.site returns exactly this shape): a client-rendered app shell
# whose only server-sent text is a <noscript> fallback message, which
# trafilatura extracts as non-empty "content" - the empty-string check alone
# missed this, silently producing a garbage note before this test was added.
_NOTION_JS_REQUIRED_HTML = """
<!doctype html><html><head><title>Notion</title></head>
<body><div id="notion-app"></div>
<noscript>JavaScript must be enabled in order to use Notion.
Please enable JavaScript to continue.</noscript>
</body></html>
"""


@respx.mock
def test_notion_fetcher_extracts_main_text(tmp_path):
    settings = Settings(project_root=tmp_path)
    respx.get("https://www.notion.so/Project-Notes-abc123").mock(
        return_value=httpx.Response(200, text=_NOTION_PAGE_HTML)
    )

    result = GenericHtmlFetcher(settings).fetch(
        "https://www.notion.so/Project-Notes-abc123", "cid5"
    )

    assert "onboarding process" in result.text
    assert "clone the repo" in result.text
    assert result.content_kind == "text"
    assert result.backend == "html-fetch"


@respx.mock
def test_generic_html_fetcher_retries_403_with_bot_user_agent(tmp_path):
    """Wikimedia 403s the browser UA but serves a descriptive bot UA."""
    settings = Settings(project_root=tmp_path)
    route = respx.get("https://en.wikipedia.org/wiki/X").mock(
        side_effect=lambda req: httpx.Response(
            200 if req.headers["User-Agent"].startswith("reel-pipeline/") else 403,
            text=_NOTION_PAGE_HTML,
        )
    )

    result = GenericHtmlFetcher(settings).fetch("https://en.wikipedia.org/wiki/X", "cid-ua")

    assert "onboarding process" in result.text
    assert route.call_count == 2


@respx.mock
def test_google_doc_uses_text_export(tmp_path):
    settings = Settings(project_root=tmp_path)
    export = "https://docs.google.com/document/d/AbC-1_x/export?format=txt"
    respx.get(export).mock(
        return_value=httpx.Response(
            200, text="﻿Startup team guide", headers={"content-type": "text/plain"}
        )
    )
    fetcher = GenericHtmlFetcher(settings)

    result = fetcher.fetch("https://docs.google.com/document/d/AbC-1_x/edit?usp=sharing", "c")
    assert result.text == "Startup team guide"
    assert result.backend == "gdocs-export"

    respx.get(export).mock(
        return_value=httpx.Response(
            200, text="<html>Sign in</html>", headers={"content-type": "text/html"}
        )
    )
    with pytest.raises(TextFetchError, match="isn't public"):
        fetcher.fetch("https://docs.google.com/document/d/AbC-1_x/edit", "c")


@respx.mock
def test_notion_fetcher_raises_clear_error_when_extraction_is_empty(tmp_path):
    # render_fallback disabled: this test targets the plain-GET/app-shell
    # heuristic specifically, not the render fallback (covered separately
    # below) - a real Chromium launch has no place in this unit test.
    settings = _settings_with_render_fallback(tmp_path, enabled=False)
    respx.get("https://www.notion.so/Private-Page-xyz").mock(
        return_value=httpx.Response(200, text=_NOTION_LOGIN_WALL_HTML)
    )

    with pytest.raises(TextFetchError, match="not public|empty|login"):
        GenericHtmlFetcher(settings).fetch("https://www.notion.so/Private-Page-xyz", "cid6")


@respx.mock
def test_notion_fetcher_raises_clear_error_for_js_rendered_app_shell(tmp_path):
    """Regression test for a real page found during manual verification: the
    server HTML is a client-rendered app shell with only a <noscript> fallback
    message, which trafilatura extracts as non-empty text - the fetcher must
    still treat this as unusable, not as real page content.
    """
    settings = _settings_with_render_fallback(tmp_path, enabled=False)
    respx.get("https://roadmap.notion.site/").mock(
        return_value=httpx.Response(200, text=_NOTION_JS_REQUIRED_HTML)
    )

    with pytest.raises(TextFetchError, match="client-side"):
        GenericHtmlFetcher(settings).fetch("https://roadmap.notion.site/", "cid-js-shell")


@respx.mock
def test_generic_html_fetcher_falls_back_to_render_when_app_shell(tmp_path, monkeypatch):
    """render_fallback enabled (the default): an app-shell plain-GET result
    escalates to RenderedHtmlFetcher instead of raising immediately.
    """
    settings = Settings(project_root=tmp_path)
    respx.get("https://roadmap.notion.site/").mock(
        return_value=httpx.Response(200, text=_NOTION_JS_REQUIRED_HTML)
    )
    rendered_html = (
        "<html><body><article><p>Real roadmap content, only visible after "
        "JS runs, long enough to clear the app-shell length heuristic.</p>"
        "</article></body></html>"
    )
    _install_fake_playwright(monkeypatch, html=rendered_html)

    result = GenericHtmlFetcher(settings).fetch("https://roadmap.notion.site/", "cid-fallback")

    assert "Real roadmap content" in result.text
    assert result.backend == "playwright-render"


@respx.mock
def test_network_security_block_page_is_refused_not_captured(tmp_path):
    """Reddit's wording, seen live on 2026-11-01: 143 chars that came back as a
    successful render, stopped only by a length floor elsewhere.
    """
    block_html = (
        "<html><body><article><p>You've been blocked by network security. To continue, "
        "log in to your account or use your developer token. If you think you've been "
        "blocked by mistake, file a ticket below and we'll look into it.</p></article>"
        "</body></html>"
    )
    respx.get("https://example.com/gated").mock(return_value=httpx.Response(200, text=block_html))

    with pytest.raises(TextFetchError, match="bot-detection challenge"):
        GenericHtmlFetcher(Settings(project_root=tmp_path)).fetch(
            "https://example.com/gated", "cid-network-block"
        )


@respx.mock
def test_generic_html_fetcher_detects_bot_challenge_on_plain_get(tmp_path):
    """A challenge page served as a plain HTTP 200 - long/non-shell enough to
    pass the app-shell heuristic on its own - must still be rejected, not
    silently captured as real content (found by review, 2026-08-10: the
    original version only checked this on the *rendered* path).
    """
    settings = Settings(project_root=tmp_path)
    challenge_html = (
        "<html><body><article><p>Verification successful. Waiting for "
        "example.com to respond. Enable JavaScript and cookies to continue "
        "so we can confirm you are not a bot before granting access to this "
        "page and its content.</p></article></body></html>"
    )
    respx.get("https://example.com/gated").mock(
        return_value=httpx.Response(200, text=challenge_html)
    )

    with pytest.raises(TextFetchError, match="bot-detection challenge"):
        GenericHtmlFetcher(settings).fetch("https://example.com/gated", "cid-plain-challenge")


@respx.mock
def test_generic_html_fetcher_does_not_flag_incidental_phrase_in_long_article(tmp_path):
    """A challenge marker phrase appearing incidentally deep in a long
    legitimate article must not trip the check - only the opening slice is
    scanned (found by review, 2026-08-10: the original whole-document scan
    risked false positives on ordinary English phrases like "just a
    moment").
    """
    settings = Settings(project_root=tmp_path)
    padding = "This article is about something else entirely. " * 30
    html = (
        f"<html><body><article><p>{padding}"
        "Just a moment later, the story continues with more real content "
        "that has nothing to do with any bot check.</p></article></body></html>"
    )
    respx.get("https://example.com/article").mock(return_value=httpx.Response(200, text=html))

    result = GenericHtmlFetcher(settings).fetch("https://example.com/article", "cid-incidental")

    assert "something else entirely" in result.text


@respx.mock
def test_notion_fetcher_raises_clear_error_on_http_failure(tmp_path):
    settings = Settings(project_root=tmp_path)
    respx.get("https://www.notion.so/Missing-Page").mock(return_value=httpx.Response(404))
    respx.get("https://archive.org/wayback/available").mock(
        return_value=httpx.Response(200, json={"archived_snapshots": {}})
    )

    with pytest.raises(TextFetchError, match="404"):
        GenericHtmlFetcher(settings).fetch("https://www.notion.so/Missing-Page", "cid7")


@respx.mock
def test_notion_fetcher_extracts_page_title_and_block_text_in_order(tmp_path):
    """Uses the real loadPageChunk request/response shape (double-nested
    "value.value", the doubly-nested block wrapper Notion's API uses) - see
    NotionFetcher's docstring for how this was confirmed against a live page.
    """
    settings = Settings(project_root=tmp_path)
    page_id = "3ab66d04-debf-805d-949e-fcfb31f8837e"
    blocks = {
        page_id: _notion_block(
            page_id, "page", title=[["Project Notes"]], content=["child-1", "child-2"]
        ),
        "child-1": _notion_block("child-1", "text", title=[["First paragraph."]]),
        "child-2": _notion_block(
            "child-2", "text", title=[["Second paragraph, ", None], ["bold bit", [["b"]]]]
        ),
    }
    respx.post("https://app.notion.com/api/v3/loadPageChunk").mock(
        return_value=httpx.Response(200, json=_notion_load_page_chunk_response(blocks))
    )

    result = NotionFetcher(settings).fetch(
        f"https://www.notion.so/Project-Notes-{page_id.replace('-', '')}", "cid-notion1"
    )

    assert result.backend == "notion-api"
    assert result.content_kind == "text"
    lines = result.text.splitlines()
    assert lines[0] == "# Project Notes"
    assert "First paragraph." in result.text
    assert "Second paragraph, bold bit" in result.text
    # Content order is preserved (child-1 before child-2), not just presence.
    assert result.text.index("First paragraph.") < result.text.index("Second paragraph")


@respx.mock
def test_notion_fetcher_skips_image_block_filename_as_noise(tmp_path):
    settings = Settings(project_root=tmp_path)
    page_id = "3ab66d04-debf-805d-949e-fcfb31f8837e"
    blocks = {
        page_id: _notion_block(page_id, "page", title=[["Notes"]], content=["img-1", "text-1"]),
        "img-1": _notion_block("img-1", "image", title=[["photo.jpg"]]),
        "text-1": _notion_block("text-1", "text", title=[["Real content."]]),
    }
    respx.post("https://app.notion.com/api/v3/loadPageChunk").mock(
        return_value=httpx.Response(200, json=_notion_load_page_chunk_response(blocks))
    )

    result = NotionFetcher(settings).fetch(
        f"https://www.notion.so/Notes-{page_id.replace('-', '')}", "cid-notion2"
    )

    assert "photo.jpg" not in result.text
    assert "Real content." in result.text


@respx.mock
def test_notion_fetcher_notes_linked_database_without_expanding_rows(tmp_path):
    settings = Settings(project_root=tmp_path)
    page_id = "3ab66d04-debf-805d-949e-fcfb31f8837e"
    blocks = {
        page_id: _notion_block(page_id, "page", title=[["Notes"]], content=["db-1"]),
        "db-1": _notion_block("db-1", "collection_view", title=[["My Database"]]),
    }
    respx.post("https://app.notion.com/api/v3/loadPageChunk").mock(
        return_value=httpx.Response(200, json=_notion_load_page_chunk_response(blocks))
    )

    result = NotionFetcher(settings).fetch(
        f"https://www.notion.so/Notes-{page_id.replace('-', '')}", "cid-notion3"
    )

    assert "linked database: My Database" in result.text
    assert "contents not included" in result.text


@respx.mock
def test_notion_fetcher_ignores_cyclic_content_reference(tmp_path):
    """A block referencing an ancestor as a child (shouldn't happen in real
    Notion data, but defended against anyway) must not infinite-loop.
    """
    settings = Settings(project_root=tmp_path)
    page_id = "3ab66d04-debf-805d-949e-fcfb31f8837e"
    blocks = {
        page_id: _notion_block(page_id, "page", title=[["Notes"]], content=["child-1"]),
        "child-1": _notion_block("child-1", "text", title=[["Child text."]], content=[page_id]),
    }
    respx.post("https://app.notion.com/api/v3/loadPageChunk").mock(
        return_value=httpx.Response(200, json=_notion_load_page_chunk_response(blocks))
    )

    result = NotionFetcher(settings).fetch(
        f"https://www.notion.so/Notes-{page_id.replace('-', '')}", "cid-notion4"
    )

    assert "Child text." in result.text


@respx.mock
def test_notion_fetcher_raises_clear_error_when_page_not_found_or_not_public(tmp_path):
    """A nonexistent/private page id still returns HTTP 200 with no "block"
    key at all in recordMap - confirmed live against Notion's real API with
    an all-zeros page id. Must not be silently treated as an empty-but-valid
    page.
    """
    settings = Settings(project_root=tmp_path)
    page_id = "00000000-0000-0000-0000-000000000000"
    respx.post("https://app.notion.com/api/v3/loadPageChunk").mock(
        return_value=httpx.Response(
            200, json={"cursor": {"stack": []}, "recordMap": {"__version__": 3}}
        )
    )

    with pytest.raises(TextFetchError, match="not found or not public"):
        NotionFetcher(settings).fetch(f"https://www.notion.so/{page_id}", "cid-notion5")


def test_notion_fetcher_raises_clear_error_when_no_page_id_in_url(tmp_path):
    settings = Settings(project_root=tmp_path)

    with pytest.raises(TextFetchError, match="could not find a Notion page id"):
        NotionFetcher(settings).fetch("https://someworkspace.notion.site/", "cid-notion6")


def test_rendered_html_fetcher_extracts_text_from_rendered_dom(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)
    rendered_html = (
        "<html><body><article><p>Content that only exists after JS runs, well "
        "past the minimum plausible length so it clears the app-shell check.</p>"
        "</article></body></html>"
    )
    browser = _install_fake_playwright(monkeypatch, html=rendered_html)

    result = RenderedHtmlFetcher(settings).fetch("https://example.com/spa", "cid-render1")

    assert "Content that only exists after JS runs" in result.text
    assert result.backend == "playwright-render"
    assert result.content_kind == "text"
    assert browser.closed is True


def test_rendered_html_fetcher_raises_when_still_app_shell_after_render(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)
    _install_fake_playwright(monkeypatch, html="<html><body></body></html>")

    with pytest.raises(TextFetchError, match="no usable text even after rendering"):
        RenderedHtmlFetcher(settings).fetch("https://example.com/broken-spa", "cid-render2")


@pytest.mark.parametrize(
    "body",
    [
        "A required part of this site couldn’t load. This may be due to a browser "
        "extension, network issues, or browser settings. Please check your connection, "
        "disable any ad blockers, or try using a different browser.",
        "Application error: a client-side exception has occurred while loading this page, "
        "see the browser console for more information.",
        "Something went wrong. We are sorry, this page could not be displayed right now.",
    ],
)
def test_rendered_html_fetcher_rejects_a_thin_error_page(tmp_path, monkeypatch, body):
    settings = Settings(project_root=tmp_path)
    _install_fake_playwright(monkeypatch, html=f"<html><body><p>{body}</p></body></html>")

    with pytest.raises(TextFetchError, match="no usable text even after rendering"):
        RenderedHtmlFetcher(settings).fetch("https://example.com/broken", "cid-render-err")


def test_rendered_html_fetcher_keeps_a_long_article_that_mentions_an_error(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)
    body = "Something went wrong at the launch, and this is the story of why. " + (
        "We traced the outage step by step through every service involved. " * 12
    )
    _install_fake_playwright(monkeypatch, html=f"<html><body><p>{body}</p></body></html>")

    result = RenderedHtmlFetcher(settings).fetch("https://example.com/post", "cid-render-ok")

    assert result.backend == "playwright-render"


def test_rendered_html_fetcher_raises_on_bot_detection_challenge(tmp_path, monkeypatch):
    """Regression test for a real page found during live verification
    (roadmap.notion.site): the challenge text is long enough to clear the
    app-shell length check and generic enough to slip past the login-wall
    markers, so without this check it would be captured as if it were real
    content.
    """
    settings = Settings(project_root=tmp_path)
    challenge_html = (
        "<html><body><article><p>Verification successful. Waiting for "
        "example.com to respond\nEnable JavaScript and cookies to continue"
        "</p></article></body></html>"
    )
    _install_fake_playwright(monkeypatch, html=challenge_html)

    with pytest.raises(TextFetchError, match="bot-detection challenge"):
        RenderedHtmlFetcher(settings).fetch("https://example.com/gated-by-cf", "cid-render6")


def test_rendered_html_fetcher_raises_on_login_wall(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)
    login_html = (
        "<html><body><article><p>Please log in to continue viewing this "
        "content - you must sign in with an account to see the rest.</p>"
        "</article></body></html>"
    )
    _install_fake_playwright(monkeypatch, html=login_html)

    with pytest.raises(TextFetchError, match="requires login"):
        RenderedHtmlFetcher(settings).fetch("https://example.com/gated", "cid-render3")


def test_rendered_html_fetcher_raises_on_timeout(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)
    _install_fake_playwright(monkeypatch, timeout=True)

    with pytest.raises(TextFetchError, match="exceeded"):
        RenderedHtmlFetcher(settings).fetch("https://example.com/slow", "cid-render4")


def test_rendered_html_fetcher_raises_clear_error_when_chromium_not_installed(
    tmp_path, monkeypatch
):
    from playwright.sync_api import Error as PlaywrightError

    settings = Settings(project_root=tmp_path)
    _install_fake_playwright(
        monkeypatch,
        launch_error=PlaywrightError(
            "Executable doesn't exist at /path/to/chromium - run playwright install"
        ),
    )

    with pytest.raises(TextFetchError, match="Chromium not installed"):
        RenderedHtmlFetcher(settings).fetch("https://example.com/x", "cid-render5")


@respx.mock
def test_generic_html_fetcher_render_fallback_disabled_never_launches_browser(
    tmp_path, monkeypatch
):
    settings = _settings_with_render_fallback(tmp_path, enabled=False)
    respx.get("https://roadmap.notion.site/").mock(
        return_value=httpx.Response(200, text=_NOTION_JS_REQUIRED_HTML)
    )

    def _fail_if_launched():
        pytest.fail("render fallback must not launch a browser when disabled")

    import playwright.sync_api as playwright_api

    monkeypatch.setattr(playwright_api, "sync_playwright", _fail_if_launched)

    with pytest.raises(TextFetchError, match="client-side"):
        GenericHtmlFetcher(settings).fetch("https://roadmap.notion.site/", "cid-no-fallback")


@pytest.mark.playwright
def test_rendered_html_fetcher_correctly_rejects_real_unfetchable_page():
    """Opt-in, real (not mocked) Playwright render against the actual
    roadmap.notion.site failure this fallback was built for - proves the
    real thing works end to end without making every `uv run pytest` depend
    on network access or a Chromium binary. Run explicitly with
    `uv run pytest -m playwright`.

    Live verification (2026-08-10) found this page returns different
    unfetchable content on different runs - sometimes a bot-detection
    challenge ("Verification successful. Waiting for ... to respond"),
    sometimes a genuine "this page couldn't be found" after the JS app
    loads - the live site's exact response is outside this pipeline's
    control and not something a test should pin to one specific wording.
    What must hold on every run is the actual property this fallback
    exists for: a page it can't genuinely capture raises TextFetchError,
    not a silent capture of unusable boilerplate as if it were real
    content.
    """
    import tempfile
    from pathlib import Path

    from reel_pipeline.config import Settings as SettingsForLiveTest

    with tempfile.TemporaryDirectory() as tmp:
        settings = SettingsForLiveTest(project_root=Path(tmp))
        with pytest.raises(TextFetchError):
            RenderedHtmlFetcher(settings).fetch("https://roadmap.notion.site/", "cid-render-live")


@respx.mock
def test_drive_fetcher_extracts_plain_text_file(tmp_path):
    settings = Settings(project_root=tmp_path)
    respx.get("https://drive.google.com/uc?export=download&id=abc123").mock(
        return_value=httpx.Response(200, text="---\nname: some-skill\n---\nBody text here.")
    )

    result = DriveFetcher(settings).fetch(
        "https://drive.google.com/file/d/abc123/view?usp=drivesdk", "cid-drive1"
    )

    assert result.backend == "drive-download"
    assert result.content_kind == "text"
    assert "Body text here." in result.text


@respx.mock
def test_drive_fetcher_extracts_html_export_via_trafilatura(tmp_path):
    settings = Settings(project_root=tmp_path)
    html = (
        "<!doctype html><html><body><article>"
        "<h1>Doc Title</h1><p>Real exported content here.</p>"
        "</article></body></html>"
    )
    respx.get("https://drive.google.com/uc?export=download&id=abc456").mock(
        return_value=httpx.Response(200, text=html)
    )

    result = DriveFetcher(settings).fetch(
        "https://drive.google.com/file/d/abc456/view", "cid-drive2"
    )

    assert "Real exported content here." in result.text


@respx.mock
def test_drive_fetcher_attaches_binary_file_to_vault(tmp_path):
    import io
    import zipfile

    settings = Settings(project_root=tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:p><w:t>Setup &amp; guide</w:t></w:p>")
    docx = buf.getvalue()
    respx.get("https://drive.google.com/uc?export=download&id=abc789").mock(
        return_value=httpx.Response(
            200, content=docx, headers={"content-disposition": 'attachment; filename="Guide.docx"'}
        )
    )

    result = DriveFetcher(settings).fetch(
        "https://drive.google.com/file/d/abc789/view", "cid-drive3"
    )

    assert (settings.vault_dir / "attachments" / "Guide.docx").read_bytes() == docx
    assert result.text.startswith("![[Guide.docx]]")
    assert "Setup & guide" in result.text
    assert result.backend == "drive-attachment"


@respx.mock
def test_drive_attachment_with_same_name_but_different_bytes_keeps_both_files(tmp_path):
    settings = Settings(project_root=tmp_path)
    attachments = settings.vault_dir / "attachments"
    attachments.mkdir(parents=True)
    (attachments / "Guide.bin").write_bytes(b"first item's file")
    respx.get("https://drive.google.com/uc?export=download&id=abc789").mock(
        return_value=httpx.Response(
            200,
            content=b"\xff\xfesecond item's file",
            headers={"content-disposition": 'attachment; filename="Guide.bin"'},
        )
    )

    result = DriveFetcher(settings).fetch(
        "https://drive.google.com/file/d/abc789/view", "cid-drive4"
    )

    assert (attachments / "Guide.bin").read_bytes() == b"first item's file"
    assert (attachments / "cid-drive4-Guide.bin").read_bytes() == b"\xff\xfesecond item's file"
    assert result.text.startswith("![[cid-drive4-Guide.bin]]")


@respx.mock
def test_drive_attachment_with_same_name_and_same_bytes_reuses_the_file(tmp_path):
    settings = Settings(project_root=tmp_path)
    attachments = settings.vault_dir / "attachments"
    attachments.mkdir(parents=True)
    (attachments / "Guide.bin").write_bytes(b"\xff\xfesame file")
    respx.get("https://drive.google.com/uc?export=download&id=abc789").mock(
        return_value=httpx.Response(
            200,
            content=b"\xff\xfesame file",
            headers={"content-disposition": 'attachment; filename="Guide.bin"'},
        )
    )

    result = DriveFetcher(settings).fetch(
        "https://drive.google.com/file/d/abc789/view", "cid-drive5"
    )

    assert sorted(p.name for p in attachments.iterdir()) == ["Guide.bin"]
    assert result.text.startswith("![[Guide.bin]]")


@respx.mock
def test_generic_fetcher_refuses_redirect_to_private_host(tmp_path, fake_dns):
    fake_dns({"public.example.com": ["93.184.216.34"], "intranet.example.com": ["10.0.0.5"]})
    respx.get("https://public.example.com/start").mock(
        return_value=httpx.Response(302, headers={"location": "http://intranet.example.com/admin"})
    )
    secret = respx.get("http://intranet.example.com/admin").mock(
        return_value=httpx.Response(200, text="<html><body>internal</body></html>")
    )

    with pytest.raises(TextFetchError, match="private or local"):
        GenericHtmlFetcher(Settings(project_root=tmp_path)).fetch(
            "https://public.example.com/start", "cid-ssrf"
        )

    assert not secret.called


@respx.mock
def test_drive_fetcher_refuses_redirect_to_loopback(tmp_path):
    respx.get("https://drive.google.com/uc?export=download&id=abc789").mock(
        return_value=httpx.Response(302, headers={"location": "http://127.0.0.1:8787/healthz"})
    )
    secret = respx.get("http://127.0.0.1:8787/healthz").mock(
        return_value=httpx.Response(200, text="internal")
    )

    with pytest.raises(TextFetchError, match="private or local"):
        DriveFetcher(Settings(project_root=tmp_path)).fetch(
            "https://drive.google.com/file/d/abc789/view", "cid-ssrf2"
        )

    assert not secret.called


class _RebindingDns:
    """Answers `first` to the guard's check, then `then` to every later lookup."""

    def __init__(self, first: str, then: str):
        self.answers = [first]
        self.then = then
        self.lookups = 0

    def __call__(self, host, port=None, *args, **kwargs):
        self.lookups += 1
        address = self.answers.pop(0) if self.answers else self.then
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]


class _RecordingServer(http.server.ThreadingHTTPServer):
    seen: list


@pytest.fixture
def loopback_secret():
    """A real HTTP server on 127.0.0.1, standing in for a service on this machine."""

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(
                (self.path, self.headers.get("Proxy-Connection"), self.headers.get("Connection"))
            )
            body = b"<html><body>internal secret</body></html>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = _RecordingServer(("127.0.0.1", 0), Handler)
    server.seen = seen
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_guarded_client_refuses_dns_rebinding_to_loopback(monkeypatch, loopback_secret):
    """The hook's lookup says public, the connection's lookup says 127.0.0.1. Only
    the address really connected to counts."""
    monkeypatch.setattr(socket, "getaddrinfo", _RebindingDns("93.184.216.34", "127.0.0.1"))
    port = loopback_secret.server_address[1]

    with _guarded_client() as client, pytest.raises(TextFetchError, match="private or local"):
        client.get(f"http://rebind.example.com:{port}/")


def test_guarded_client_connects_to_the_address_it_checked(monkeypatch):
    """Connecting by name would resolve again and reopen the gap, so the backend must
    be handed the checked IP. TLS still verifies the name: httpcore takes SNI and the
    certificate hostname from the URL, not from the address connected to."""
    dns = _RebindingDns("93.184.216.34", "93.184.216.34")
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    connected = []

    def _record(self, host, port, *args, **kwargs):
        connected.append((host, port))
        raise httpcore.ConnectError("stop here")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", _record)

    with _guarded_client() as client, pytest.raises(httpx.ConnectError):
        client.get("https://public.example.com/")

    assert connected == [("93.184.216.34", 443)]


def test_guarded_client_tries_the_next_address_when_one_is_down(monkeypatch):
    def _two_addresses(host, port=None, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port or 0))
            for a in ("93.184.216.34", "93.184.216.35")
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _two_addresses)
    connected = []

    def _record(self, host, port, *args, **kwargs):
        connected.append(host)
        raise httpcore.ConnectError("down")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", _record)

    with _guarded_client() as client, pytest.raises(httpx.ConnectError):
        client.get("https://public.example.com/")

    assert connected == ["93.184.216.34", "93.184.216.35"]


def test_guarded_client_refuses_when_any_resolved_address_is_private(monkeypatch):
    """Round-robin answers can mix a public and a private address."""

    def _mixed(host, port=None, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port or 0))
            for a in ("93.184.216.34", "10.0.0.5")
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _mixed)
    with _guarded_client() as client, pytest.raises(TextFetchError, match="private or local"):
        client.get("https://public.example.com/")


def test_guarded_client_reports_unresolvable_host_as_a_connect_error():
    with _guarded_client() as client, pytest.raises(httpx.ConnectError):
        client.get("https://nowhere.example.com/")


def test_guarded_client_ignores_proxy_environment(monkeypatch):
    """A proxy would connect for us and resolve the name itself, past the guard."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    with _guarded_client() as client:
        assert not client._mounts


@pytest.mark.playwright
def test_rendered_fetcher_refuses_dns_rebinding_in_real_chromium(
    tmp_path, monkeypatch, loopback_secret
):
    """Python's lookups first say public, then 127.0.0.1, and Chromium's own resolver is
    forced to 127.0.0.1. Whatever the fetch returns, the loopback server must not be hit."""
    from playwright.sync_api import BrowserType

    monkeypatch.setattr(socket, "getaddrinfo", _RebindingDns("93.184.216.34", "127.0.0.1"))

    original_launch = BrowserType.launch

    def _launch(self, **kwargs):
        args = [*kwargs.pop("args", []), "--host-resolver-rules=MAP rebind.test 127.0.0.1"]
        return original_launch(self, args=args, **kwargs)

    monkeypatch.setattr(BrowserType, "launch", _launch)
    port = loopback_secret.server_address[1]

    fetcher = RenderedHtmlFetcher(_settings_with_render_fallback(tmp_path, enabled=True))
    with pytest.raises(TextFetchError) as refused:
        fetcher.fetch(f"http://rebind.test:{port}/", "cid-rebind-real")

    assert loopback_secret.seen == []
    assert "private or local" in str(refused.value)


@pytest.mark.playwright
def test_rendered_fetcher_proxy_covers_what_the_route_handler_cannot(tmp_path, monkeypatch):
    """A page on an allowed host points a WebSocket, a fetch, and an image at a host that
    resolves to a refused address, then the same at the address itself, plus a WebRTC
    STUN request. The route handler is switched off and never sees WebSockets or WebRTC
    anyway, so nothing but the proxy and the WebRTC policy stands in the way."""
    from playwright.sync_api import BrowserType

    page_body = (
        "<html><body><article><h1>Public page</h1><p>"
        + "This paragraph is ordinary public text for the extractor to find. " * 20
        + "</p></article><script>const t='http://rebind.test:SECRET_PORT/';"
        "try{new WebSocket(t.replace('http','ws')+'ws')}catch(e){}"
        "fetch(t+'fetch',{mode:'no-cors'}).catch(()=>{});"
        "document.body.appendChild(new Image()).src=t+'img';"
        "const l=t.replace('rebind.test','127.0.0.2');"
        "try{new WebSocket(l.replace('http','ws')+'literal-ws')}catch(e){}"
        "fetch(l+'literal-fetch',{mode:'no-cors'}).catch(()=>{});"
        "const pc=new RTCPeerConnection({iceServers:[{urls:'stun:127.0.0.2:STUN_PORT'}]});"
        "pc.createDataChannel('x');pc.createOffer().then(o=>pc.setLocalDescription(o));"
        "</script></body></html>"
    )
    served, secret_seen = [], []

    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            served.append(self.path)
            port = secret.server_address[1]
            stun_port = stun.getsockname()[1]
            body = page_body.replace("SECRET_PORT", str(port))
            body = body.replace("STUN_PORT", str(stun_port)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class Secret(Page):
        def do_GET(self):
            secret_seen.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    public = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Page)
    secret = http.server.ThreadingHTTPServer(("127.0.0.2", 0), Secret)
    for server in (public, secret):
        threading.Thread(target=server.serve_forever, daemon=True).start()

    stun_seen = []
    stun = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stun.bind(("127.0.0.2", 0))

    def _record_stun():
        try:
            while True:
                stun_seen.append(stun.recv(2048))
        except OSError:
            pass

    threading.Thread(target=_record_stun, daemon=True).start()

    def _dns(host, port=None, *args, **kwargs):
        address = "127.0.0.2" if host == "rebind.test" else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _dns)
    monkeypatch.setattr("reel_pipeline.text_fetcher.is_private_or_local_host", lambda host: False)
    monkeypatch.setattr(
        "reel_pipeline.text_fetcher.is_non_public_ip", lambda address: address == "127.0.0.2"
    )
    original_launch = BrowserType.launch

    def _launch(self, **kwargs):
        rules = "--host-resolver-rules=MAP rebind.test 127.0.0.2, MAP public.test 127.0.0.1"
        return original_launch(self, args=[*kwargs.pop("args", []), rules], **kwargs)

    monkeypatch.setattr(BrowserType, "launch", _launch)

    try:
        result = RenderedHtmlFetcher(_settings_with_render_fallback(tmp_path, enabled=True)).fetch(
            f"http://public.test:{public.server_address[1]}/", "cid-proxy-real"
        )
    finally:
        stun.close()
        for server in (public, secret):
            server.shutdown()
            server.server_close()

    assert "ordinary public text" in result.text
    assert "/" in served
    assert secret_seen == []
    assert stun_seen == []


@pytest.fixture
def proxy_reaches_loopback(monkeypatch):
    """The proxy refuses loopback, so its forwarding can only be tested against a real
    local server with the address check turned off."""
    monkeypatch.setattr("reel_pipeline.text_fetcher.is_non_public_ip", lambda address: False)
    monkeypatch.setattr(socket, "getaddrinfo", _RebindingDns("127.0.0.1", "127.0.0.1"))


def _proxy_exchange(proxy, request: bytes, then: bytes = b"") -> bytes:
    with socket.create_connection(proxy.server_address, timeout=5) as client:
        client.sendall(request)
        if then:
            assert client.recv(4096).startswith(b"HTTP/1.1 200")
            client.sendall(then)
        received = b""
        while data := client.recv(4096):
            received += data
        return received


def test_proxy_forwards_plain_http_in_origin_form(proxy_reaches_loopback, loopback_secret):
    target = f"127.0.0.1:{loopback_secret.server_address[1]}"
    with _running_public_only_proxy() as proxy:
        response = _proxy_exchange(
            proxy,
            f"GET http://{target}/page?q=1 HTTP/1.1\r\n"
            f"Host: {target}\r\nProxy-Connection: keep-alive\r\n\r\n".encode(),
        )

    assert b"internal secret" in response
    assert loopback_secret.seen == [("/page?q=1", None, "close")]


def test_proxy_tunnels_connect(proxy_reaches_loopback, loopback_secret):
    target = f"127.0.0.1:{loopback_secret.server_address[1]}"
    with _running_public_only_proxy() as proxy:
        response = _proxy_exchange(
            proxy,
            f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode(),
            then=b"GET /t HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n",
        )

    assert b"internal secret" in response
    assert [seen[0] for seen in loopback_secret.seen] == ["/t"]


@pytest.mark.parametrize("form", ["GET http://{}/ HTTP/1.1", "CONNECT {} HTTP/1.1"])
def test_proxy_refuses_a_name_resolving_to_loopback(monkeypatch, loopback_secret, form):
    monkeypatch.setattr(socket, "getaddrinfo", _RebindingDns("127.0.0.1", "127.0.0.1"))
    target = f"rebind.example.com:{loopback_secret.server_address[1]}"
    with _running_public_only_proxy() as proxy:
        response = _proxy_exchange(proxy, f"{form.format(target)}\r\n\r\n".encode())

    assert response.startswith(b"HTTP/1.1 403")
    assert proxy.refused == {"rebind.example.com"}
    assert loopback_secret.seen == []


@pytest.mark.parametrize(
    "request_line",
    [b"GET ftp://example.com/ HTTP/1.1", b"GET /relative HTTP/1.1", b"nonsense"],
)
def test_proxy_rejects_anything_but_a_proxy_request(monkeypatch, request_line):
    monkeypatch.setattr(socket, "getaddrinfo", _RebindingDns("127.0.0.1", "127.0.0.1"))
    with _running_public_only_proxy() as proxy:
        response = _proxy_exchange(proxy, request_line + b"\r\n\r\n")

    assert response.startswith(b"HTTP/1.1 400")


def test_rendered_fetcher_launches_chromium_behind_the_proxy(tmp_path, monkeypatch):
    browser = _install_fake_playwright(monkeypatch, html="<html><body>x</body></html>")
    with pytest.raises(TextFetchError):
        RenderedHtmlFetcher(_settings_with_render_fallback(tmp_path, enabled=True)).fetch(
            "https://example.com/app", "cid-render-proxy"
        )

    assert browser.launch_kwargs["proxy"]["server"].startswith("http://127.0.0.1:")
    assert browser.launch_kwargs["proxy"]["bypass"] == "<-loopback>"
    assert _CHROMIUM_NO_DIRECT_WEBRTC in browser.launch_kwargs["args"]


class _FakeRoute:
    def __init__(self, url, *, navigation):
        self.request = _FakeRouteRequest(url, navigation)
        self.outcome = None

    def abort(self):
        self.outcome = "aborted"

    def continue_(self):
        self.outcome = "continued"


class _FakeRouteRequest:
    def __init__(self, url, navigation):
        self.url = url
        self._navigation = navigation

    def is_navigation_request(self):
        return self._navigation


def test_rendered_fetcher_blocks_navigation_to_private_host(tmp_path, monkeypatch):
    browser = _install_fake_playwright(monkeypatch, html="<html><body>x</body></html>")
    fetcher = RenderedHtmlFetcher(_settings_with_render_fallback(tmp_path, enabled=True))
    try:
        fetcher.fetch("https://example.com/app", "cid-render-guard")
    except TextFetchError:
        pass  # the fake page's thin content is not what this test checks

    handler = browser._page.route_handler
    assert handler is not None

    redirected = _FakeRoute("http://127.0.0.1:8787/healthz", navigation=True)
    handler(redirected)
    assert redirected.outcome == "aborted"

    page_load = _FakeRoute("https://example.com/app", navigation=True)
    handler(page_load)
    assert page_load.outcome == "continued"

    subresource = _FakeRoute("https://cdn.example.com/app.js", navigation=False)
    handler(subresource)
    assert subresource.outcome == "continued"

    # A page's script or image can reach this machine too, even if it can't read
    # the answer back: fire-and-forget GETs still trigger things.
    private_subresource = _FakeRoute("http://127.0.0.1:8787/trigger", navigation=False)
    handler(private_subresource)
    assert private_subresource.outcome == "aborted"

    # No host at all: resolving "" would return this machine's own addresses.
    inline = _FakeRoute("data:image/png;base64,AAAA", navigation=False)
    handler(inline)
    assert inline.outcome == "continued"


def _drive_folder_listing(*entries: tuple[str, str, str]) -> str:
    """Mirrors the markup embeddedfolderview really serves (captured live 2026-11-01)."""
    rows = "".join(
        f'<div class="flip-entry" id="entry-{entry_id}" tabindex="0" role="link">'
        f'<div class="flip-entry-info"><a href="{href}" target="_blank">'
        '<div class="flip-entry-visual"></div>'
        f'<div class="flip-entry-title">{title}</div></a></div>'
        '<div class="flip-entry-last-modified"><div>Oct 25</div></div></div>'
        for entry_id, href, title in entries
    )
    return f'<html><body><div class="flip-entries">{rows}</div></body></html>'


@respx.mock
@pytest.mark.parametrize(
    "url",
    [
        "https://drive.google.com/drive/folders/folder123?usp=sharing",
        "https://drive.google.com/drive/mobile/folders/folder123",
        "https://drive.google.com/drive/u/0/folders/folder123",
        "https://drive.google.com/folderview?id=folder123",
    ],
)
def test_drive_fetcher_reads_every_file_in_a_public_folder(tmp_path, url):
    settings = Settings(project_root=tmp_path)
    respx.get("https://drive.google.com/embeddedfolderview?id=folder123").mock(
        return_value=httpx.Response(
            200,
            text=_drive_folder_listing(
                ("aaa111", "https://drive.google.com/file/d/aaa111/view?usp=drive_web", "code.zip"),
                (
                    "bbb222",
                    "https://drive.google.com/file/d/bbb222/view?usp=drive_web",
                    "A &amp; B.md",
                ),
            ),
        )
    )
    respx.get("https://drive.google.com/uc?export=download&id=aaa111").mock(
        return_value=httpx.Response(
            200,
            content=b"PK\x03\x04\xff\xfe",
            headers={"content-disposition": 'attachment; filename="code.zip"'},
        )
    )
    respx.get("https://drive.google.com/uc?export=download&id=bbb222").mock(
        return_value=httpx.Response(200, text="Readable markdown body.")
    )

    result = DriveFetcher(settings).fetch(url, "cid-folder1")

    assert result.backend == "drive-folder"
    assert result.content_kind == "text"
    assert "## code.zip" in result.text
    assert "![[code.zip]]" in result.text
    assert "## A & B.md" in result.text
    assert "Readable markdown body." in result.text
    assert (settings.vault_dir / "attachments" / "code.zip").exists()


@respx.mock
def test_drive_folder_keeps_going_past_one_bad_file_and_names_what_it_skipped(tmp_path):
    settings = Settings(project_root=tmp_path)
    respx.get("https://drive.google.com/embeddedfolderview?id=folder123").mock(
        return_value=httpx.Response(
            200,
            text=_drive_folder_listing(
                ("aaa111", "https://drive.google.com/file/d/aaa111/view", "gone.pdf"),
                ("sub222", "https://drive.google.com/drive/folders/sub222", "Nested"),
                ("ccc333", "https://drive.google.com/file/d/ccc333/view", "ok.txt"),
            ),
        )
    )
    respx.get("https://drive.google.com/uc?export=download&id=aaa111").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://drive.google.com/uc?export=download&id=ccc333").mock(
        return_value=httpx.Response(200, text="Still captured.")
    )

    result = DriveFetcher(settings).fetch(
        "https://drive.google.com/drive/folders/folder123", "cid-folder2"
    )

    assert "Still captured." in result.text
    assert "## gone.pdf" in result.text and "404" in result.text
    assert (
        "## Nested" in result.text
        and "https://drive.google.com/drive/folders/sub222" in result.text
    )


@respx.mock
def test_drive_folder_with_no_visible_files_raises_clear_error(tmp_path):
    settings = Settings(project_root=tmp_path)
    respx.get("https://drive.google.com/embeddedfolderview?id=folder123").mock(
        return_value=httpx.Response(200, text="<html><body>Sign in</body></html>")
    )

    with pytest.raises(TextFetchError, match="no files"):
        DriveFetcher(settings).fetch(
            "https://drive.google.com/drive/folders/folder123", "cid-folder3"
        )


@respx.mock
def test_drive_folder_where_every_file_fails_raises_instead_of_writing_an_empty_note(tmp_path):
    settings = Settings(project_root=tmp_path)
    respx.get("https://drive.google.com/embeddedfolderview?id=folder123").mock(
        return_value=httpx.Response(
            200,
            text=_drive_folder_listing(
                ("aaa111", "https://drive.google.com/file/d/aaa111/view", "gone.pdf"),
            ),
        )
    )
    respx.get("https://drive.google.com/uc?export=download&id=aaa111").mock(
        return_value=httpx.Response(404)
    )

    with pytest.raises(TextFetchError, match="none of the"):
        DriveFetcher(settings).fetch(
            "https://drive.google.com/drive/folders/folder123", "cid-folder4"
        )


@respx.mock
def test_drive_virus_scan_interstitial_is_an_error_not_note_content(tmp_path):
    settings = Settings(project_root=tmp_path)
    interstitial = (
        "<!doctype html><html><body><article><h1>Virus scan warning</h1>"
        "<p>Google Drive can't scan this file for viruses. big.pdf is too large "
        "for Google to scan for viruses. Would you still like to download this file?</p>"
        "</article></body></html>"
    )
    respx.get("https://drive.google.com/uc?export=download&id=big123").mock(
        return_value=httpx.Response(200, text=interstitial)
    )

    with pytest.raises(TextFetchError, match="too large"):
        DriveFetcher(settings).fetch("https://drive.google.com/file/d/big123/view", "cid-drive6")


def test_drive_fetcher_raises_clear_error_when_no_file_id_in_url(tmp_path):
    settings = Settings(project_root=tmp_path)

    with pytest.raises(TextFetchError, match="could not find a Drive file id"):
        DriveFetcher(settings).fetch("https://drive.google.com/drive/my-drive", "cid-drive4")


@respx.mock
def test_drive_fetcher_raises_clear_error_on_http_failure(tmp_path):
    settings = Settings(project_root=tmp_path)
    respx.get("https://drive.google.com/uc?export=download&id=abc999").mock(
        return_value=httpx.Response(404)
    )

    with pytest.raises(TextFetchError, match="404"):
        DriveFetcher(settings).fetch("https://drive.google.com/file/d/abc999/view", "cid-drive5")


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
        GenericHtmlFetcher,
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
        GenericHtmlFetcher,
        "fetch",
        lambda self, url, content_id: pytest.fail(
            "generic HTML fetcher should not handle notion.so - NotionFetcher should"
        ),
    )
    monkeypatch.setattr(
        GitHubFetcher,
        "fetch",
        lambda self, url, content_id: pytest.fail("GitHub should not handle notion.so"),
    )

    DispatchingTextFetcher(settings).fetch("https://www.notion.so/Some-Page-abc", "cid9")

    assert calls == [("notion", "https://www.notion.so/Some-Page-abc")]


def test_dispatching_text_fetcher_routes_unenumerated_domain_to_generic_html(tmp_path, monkeypatch):
    """2026-08-10 catch-all policy: any non-GitHub host goes through the
    generic HTML fetcher by default, not an "unrecognized domain" rejection.
    """
    settings = Settings(project_root=tmp_path)
    calls = []
    monkeypatch.setattr(
        GenericHtmlFetcher,
        "fetch",
        lambda self, url, content_id: (
            calls.append(("generic", url)) or TranscriptResultStub(content_id)
        ),
    )

    DispatchingTextFetcher(settings).fetch("https://example.com/x", "cid10")

    assert calls == [("generic", "https://example.com/x")]


def test_dispatching_text_fetcher_routes_drive_to_drive_fetcher(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)
    calls = []
    monkeypatch.setattr(
        DriveFetcher,
        "fetch",
        lambda self, url, content_id: (
            calls.append(("drive", url)) or TranscriptResultStub(content_id)
        ),
    )
    monkeypatch.setattr(
        GenericHtmlFetcher,
        "fetch",
        lambda self, url, content_id: pytest.fail(
            "generic HTML fetcher should not handle drive.google.com - DriveFetcher should"
        ),
    )

    DispatchingTextFetcher(settings).fetch("https://drive.google.com/file/d/abc123/view", "cid11")

    assert calls == [("drive", "https://drive.google.com/file/d/abc123/view")]


def test_get_text_fetcher_returns_dispatching_instance(tmp_path):
    settings = Settings(project_root=tmp_path)
    assert isinstance(get_text_fetcher(settings), DispatchingTextFetcher)


def test_rendered_html_fetcher_explains_event_loop_limitation(tmp_path, monkeypatch):
    """Regression test for a real failure (2026-08-23, threads.com share link).

    asyncio raises NotImplementedError bare and message-less when the loop
    can't spawn subprocesses, so the failure reached needs-attention.txt as
    literally "processing failed: NotImplementedError()" - unactionable, with
    the real traceback only in the webhook log.
    """
    import playwright.sync_api as playwright_api

    settings = Settings(project_root=tmp_path)

    def _cannot_spawn_subprocess():
        raise NotImplementedError

    monkeypatch.setattr(playwright_api, "sync_playwright", _cannot_spawn_subprocess)

    with pytest.raises(TextFetchError, match="cannot spawn the browser driver subprocess"):
        RenderedHtmlFetcher(settings).fetch("https://example.com/spa", "cid-render-loop")


def test_rendered_html_fetcher_scopes_subprocess_capable_policy_then_restores(
    tmp_path, monkeypatch
):
    """`serve-webhook` sets the Windows selector policy process-wide (cli.py),
    which cannot spawn the subprocess Playwright needs. The fetcher must swap
    in a subprocess-capable policy for the duration of the call and put the
    original back - without the swap, Playwright sees the selector policy and
    the render path is dead under the webhook server.
    """
    if sys.platform != "win32":
        pytest.skip("the selector/proactor policy conflict is Windows-only")

    import playwright.sync_api as playwright_api

    settings = Settings(project_root=tmp_path)
    rendered_html = (
        "<html><body><article><p>Rendered content that is comfortably past the "
        "minimum plausible length so it clears the app-shell check cleanly.</p>"
        "</article></body></html>"
    )
    _install_fake_playwright(monkeypatch, html=rendered_html)

    fake_factory = playwright_api.sync_playwright
    seen = {}

    def _recording_factory():
        seen["policy"] = type(asyncio.get_event_loop_policy())
        return fake_factory()

    monkeypatch.setattr(playwright_api, "sync_playwright", _recording_factory)

    original = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        RenderedHtmlFetcher(settings).fetch("https://example.com/spa", "cid-render-policy")
        assert seen["policy"] is asyncio.WindowsProactorEventLoopPolicy
        assert isinstance(asyncio.get_event_loop_policy(), asyncio.WindowsSelectorEventLoopPolicy)
    finally:
        asyncio.set_event_loop_policy(original)
