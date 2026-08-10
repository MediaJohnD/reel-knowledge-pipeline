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


def test_normalize_url_strips_youtube_share_id():
    """Regression test: the same YouTube video, shared twice with different
    iOS share-sheet 'is' identifiers, produced two different content_ids for
    identical content and was ingested as two duplicate notes."""
    assert normalize_url("https://youtu.be/vJEy3nP2_C8?is=1BPVF-kZhr8DII5d") == normalize_url(
        "https://youtu.be/vJEy3nP2_C8?is=91J3tJQZAnXszpcn"
    )


def make_settings(tmp_path, allowed, blocked=None) -> Settings:
    return Settings(
        project_root=tmp_path,
        download=DownloadConfig(allowed_domains=allowed, blocked_domains=blocked or []),
    )


def test_validate_url_accepts_uppercase_scheme(tmp_path):
    """Regression test: validate_url checked the raw parsed scheme against a
    lowercase-only set, so a legitimate uppercase-scheme URL (e.g. from a client
    that uppercases it) was rejected even though normalize_url already lowercases
    the scheme for the accepted case.
    """
    settings = make_settings(tmp_path, allowed=["youtube.com"])

    result = validate_url("HTTPS://youtube.com/watch?v=abc123", settings)

    assert result.ok


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

    assert classify_url_kind("https://youtube.com/watch?v=abc", settings) == "media"


def test_classify_url_kind_text_domain(tmp_path):
    settings = make_settings(tmp_path, allowed=["youtube.com"])

    assert classify_url_kind("https://github.com/owner/repo", settings) == "text"


def test_classify_url_kind_unenumerated_domain_defaults_to_text(tmp_path):
    """As of 2026-08-10, text-capture is a catch-all: any domain not matched
    to the media allow-list (and not explicitly blocked) is text by default -
    not just an enumerated platform list. See CLAUDE.md's 2026-08-10 entry.
    """
    settings = make_settings(tmp_path, allowed=["youtube.com"])

    assert classify_url_kind("https://airtable.com/base/abc", settings) == "text"


def test_classify_url_kind_blocked_text_domain_returns_none(tmp_path):
    settings = make_settings(tmp_path, allowed=["youtube.com"])
    settings.text_capture.blocked_domains = ["airtable.com"]

    assert classify_url_kind("https://airtable.com/base/abc", settings) is None


def test_classify_url_kind_rejects_malformed_url_instead_of_defaulting_to_text(tmp_path):
    """The catch-all default must not launder a non-http(s)/no-host URL
    (that validate_url() already rejected) into "text".
    """
    settings = make_settings(tmp_path, allowed=["youtube.com"])

    assert classify_url_kind("not a url", settings) is None
    assert classify_url_kind("ftp://example.com/file", settings) is None


def test_classify_url_kind_rejects_loopback_and_private_ip_targets(tmp_path):
    """The catch-all default must not turn into an SSRF vector against this
    machine's own services (e.g. Ollama on 127.0.0.1) or the local network.
    """
    settings = make_settings(tmp_path, allowed=["youtube.com"])

    assert classify_url_kind("http://127.0.0.1:11434/", settings) is None
    assert classify_url_kind("http://localhost:11434/", settings) is None
    assert classify_url_kind("http://192.168.1.1/admin", settings) is None
    assert classify_url_kind("http://169.254.169.254/latest/meta-data/", settings) is None


def test_classify_url_kind_prefers_media_on_overlap(tmp_path):
    """Documented tie-break (see validators.py) for the defensive case where a
    domain is misconfigured into both media and text-capture-blocked lists -
    should not happen in practice, but the precedence must be deterministic.
    """
    settings = make_settings(tmp_path, allowed=["example.com"])
    settings.text_capture.blocked_domains = ["example.com"]

    assert classify_url_kind("https://example.com/x", settings) == "media"
