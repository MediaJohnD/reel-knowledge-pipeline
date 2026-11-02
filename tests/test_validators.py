from __future__ import annotations

import sys
import types

import pytest

from reel_pipeline.config import DownloadConfig, Settings
from reel_pipeline.validators import (
    classify_url_kind,
    compute_content_id,
    normalize_url,
    sanitize_url,
    unwrap_redirect,
    validate_url,
)


def _install_fake_yt_dlp(monkeypatch, *, raises: bool):
    """Fakes the yt_dlp package well enough to exercise
    _classify_drive_url_kind()'s metadata-only probe without a real network
    call - same pattern as tests/test_downloader.py's _install_fake_yt_dlp.
    """

    class FakeYoutubeDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def extract_info(self, url, download=True):
            if raises:
                raise RuntimeError("[GoogleDrive] Unable to download JSON metadata: 400")
            return {"ext": "mp4"}

    fake_module = types.ModuleType("yt_dlp")
    fake_module.YoutubeDL = FakeYoutubeDL  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yt_dlp", fake_module)


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


def test_normalize_url_is_idempotent():
    """docs/formal-model.md Property 2.1: normalizing twice changes nothing."""
    urls = [
        "HTTPS://WWW.GitHub.com/a/b/?utm_source=x&z=1&a=2#frag",
        "https://www.instagram.com/reel/abc/?igsh=xyz&img_index=2",
        "https://acme.notion.site/Page-123?source=copy_link&pvs=4",
        "https://youtu.be/abc?si=share&t=10",
        "https://docs.google.com/document/d/ID/edit?usp=sharing",
        "https://example.com",
    ]
    for url in urls:
        once = normalize_url(url)
        assert normalize_url(once) == once, url


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


def test_instagram_share_tokens_do_not_change_content_id():
    """Regression test: reel DcEXXKGiIQ2 was ingested twice (two vault notes) because
    ?igsi= and ?stkn= share tokens weren't stripped. Instagram keeps minting new token
    names, so every Instagram query param except img_index is dropped."""
    assert compute_content_id(
        "https://www.instagram.com/reel/DcEXXKGiIQ2/?igsi=Y284ZnUyanU4MHNl"
    ) == compute_content_id("https://www.instagram.com/reel/DcEXXKGiIQ2/?stkn=MTF4bDlxNnpyaGl6Zg==")
    assert normalize_url(
        "https://www.instagram.com/p/abc/?comment_id=1&some_future_token=x"
    ) == normalize_url("https://instagram.com/p/abc")
    # Host detection must ignore an explicit port, or the allowlist is bypassed.
    assert "stkn" not in normalize_url("https://instagram.com:443/p/abc/?stkn=x")


def test_paid_ad_click_ids_do_not_change_content_id():
    """Regression test: the same storefront page reached through two different Meta ad
    creatives carried different ad_id/campaign_id/dm_*/utm_id values and was ingested as
    two notes (rayneo-io-ai-glasses, 2026-09-18). Same root cause as fbclid above."""
    page = "https://rayneo.com/pages/rayneo-io-ai-glasses"
    creative_a = (
        f"{page}?ad_id=1202555039891&campaign_id=1202554218066"
        "&dm_ad=1202555039891&dm_cam=1202554218066&utm_id=1202554218066"
    )
    creative_b = (
        f"{page}?ad_id=9999999999999&campaign_id=8888888888888"
        "&dm_ad=9999999999999&dm_grp=7777777777777&dm_net=f&utm_id=8888888888888"
    )

    assert compute_content_id(creative_a) == compute_content_id(creative_b)
    assert compute_content_id(creative_a) == compute_content_id(page)
    # LinkedIn's first-party ad cookie id rides along on shared post links.
    assert normalize_url("https://example.com/x?li_fat_id=abc&rcm=def") == normalize_url(
        "https://example.com/x"
    )


def test_ad_tracking_variants_do_not_change_content_id():
    """Regression test: consultantgigs.org/onboarding/consultants (utm_adname etc.),
    fractionalinabox.com/registration1 (cmc_adid/fbc_id/h_ad_id, three notes) and
    github.com/d4vinci/Scrapling (utm_referrer) each became duplicate notes in
    2026-09/10 through params the blocklist didn't name."""
    page = "https://example.com/registration1"
    variants = [
        f"{page}?utm_adname=INT-481&utm_adset=a&utm_adset_id=1&utm_campaign_id=2",
        f"{page}?utm_referrer=sp_auto_dm",
        f"{page}?UTM_Whatever=x",
        f"{page}?cmc_adid=fb_1&fbc_id=2&h_ad_id=3",
        f"{page}?adset_id=4",
    ]

    for variant in variants:
        assert compute_content_id(variant) == compute_content_id(page), variant
    # A param that merely contains "utm" is not tracking and still identifies content.
    assert normalize_url(f"{page}?autumn=1") != normalize_url(page)


