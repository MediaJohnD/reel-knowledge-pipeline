"""Monthly link health check over every reel note. Read-only toward the notes.

Pulls URLs out of the notes the ledger and the review manifest know about, then asks
each one whether it still answers (HEAD, then GET without reading the body). Writes
data/review/link_health.json and a dated report in the vault's Reels folder.

Not checked, by design: login-walled social hosts (note-only, a GET there proves
nothing), image and badge assets, GitHub (scripts/vet_repos.py covers it with the API),
private targets (the guarded client refuses them), and URLs carrying a token (a GET
could use it up). "live" means the URL answered, not that the content is still what the
note says.

Usage: recheck_links.py [--if-older-than DAYS] [--limit N] [--self-check]
"""

import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src")]

HEALTH = ROOT / "data" / "review" / "link_health.json"
URL = re.compile(r"https?://[^\s\]>\"'|`<\\]+")
NOTE_ONLY = (
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "linkedin.com",
    "x.com",
    "twitter.com",
    "t.co",
    "threads.net",
    "threads.com",
    "tiktok.com",
)
SKIP_GITHUB = ("github.com", "raw.githubusercontent.com", "gist.github.com")
ASSET_HOSTS = ("img.shields.io", "media.skillry.dev", "camo.githubusercontent.com")
ASSET_EXT = re.compile(r"\.(png|jpe?g|gif|svg|webp|ico|mp4|webm|mov|woff2?)$", re.I)
PLACEHOLDER = re.compile(
    r"(^|\.)(example(-[\w-]+)?\.(com|org|net)|your[-\w]*\.\w+|domain\.com|test)$", re.I
)
TEMPLATE = re.compile(r"[{}]|/URL$")  # https://x.io/chat/{uri}, https://r.jina.ai/URL
TOKENISH = re.compile(r"(token|key|secret|sig|auth|unsubscribe|session)", re.I)
HOST_GAP = 1.0  # seconds between requests to one host
HOST_TRIP = 5  # consecutive connection failures before giving up on a host
TIMEOUT = 15
DIRECTORY_MIN = 200  # a note citing this many URLs is a link directory, not a reel
# https://openrouter.ai/api/v1, https://integrate.api.nvidia.com/v1 have no page of their own
API_BASE = re.compile(r"(^|\.)api\.[^/]+/v\d+(beta\d*)?/?$|/api/v\d+(beta\d*)?/?$")


def _on(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)


def extract(text):
    out = set()
    for raw in URL.findall(text):
        u = raw.rstrip(".,;:!?*_~")
        while u.endswith(")") and u.count(")") > u.count("("):
            u = u[:-1].rstrip(".,;:!?*_~")
        out.add(u.split("#", 1)[0])
    return out


