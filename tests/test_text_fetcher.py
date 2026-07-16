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
