from __future__ import annotations

from reel_pipeline.config import DownloadConfig, Settings
from reel_pipeline.validators import validate_url


def make_settings(tmp_path, allowed, blocked=None) -> Settings:
    return Settings(
        project_root=tmp_path,
        download=DownloadConfig(allowed_domains=allowed, blocked_domains=blocked or []),
    )


def test_subdomain_allow_entry_permits_only_that_subdomain(tmp_path):
    settings = make_settings(tmp_path, allowed=["drive.google.com"])

    allowed = validate_url("https://drive.google.com/file/d/abc123/view", settings)
    other_google_property = validate_url("https://docs.google.com/document/d/abc123", settings)

    assert allowed.ok
    assert not other_google_property.ok


def test_bare_domain_allow_entry_permits_all_subdomains(tmp_path):
    settings = make_settings(tmp_path, allowed=["youtube.com"])

    apex = validate_url("https://youtube.com/watch?v=abc123", settings)
    subdomain = validate_url("https://m.youtube.com/watch?v=abc123", settings)

    assert apex.ok
    assert subdomain.ok
