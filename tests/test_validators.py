from __future__ import annotations

from reel_pipeline.config import DownloadConfig, Settings
from reel_pipeline.validators import classify_url_kind, normalize_url, validate_url


def test_normalize_url_strips_tracking_params_including_fbclid():
    """Regression test: the same GitHub URL, shared directly vs. relayed through
    Facebook (which appends fbclid), used to normalize to two different URLs and
    therefore two different content_ids for identical content - found during
    manual testing of the text-capture feature.
    """
    direct = normalize_url("https://github.com/public-apis/public-apis")
    via_facebook = normalize_url(
        "https://github.com/public-apis/public-apis"
        "?fbclid=PAVERFWATDkcxwZG9mAmV4dG4DYWVtAjEwAHNydGMGYXBwX2lkDzEyNDAyNDU3NDI4NzQxNAAB"
    )

    assert direct == via_facebook


def test_normalize_url_strips_mibextid_gclid_and_mailchimp_params():
    assert normalize_url("https://example.com/x?mibextid=abc") == normalize_url(
        "https://example.com/x"
    )
    assert normalize_url("https://example.com/x?gclid=abc") == normalize_url(
        "https://example.com/x"
    )
    assert normalize_url("https://example.com/x?mc_cid=abc&mc_eid=def") == normalize_url(
        "https://example.com/x"
    )


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
