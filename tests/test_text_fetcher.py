from __future__ import annotations

import base64

import httpx
import pytest
import respx

from reel_pipeline.config import Settings
from reel_pipeline.text_fetcher import GitHubFetcher, NotionFetcher, TextFetchError


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

    result = NotionFetcher(settings).fetch(
        "https://www.notion.so/Project-Notes-abc123", "cid5"
    )

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