def classify_target(url):
    """-> a skip reason, or None if the URL should be requested."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return "malformed"
    if not host or "." not in host:
        return "malformed"
    if _on(host, SKIP_GITHUB):
        return "github"
    if _on(host, NOTE_ONLY):
        return "note-only"
    if _on(host, ASSET_HOSTS) or ASSET_EXT.search(parts.path):
        return "asset"
    if PLACEHOLDER.search(host) or TEMPLATE.search(url):
        return "placeholder"
    if parts.query and TOKENISH.search(parts.query):
        return "has-token"
    return None


def verdict(code):
    if code is None:
        return "error"
    if code < 400:
        return "live"
    if code in (404, 410):
        return "dead"
    if code in (401, 405):
        return "answers"  # wants a key or another method: the endpoint exists
    if code in (402, 403, 406, 429, 451, 503, 999):
        return "blocked"
    return "error"


def redact(url):
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}" + ("?..." if p.query else "")


def collect_notes():
    """-> ({note path: verdict or None}, Reels folder). Existing files only."""
    import review_new_reels as review

    from reel_pipeline.config import load_settings

    s = load_settings()
    state = json.loads((s.inbox_dir / "state.json").read_text("utf-8"))["items"]
    items = review.load_manifest()["items"]
    notes = {}
    for cid, rec in state.items():
        if rec.get("note_path"):
            notes[rec["note_path"]] = (items.get(cid) or {}).get("verdict")
    for v in items.values():
        if v.get("note_path"):
            notes[v["note_path"]] = v.get("verdict") or notes.get(v["note_path"])
    return {p: v for p, v in notes.items() if Path(p).exists()}, Path(s.vault_dir)


def urls_by_note(notes):
    """-> {url: [note paths]}"""
    found = {}
    for p in notes:
        try:
            text = Path(p).read_text("utf-8", errors="replace")
        except OSError:
            continue
        for u in extract(text):
            found.setdefault(u, []).append(p)
    return found


def probe(client, url):
    """-> (status code or None, error class or None). Never reads a body."""
    import httpx

    from reel_pipeline.text_fetcher import TextFetchError

    headers = {"User-Agent": "reel-knowledge-pipeline link-check"}
    try:
        try:
            code = client.head(url, timeout=TIMEOUT, headers=headers).status_code
            if code < 400:
                return code, None
        except httpx.TransportError as exc:
            if isinstance(exc, httpx.ConnectError):
                raise
            # Plenty of servers hang up on or ignore HEAD and answer GET fine.
        with client.stream("GET", url, timeout=TIMEOUT, headers=headers) as r:
            return r.status_code, None
    except TextFetchError:
        return None, "refused"
    except Exception as exc:
        return None, type(exc).__name__


def www_variant(url):
    """-> the www. form of a bare two-label host, else None. delta.com refuses, www answers."""
    p = urlsplit(url)
    if (p.hostname or "").count(".") != 1:
        return None
    return p._replace(netloc="www." + p.netloc).geturl()


def is_api_base(url):
    p = urlsplit(url)
    return bool(API_BASE.search(f"{p.hostname}{p.path}"))


def check_host(urls, probe_one=probe, gap=HOST_GAP):
    from reel_pipeline.text_fetcher import _guarded_client

    results = {}
    fails = 0
    with _guarded_client() as client:
        for i, u in enumerate(urls):
            if fails >= HOST_TRIP:
                results[u] = {"status": "error", "error": "host down, not tried"}
                continue
            if i:
                time.sleep(gap)
            code, err = probe_one(client, u)
            alt = www_variant(u) if err == "ConnectError" else None
            if alt:
                alt_code, _ = probe_one(client, alt)
                if alt_code is not None and alt_code < 400:
                    code, err = alt_code, None
            fails = fails + 1 if err else 0
            status = "refused" if err == "refused" else verdict(code)
            if err == "ConnectError":
                status = "dead"  # no DNS or nothing listening; the retest pass confirms
            if code == 404 and is_api_base(u):
                status = "answers"  # the server is there; a base URL just has no route
            results[u] = {"status": status, "code": code, "error": err}
    return results


def run_checks(urls, workers=16):
    by_host = {}
    for u in urls:
        by_host.setdefault(urlsplit(u).hostname, []).append(u)
    out = {}
    with ThreadPoolExecutor(workers) as pool:
        for r in pool.map(check_host, by_host.values()):
            out.update(r)
    return out


def directory_urls(found):
    """-> the URLs cited only by link-directory notes (an API or dataset index, say)."""
    per_note = Counter(n for names in found.values() for n in names)
    big = {n for n, c in per_note.items() if c >= DIRECTORY_MIN}
    return {u for u, names in found.items() if all(n in big for n in names)}, big


def tally(results, urls):
    counts = Counter(r["status"] for u, r in results.items() if u in urls)
    return ", ".join(f"{k} {counts[k]}" for k in sorted(counts)) or "none"


def report(when, results, skipped, found, notes):
    in_dirs, big = directory_urls(found)
    in_reels = set(results) - in_dirs
    dead = sorted(u for u, r in results.items() if r["status"] == "dead" and u not in in_dirs)
    dir_dead = sorted(u for u, r in results.items() if r["status"] == "dead" and u in in_dirs)
    active = ("try-now", "comment-for-link", "already-have")
    urgent = [u for u in dead if any(notes.get(n) in active for n in found[u])]
    lines = [
        "---",
        "tags: [reels, audit, link-recheck]",
        f"created: {when:%Y-%m-%d}",
        "source: scripts/recheck_links.py",
        "---",
        "",
        f"# {when:%Y-%m-%d} Reels Link Recheck",
        "",
        "## Executive summary",
        "",
        f"- **Notes scanned:** {len(notes)}. **Unique URLs found:** {len(found)}.",
        f"- **Requested:** {len(results)}.",
        f"- **Links in reels and other notes ({len(in_reels)}):** {tally(results, in_reels)}.",
        f"- **Links in {len(big)} link-directory notes ({len(results) - len(in_reels)}):** "
        f"{tally(results, in_dirs)}. These are third-party indexes, not links from reels: "
        + ", ".join(sorted(f"[[{Path(n).stem}]]" for n in big))
        + ".",
        "- **Not requested:** "
        + ", ".join(f"{k} {v}" for k, v in sorted(Counter(skipped.values()).items()))
        + ". GitHub is covered by the repo vetting report.",
        "- live means the URL answered, not that its content still matches the note. "
        "answers means an endpoint that wants a key or another method (401, 405), or an API base "
        "URL with no route of its own (404 on /v1), so it exists. "
        "blocked means the site refused an automated check (403, 429, 503), so it is unknown, "
        "not broken: this audit does not pose as a browser to get past that. "
        "error means no usable answer on two passes (timeouts, 5xx).",
        "- Many URLs come from speech-to-text or vision output, so some dead ones were "
        "never real links.",
        "",
        f"## Dead links on notes you may act on ({len(urgent)})",
        "",
    ]
    for u in urgent:
        names = sorted(
            f"[[{Path(n).stem}]] ({notes.get(n)})" for n in found[u] if notes.get(n) in active
        )
        lines.append(f"- <{redact(u)}>: {', '.join(names[:4])}")
    rest = [u for u in dead if u not in urgent]
    lines += ["", f"## Other dead links ({len(rest)})", ""]
    for u in rest:
        lines.append(f"- <{redact(u)}>: {', '.join(f'[[{Path(n).stem}]]' for n in found[u][:3])}")
    lines += ["", f"## Dead links in link-directory notes ({len(dir_dead)})", ""]
    lines += [f"- <{redact(u)}>" for u in dir_dead]
    blocked = Counter(
        urlsplit(u).hostname
        for u, r in results.items()
        if r["status"] == "blocked" and u not in in_dirs
    )
    lines += ["", "## Hosts that refused the check, outside link directories", ""]
    for host, n in blocked.most_common():
        lines.append(f"- {host}: {n}")
    return "\n".join(lines) + "\n"


def is_fresh(days):
    if not days or not HEALTH.exists():
        return False
    try:
        last = datetime.fromisoformat(json.loads(HEALTH.read_text("utf-8"))["checked_at"])
    except (ValueError, KeyError):
        return False
    return datetime.now(UTC) - last < timedelta(days=days)


def self_check():
    got = extract("see (https://a.io/x), https://b.io/y. [t](https://c.io/z_(1)) https://d.io/#f")
    assert got == {"https://a.io/x", "https://b.io/y", "https://c.io/z_(1)", "https://d.io/"}, got
    assert classify_target("https://www.instagram.com/reel/1/") == "note-only"
    assert classify_target("https://github.com/a/b") == "github"
    assert classify_target("https://img.shields.io/x") == "asset"
    assert classify_target("https://site.io/logo.PNG") == "asset"
    assert classify_target("https://example.com/") == "placeholder"
    assert classify_target("https://example-shop.com/product") == "placeholder"
    assert classify_target("https://claude.ai/chat/{uri}") == "placeholder"
    assert classify_target("https://r.jina.ai/URL") == "placeholder"
    assert classify_target("https://site.io/?mcp_token=abc") == "has-token"
    assert classify_target("https://site.io/page?id=4") is None
    assert classify_target("https://nodots/") == "malformed"
    assert [verdict(c) for c in (200, 301, 404, 410, 401, 405, 403, 500, None)] == [
        "live",
        "live",
        "dead",
        "dead",
        "answers",
        "answers",
        "blocked",
        "error",
        "error",
    ]
    assert redact("https://a.io/p?token=1#x") == "https://a.io/p?..."

    calls = []

    def down(client, url):
        calls.append(url)
        return None, "ConnectError"

    res = check_host([f"https://www.h.io/{i}" for i in range(8)], probe_one=down, gap=0)
    assert len(calls) == HOST_TRIP, calls
    assert res["https://www.h.io/7"]["error"] == "host down, not tried"
    assert res["https://www.h.io/0"]["status"] == "dead"

    assert www_variant("https://delta.com/us?a=1") == "https://www.delta.com/us?a=1"
    assert www_variant("https://docs.delta.com/") is None
    assert www_variant("https://www.delta.com/") is None

    def bare_refuses(client, url):
        return (200, None) if url.startswith("https://www.") else (None, "ConnectError")

    res = check_host(["https://delta.com/x"], probe_one=bare_refuses, gap=0)
    assert res["https://delta.com/x"]["status"] == "live", res

    def not_found(client, url):
        return 404, None

    res = check_host(
        ["https://o.ai/api/v1", "https://api.o.ai/v1beta/", "https://o.ai/docs/v3"], not_found, 0
    )
    assert res["https://o.ai/api/v1"]["status"] == "answers", res
    assert res["https://api.o.ai/v1beta/"]["status"] == "answers", res
    assert res["https://o.ai/docs/v3"]["status"] == "dead", res

    import httpx

    class Answer:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class HeadFails:
        def __init__(self, exc):
            self.exc = exc
            self.gets = 0

        def head(self, url, **kw):
            raise self.exc

        def stream(self, method, url, **kw):
            self.gets += 1
            return Answer()

    hangs = HeadFails(httpx.ReadTimeout("hung"))
    assert probe(hangs, "https://h.io/") == (200, None) and hangs.gets == 1
    refused = HeadFails(httpx.ConnectError("nope"))
    assert probe(refused, "https://h.io/") == (None, "ConnectError") and refused.gets == 0

    many = {f"https://d.io/{i}": ["big"] for i in range(DIRECTORY_MIN)}
    many["https://both.io/"] = ["big", "reel"]
    many["https://only.io/"] = ["reel"]
    in_dirs, big = directory_urls(many)
    assert big == {"big"} and "https://d.io/0" in in_dirs
    assert "https://both.io/" not in in_dirs and "https://only.io/" not in in_dirs
    print("self-check ok")


def main(argv):
    if "--self-check" in argv:
        return self_check()
    days = int(argv[argv.index("--if-older-than") + 1]) if "--if-older-than" in argv else 0
    if is_fresh(days):
        print(f"link health younger than {days} days, nothing to do")
        return
    notes, reels = collect_notes()
    found = urls_by_note(notes)
    targets, skipped = [], {}
    for u in found:
        why = classify_target(u)
        if why:
            skipped[u] = why
        else:
            targets.append(u)
    if "--limit" in argv:
        targets = targets[: int(argv[argv.index("--limit") + 1])]
    print(f"notes={len(notes)} urls={len(found)} requesting={len(targets)}", flush=True)
    print("link directories:", sorted(Path(n).stem for n in directory_urls(found)[1]), flush=True)
    results = run_checks(targets)
    again = [u for u, r in results.items() if r["status"] in ("dead", "error")]
    print(f"retesting {len(again)}", flush=True)
    results.update(run_checks(again))
    when = datetime.now(UTC)
    payload = {
        "checked_at": when.isoformat(),
        "skipped": dict(Counter(skipped.values())),
        "results": {
            redact(u): {**r, "notes": [Path(n).stem for n in found[u]]} for u, r in results.items()
        },
    }
    HEALTH.write_text(json.dumps(payload, indent=1), "utf-8")
    local = when.astimezone()  # the note is named for the owner's day, not UTC's
    out = reels / f"{local:%Y-%m-%d} Reels Link Recheck.md"
    out.write_text(report(local, results, skipped, found, notes), "utf-8")
    print(dict(Counter(r["status"] for r in results.values())), "->", out)


if __name__ == "__main__":
    main(sys.argv[1:])
