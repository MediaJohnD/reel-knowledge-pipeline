"""URL validation, normalization, and deterministic content-id derivation.

Normalization + content-id hashing are what makes queue processing idempotent:
the same URL (modulo tracking params/casing) always maps to the same content_id,
so re-adding it to the queue or re-delivering a webhook is a safe no-op once
that content_id reaches a terminal state in state.json.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from reel_pipeline.config import Settings

# Query params that don't affect the identity of the content (tracking/referral noise).
# fbclid/mibextid in particular: their absence caused a real duplicate content_id for the
# exact same GitHub URL during manual testing - a link shared once directly and once
# re-shared/relayed through Facebook normalized to two different URLs without this.
_STRIP_QUERY_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "si",
    "feature",
    "igshid",
    "igsh",
    "ref",
    "ref_src",
    "s",
    "fbclid",
    "mibextid",
    "gclid",
    "mc_cid",
    "mc_eid",
}


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, drop fragment, drop tracking query params, sort the rest."""
    url = url.strip()
    parsed = urlparse(url)
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[len("www.") :]
    path = parsed.path.rstrip("/") or ""
    kept_params = sorted(
        (key, value)
        for key, value in parse_qsl(parsed.query)
        if key.lower() not in _STRIP_QUERY_PARAMS
    )
    query = urlencode(kept_params)
    return urlunparse((scheme, netloc, path, "", query, ""))


def compute_content_id(url: str) -> str:
    """Deterministic short id derived from the normalized URL."""
    normalized = normalize_url(url)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return digest[:16]


def _registrable_domain(netloc: str) -> str:
    """Best-effort registrable domain (last two labels), good enough for allow/block lists."""
    host = netloc.split(":")[0].lower()
    if host.startswith("www."):
        host = host[len("www.") :]
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    normalized_url: str
    content_id: str
    reason: str | None = None
    blocked: bool = False


def validate_url(url: str, settings: Settings) -> ValidationResult:
    """Validate a submitted URL against basic shape and the allow/block domain lists.

    Blocked domains (e.g. instagram.com) are reported distinctly (blocked=True) so
    callers can route them to needs-attention.txt with a clear "manual ingestion
    required" reason, per the no-browser-automation guardrail, rather than treating
    them as a generic failure.
    """
    stripped = url.strip()
    if not stripped:
        return ValidationResult(ok=False, normalized_url="", content_id="", reason="empty URL")

    parsed = urlparse(stripped)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ValidationResult(
            ok=False,
            normalized_url="",
            content_id="",
            reason=f"not a valid http(s) URL: {stripped!r}",
        )

    normalized = normalize_url(stripped)
    content_id = compute_content_id(stripped)
    domain = _registrable_domain(parsed.netloc)
    host = parsed.netloc.split(":")[0].lower()
    if host.startswith("www."):
        host = host[len("www.") :]

    blocked_domains = settings.download.blocked_domains
    if any(domain == blocked or domain.endswith(f".{blocked}") for blocked in blocked_domains):
        return ValidationResult(
            ok=False,
            normalized_url=normalized,
            content_id=content_id,
            reason=(
                f"domain '{domain}' is blocked by default (no browser automation / "
                "scraping ingestion for this platform). Use manual paste, webhook, "
                "or share-sheet export instead."
            ),
            blocked=True,
        )

    # Matched against the full host (not reduced to a registrable domain) so an
    # allow-list entry with a subdomain (e.g. "drive.google.com") only allows that
    # subdomain, rather than every property under the parent domain (e.g. all of
    # google.com - Docs, Sheets, Photos, Search).
    allowed = settings.download.allowed_domains
    if allowed and not any(host == d or host.endswith(f".{d}") for d in allowed):
        return ValidationResult(
            ok=False,
            normalized_url=normalized,
            content_id=content_id,
            reason=f"domain '{domain}' is not in the configured allow-list",
        )

    return ValidationResult(ok=True, normalized_url=normalized, content_id=content_id)


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
