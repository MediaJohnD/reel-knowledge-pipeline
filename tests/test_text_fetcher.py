from __future__ import annotations

import base64

import httpx
import pytest
import respx

from reel_pipeline.config import RenderFallbackConfig, Settings, TextCaptureConfig
from reel_pipeline.text_fetcher import (
    DispatchingTextFetcher,
    DriveFetcher,
    GenericHtmlFetcher,
    GitHubFetcher,
    NotionFetcher,
    RenderedHtmlFetcher,
    TextFetchError,
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

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, browser=None, launch_error=None):
        self._browser = browser
        self._launch_error = launch_error

    def launch(self):
        if self._launch_error is not None:
            raise self._launch_error
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
    monkeypatch.setattr(
        playwright_api, "sync_playwright", lambda: _FakeSyncPlaywright(chromium)
    )
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
        "child-1": _notion_block(
            "child-1", "text", title=[["Child text."]], content=[page_id]
        ),
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
            RenderedHtmlFetcher(settings).fetch(
                "https://roadmap.notion.site/", "cid-render-live"
            )


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
def test_drive_fetcher_raises_clear_error_on_binary_file(tmp_path):
    settings = Settings(project_root=tmp_path)
    respx.get("https://drive.google.com/uc?export=download&id=abc789").mock(
        return_value=httpx.Response(200, content=b"\x89PNG\r\n\x1a\n\x00\x01\xff\xfe")
    )

    with pytest.raises(TextFetchError, match="binary format"):
        DriveFetcher(settings).fetch("https://drive.google.com/file/d/abc789/view", "cid-drive3")


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


def test_dispatching_text_fetcher_routes_unenumerated_domain_to_generic_html(
    tmp_path, monkeypatch
):
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

    DispatchingTextFetcher(settings).fetch(
        "https://drive.google.com/file/d/abc123/view", "cid11"
    )

    assert calls == [("drive", "https://drive.google.com/file/d/abc123/view")]


def test_get_text_fetcher_returns_dispatching_instance(tmp_path):
    settings = Settings(project_root=tmp_path)
    assert isinstance(get_text_fetcher(settings), DispatchingTextFetcher)