def test_normalize_url_strips_notion_copy_link_source_only_on_notion_hosts():
    """Regression test: the same Notion page ingested with and without Notion's
    `?source=copy_link` minted two content_ids and two notes (23-things-..., 2026-10-04)."""
    page = "https://app.notion.com/p/23-things-3c6e396e06bb81028c50d7b565f270d6"

    assert compute_content_id(f"{page}?source=copy_link") == compute_content_id(page)
    assert normalize_url("https://x.notion.site/Page-abc?source=copy_link") == normalize_url(
        "https://x.notion.site/Page-abc"
    )
    # `source` can identify content elsewhere, so it stays off the global strip list.
    assert "source=feed" in normalize_url("https://example.com/x?source=feed")


def test_sanitize_url_drops_auth_tokens_without_changing_the_content_id():
    """Regression test: a ManyChat share link committed a live JWT (mcp_token) to the
    vault's git remote. The token has to survive normalize_url (content_id stability
    for anything already ingested) and die in sanitize_url (what gets persisted)."""
    url = (
        "https://app.manychat.com/flowPlayerPage"
        "?share_hash=abc123&mcp_token=eyJhbGciOiJIUzI1NiJ9.payload.sig"
    )

    sanitized = sanitize_url(url)

    assert sanitized == "https://app.manychat.com/flowPlayerPage?share_hash=abc123"
    assert "mcp_token" in normalize_url(url)
    assert compute_content_id(url) != compute_content_id(sanitized)


def test_sanitize_url_reuses_the_instagram_allowlist():
    """Instagram per-share tokens are already gone from normalize_url, so sanitize_url
    inherits that instead of chasing each new token name (stkn, igsh, igsi, ...)."""
    assert (
        sanitize_url("https://www.instagram.com/reel/DcEXXKGiIQ2/?stkn=MTF4bDlxNnpyaGl6Zg==")
        == "https://instagram.com/reel/DcEXXKGiIQ2"
    )


def test_instagram_img_index_still_distinguishes_carousel_slides():
    """GalleryDlDownloader turns img_index into --range and captures only that slide,
    so different img_index values are different content - but the same slide
    re-shared with a different token is not."""
    slide_2 = "https://www.instagram.com/p/DaeWEfhFPjZ/?img_index=2&igsh=MWRmZmpkY243bDNjMw=="
    assert compute_content_id(slide_2) != compute_content_id(
        "https://www.instagram.com/p/DaeWEfhFPjZ/?img_index=4&igsh=MWRmZmpkY243bDNjMw=="
    )
    assert compute_content_id(slide_2) == compute_content_id(
        "https://www.instagram.com/p/DaeWEfhFPjZ/?stkn=abc&img_index=2"
    )
    # img_index is canonicalized the way the downloader reads it, so URLs that capture
    # the same slide get the same id: first value wins, leading zeros don't matter, and
    # a key or value the downloader ignores means "whole post".
    post = "https://www.instagram.com/p/DaeWEfhFPjZ/"
    assert compute_content_id(slide_2) == compute_content_id(f"{post}?img_index=2&img_index=4")
    assert compute_content_id(slide_2) == compute_content_id(f"{post}?img_index=02")
    assert compute_content_id(post) == compute_content_id(f"{post}?img_index=abc")
    assert compute_content_id(post) == compute_content_id(f"{post}?IMG_INDEX=2")


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


def test_classify_url_kind_rejects_hostname_resolving_to_private_address(tmp_path, fake_dns):
    settings = make_settings(tmp_path, allowed=["youtube.com"])
    fake_dns(
        {
            "intranet.example.com": ["10.0.0.5"],
            "mixed.example.com": ["93.184.216.34", "192.168.1.9"],
            "public.example.com": ["93.184.216.34"],
        }
    )

    assert classify_url_kind("https://intranet.example.com/wiki", settings) is None
    assert classify_url_kind("https://mixed.example.com/", settings) is None
    assert classify_url_kind("https://public.example.com/post", settings) == "text"


def test_classify_url_kind_keeps_text_when_dns_lookup_fails(tmp_path):
    """A lookup failure proves nothing about the address, and the fetch reports
    its own clear error later - rejecting here would just hide that error.
    """
    settings = make_settings(tmp_path, allowed=["youtube.com"])

    assert classify_url_kind("https://does-not-resolve.example.com/", settings) == "text"


