from __future__ import annotations

import base64

import httpx
import pytest
import respx

from reel_pipeline.config import Settings
from reel_pipeline.text_fetcher import (
    DispatchingTextFetcher,
    GenericHtmlFetcher,
    GitHubFetcher,
    NotionFetcher,
    TextFetchError,
    get_text_fetcher,
)


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
    settings = Settings(project_root=tmp_path)
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
    settings = Settings(project_root=tmp_path)
    respx.get("https://roadmap.notion.site/").mock(
        return_value=httpx.Response(200, text=_NOTION_JS_REQUIRED_HTML)
    )

    with pytest.raises(TextFetchError, match="client-side"):
        GenericHtmlFetcher(settings).fetch("https://roadmap.notion.site/", "cid-js-shell")


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


def test_get_text_fetcher_returns_dispatching_instance(tmp_path):
    settings = Settings(project_root=tmp_path)
    assert isinstance(get_text_fetcher(settings), DispatchingTextFetcher)