def test_classify_url_kind_rejects_private_targets_hidden_by_url_syntax(tmp_path):
    """Host parsing used to split the raw netloc on ":", so userinfo and
    bracketed IPv6 slipped past the private-address check.
    """
    settings = make_settings(tmp_path, allowed=["youtube.com"])

    assert classify_url_kind("http://user@127.0.0.1/", settings) is None
    assert classify_url_kind("http://[::1]:8080/", settings) is None
    assert classify_url_kind("http://[::ffff:127.0.0.1]/", settings) is None
    assert classify_url_kind("http://foo.localhost/", settings) is None


def test_classify_url_kind_rejects_tailnet_cgnat_addresses(tmp_path):
    """100.64.0.0/10 is what Tailscale hands out - it reaches the owner's own
    machines, but ipaddress doesn't count it as is_private.
    """
    settings = make_settings(tmp_path, allowed=["youtube.com"])

    assert classify_url_kind("http://100.100.100.100:8787/healthz", settings) is None


def test_classify_url_kind_prefers_media_on_overlap(tmp_path):
    """Documented tie-break (see validators.py) for the defensive case where a
    domain is misconfigured into both media and text-capture-blocked lists -
    should not happen in practice, but the precedence must be deterministic.
    """
    settings = make_settings(tmp_path, allowed=["example.com"])
    settings.text_capture.blocked_domains = ["example.com"]

    assert classify_url_kind("https://example.com/x", settings) == "media"


def test_classify_url_kind_drive_video_url_stays_media(tmp_path, monkeypatch):
    """drive.google.com is unique among download.allowed_domains hosts - it
    hosts both video files and arbitrary shared documents, so it's the only
    one that runs a live yt-dlp probe instead of returning "media"
    immediately (see _classify_drive_url_kind's docstring in validators.py).
    """
    _install_fake_yt_dlp(monkeypatch, raises=False)
    settings = make_settings(tmp_path, allowed=["drive.google.com"])

    assert classify_url_kind("https://drive.google.com/file/d/abc123/view", settings) == "media"


def test_classify_url_kind_drive_non_video_url_becomes_text(tmp_path, monkeypatch):
    """A shared Drive document (not a video) fails yt-dlp's metadata-only
    probe the same way a real download attempt would - falls through to
    "text" instead of being stuck as an unfetchable "media" item. Confirmed
    live against a real example (a shared .md skill file) before this was
    written - see docs/superpowers/specs/2026-08-10-drive-text-capture-design.md.
    """
    _install_fake_yt_dlp(monkeypatch, raises=True)
    settings = make_settings(tmp_path, allowed=["drive.google.com"])

    assert classify_url_kind("https://drive.google.com/file/d/abc123/view", settings) == "text"


@pytest.mark.parametrize(
    ("wrapped", "target"),
    [
        (
            "https://l.instagram.com/?u=https%3A%2F%2Fa.example%2Fb%3Fc%3D1&e=AT0x",
            "https://a.example/b?c=1",
        ),
        ("https://l.facebook.com/l.php?u=https%3A%2F%2Fa.example%2Fb&h=AT0", "https://a.example/b"),
        ("https://lm.facebook.com/l.php?u=https%3A%2F%2Fa.example%2Fb", "https://a.example/b"),
        ("https://www.google.com/url?q=https%3A%2F%2Fa.example%2Fb&sa=D", "https://a.example/b"),
        ("https://www.google.com/url?url=https%3A%2F%2Fa.example%2Fb", "https://a.example/b"),
        (
            "https://www.linkedin.com/safety/go?url=https%3A%2F%2Fa.example%2Fb",
            "https://a.example/b",
        ),
        (
            "https://www.youtube.com/redirect?q=https%3A%2F%2Fa.example%2Fb&v=1",
            "https://a.example/b",
        ),
    ],
)
def test_unwrap_redirect_finds_the_real_link(wrapped, target):
    assert unwrap_redirect(wrapped) == target


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/p/abc/?u=https%3A%2F%2Fa.example",
        "https://www.google.com/search?q=https%3A%2F%2Fa.example",
        "https://www.youtube.com/watch?v=abc",
        "https://evil.example/l.php?u=https%3A%2F%2Fa.example",
        "https://l.facebook.com/l.php?u=javascript%3Aalert(1)",
        "https://l.facebook.com/l.php?u=file%3A%2F%2F%2Fetc%2Fpasswd",
        "https://l.facebook.com/l.php",
        "not a url",
    ],
)
def test_unwrap_redirect_leaves_everything_else_alone(url):
    assert unwrap_redirect(url) == url


def test_unwrap_redirect_handles_a_wrapper_inside_a_wrapper():
    inner = "https%253A%252F%252Fa.example%252Fb"
    assert (
        unwrap_redirect(
            f"https://www.google.com/url?q=https%3A%2F%2Fl.facebook.com%2Fl.php%3Fu%3D{inner}"
        )
        == "https://a.example/b"
    )
