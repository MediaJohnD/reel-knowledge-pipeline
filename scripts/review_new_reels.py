(
    """Auto-review of newly captured Reel notes. Dry-run by default.

For each ledger record with status done that is not in data/review/reviewed.json:
read the full note, research every URL / named repo / tool with several tools
(gh api, Context7 REST, crawl4ai, SearXNG, yt-dlp), condense long text with local
Ollama, get the verdict from the pipeline's free LLM waterfall (never Claude), then
append a "## Review (auto, DATE)" section to the note, add a row to the vault digest
and record the manifest. Read-only on state.json (no ledger lock needed); note writes
are skipped while the worker's state.run_once.lock is held. Never raises out per item.

  --apply  write   --limit N (default 10)   --note <path>   --self-check
  --research-only <note>  full research + judge, print JSON to stdout, write nothing
  --rereview <note>       replace only that note's "## Review (auto," section, update digest """
    """+ manifest
  --gen-have              regenerate config/have.txt from what is installed
  --seed DIR  one-off: mark ids covered by batch-*/recheck-*.json in DIR as seeded
"""
)

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "review" / "reviewed.json"
DIGEST_NAME = "Reel Review Digest.md"  # in settings.vault_dir (the Reels folder)
PROMPT = ROOT / "config" / "prompts" / "review_reel.md"
YTDLP = ROOT / ".venv" / "Scripts" / "yt-dlp.exe"
CRAWL, SEARX, OLLAMA_MODEL = "http://127.0.0.1:11235", "http://127.0.0.1:8888", "qwen2.5:7b"
VERDICTS = {"try-now", "later", "skip", "already-have"}
LOGIN_WALLED = ("instagram.com", "facebook.com", "tiktok.com", "fb.watch")
YT_DOMAINS = ("youtube.com", "youtu.be")
URL_RE = re.compile(r"https?://[^\s<>)\]\"'`]+")
GH_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)")
OWNERS = {"mediajohnd"}  # vault owner's GitHub logins (lowercase); gh api user adds the live login
MIN_STARS = 25
SHORTENERS = (
    "bit.ly",
    "t.co",
    "tinyurl.com",
    "ow.ly",
    "buff.ly",
    "amzn.to",
    "geni.us",
    "rebrand.ly",
    "is.gd",
    "cutt.ly",
    "lnkd.in",
)
AFFILIATE = re.compile(
    r"[?&](aff\w*|ref|via|tag|utm_medium=affiliate)=|/(aff|go|refer|ref)/|affiliate", re.I
)
UNRELATED = ("delta.com",)
VENDORS = (
    "claude.com",
    "claude.ai",
    "anthropic.com",
    "openai.com",
    "google.com",
    "microsoft.com",
    "github.com",
    "youtube.com",
)
CATALOG = {"awesome", "index", "catalog", "list", "lists"}
INSTALL_RE = re.compile(
    r"\b(npx|npm\s+(?:i|install)|pip3?\s+install|uv\s+tool\s+install|uv\s+pip\s+install|cargo\s+install)\s+((?:-\S+\s+)*)([^\s;&|`'\"]+)",
    re.I,
)
PIPE_SH = re.compile(r"(curl|wget|iwr|irm)[^\n|]*\|\s*(sudo\s+)?(ba)?sh|\|\s*iex", re.I)
MAX_REPOS, MAX_SITES, MAX_TOOLS = 4, 4, 3
HAVE = ROOT / "config" / "have.txt"
USED = set()  # tools that actually ran this research; filled by the tool functions themselves
LOGIN_URL = re.compile(r"log-?in|sign-?in|sign-?up|/auth\b|/sso\b|/account", re.I)
LOGIN_TEXT = re.compile(r"(log ?in|sign ?in) to (continue|your)|enter your password", re.I)
LIB_FILES = {
    "package.json",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
    "setup.py",
    "requirements.txt",
}
LIB_TOPICS = {"library", "sdk", "framework", "cli", "api"}
PREFER = ("github.com", "npmjs.com", "pypi.org", "crates.io", "pkg.go.dev")
BAD_STEP = re.compile(
    r"\b(comment|like|follow|subscribe|watch|rewatch|dm)\b[^.]*\b(reel|video|post|creator|instagram|account|channel)\b|^\s*(comment|like|subscribe|watch)\b",
    re.I,
)
FIELDS = (
    "verdict",
    "businesses",
    "effort",
    "cost",
    "risk",
    "first_step",
    "evidence",
    "tools_used",
    "confidence",
)


def load_manifest():
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "items": {}}


def save_manifest(m):  # atomic: tmp + replace
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=MANIFEST.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=1)
    os.replace(tmp, MANIFEST)


def gh_name(r):
    return r.removesuffix(".git").rstrip(".-")


def select(state_items, manifest, limit):
    (
        """done records not yet reviewed (review_failed / needs-human retried up to 3 attempts, """
        """then parked), oldest first."""
    )
    done = manifest["items"]
    out = []
    for cid, r in state_items.items():
        if r.get("status") != "done" or not r.get("note_path"):
            continue
        m = done.get(cid)
        if m and (m["status"] not in ("review_failed", "needs-human") or m.get("attempts", 1) >= 3):
            continue
        out.append((r.get("added_at") or "", cid, r))
    return [(c, r) for _, c, r in sorted(out, key=lambda x: x[0])[:limit]]


def extract(text, tools):
    urls = list(dict.fromkeys(u.rstrip(".,;:") for u in URL_RE.findall(text)))
    repos = list(dict.fromkeys(f"{o}/{gh_name(r)}" for o, r in GH_RE.findall(text)))
    if isinstance(tools, str):  # frontmatter stores tools_mentioned as a JSON-array string
        try:
            tools = json.loads(tools)
        except ValueError:
            tools = [tools]
    return urls, repos, [t for t in (tools or []) if isinstance(t, str)]


# ---- tools (each returns text or None; failures are recorded, never raised) ----
def _run(cmd, timeout=40):
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace"
        )
        return p.stdout if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def gh_api(path, raw=False):
    cmd = ["gh", "api", path] + (["-H", "Accept: application/vnd.github.raw+json"] if raw else [])
    out = _run(cmd)
    if out is None:
        return None
    USED.add("gh")
    return out if raw else json.loads(out)


def gh_repo(full):
    info = gh_api(f"repos/{full}")
    if not info:
        return {"full_name": full, "exists": False}
    rel = gh_api(f"repos/{full}/releases/latest") or {}
    com = gh_api(f"repos/{full}/commits?per_page=1") or [{}]
    sec = (
        gh_api(
            f"search/issues?q={quote(f'repo:{full} is:issue is:open label:security')}&per_page=1"
        )
        or {}
    )
    readme = (gh_api(f"repos/{full}/readme", raw=True) or "")[:6000]
    files = [f.get("name") for f in (gh_api(f"repos/{full}/contents") or []) if isinstance(f, dict)]
    return {
        "full_name": info["full_name"],
        "exists": True,
        "stars": info.get("stargazers_count"),
        "license": (info.get("license") or {}).get("spdx_id"),
        "archived": info.get("archived"),
        "pushed_at": info.get("pushed_at"),
        "last_commit": (com[0].get("commit", {}).get("committer") or {}).get("date"),
        "last_release": rel.get("tag_name"),
        "open_security_issues": sec.get("total_count"),
        "description": info.get("description"),
        "topics": info.get("topics") or [],
        "files": files,
        "url": info.get("html_url"),
        "pipe_to_shell_in_readme": bool(PIPE_SH.search(readme)),
        "readme_head": readme[:1500],
    }


def http(method, url, **kw):
    import httpx

    try:
        return httpx.request(method, url, timeout=kw.pop("timeout", 30), **kw)
    except httpx.HTTPError:
        return None


def crawl_auth():
    (
        """crawl4ai needs a bearer token: env CRAWL4AI_API_TOKEN, else the crawl4ai MCP server's """
        """Authorization header. Never printed."""
    )
    if os.environ.get("CRAWL4AI_API_TOKEN"):
        return {"Authorization": "Bearer " + os.environ["CRAWL4AI_API_TOKEN"]}
    try:
        h = (
            json.loads((Path.home() / ".claude.json").read_text("utf-8"))["mcpServers"][
                "crawl4ai"
            ].get("headers")
            or {}
        )
        return {"Authorization": h["Authorization"]} if "Authorization" in h else {}
    except (OSError, ValueError, KeyError):
        return {}


def crawl(url):
    r = http("POST", f"{CRAWL}/md", json={"url": url, "f": "fit"}, headers=crawl_auth(), timeout=60)
    if r is not None and r.status_code == 200:
        try:
            t = (r.json().get("markdown") or "")[:8000] or None
            USED.add("crawl4ai") if t else None
            return t
        except ValueError:
            return None
    return None


def searx(q, n=8):
    """-> [(title, url, snippet)]"""
    r = http("GET", f"{SEARX}/search", params={"q": q, "format": "json"})
    try:
        out = (
            [(x["title"], x["url"], x.get("content") or "") for x in r.json()["results"][:n]]
            if r is not None and r.status_code == 200
            else []
        )
    except (ValueError, KeyError):
        return []
    USED.add("searxng") if out else None
    return out


def tokens(name):
    return [
        t
        for t in re.findall(r"[a-z0-9]+", name.lower())
        if len(t) >= 3 and t not in ("the", "app", "com", "official")
    ]


def host_of(u):
    return re.sub(r"^https?://(www\.)?", "", u).split("/")[0].lower()


def on_domain(u, domains):
    """URL's real hostname is one of `domains` or a subdomain of one (not a substring match)."""
    h = (urlsplit(u if "://" in u else f"https://{u}").hostname or "").lower()
    return any(h == d or h.endswith(f".{d}") for d in domains)


def phrase_in(text, name):
    (
        """Full tool-name phrase (words in order, any separator; or the squashed form) appears """
        """in text."""
    )
    words = re.findall(r"[a-z0-9]+", name.lower())
    if not words:
        return False
    return " ".join(words) in " ".join(re.findall(r"[a-z0-9]+", text.lower())) or (
        len(words) > 1 and "".join(words) in squash(text)
    )


def vendor_generic(url, name):
    (
        """A big-vendor page (claude.com...) is no evidence for a third-party tool unless the """
        """tool name is the vendor's."""
    )
    h = host_of(url)
    return any(
        h == v or h.endswith("." + v)
        for v in VENDORS
        if v not in ("github.com", "youtube.com") and v.split(".")[0] not in squash(name)
    )


def relevant(title, url, snippet, name):
    (
        """Search hit must contain the FULL tool-name phrase in title/url/snippet; no login """
        """walls, unrelated domains or generic vendor pages."""
    )
    h = host_of(url)
    if (
        not phrase_in(f"{title} {url} {snippet}", name)
        or LOGIN_URL.search(url)
        or re.search(r"log ?in|sign ?in", title, re.I)
    ):
        return False
    if any(h.endswith(d) for d in LOGIN_WALLED + UNRELATED) or vendor_generic(url, name):
        return False
    return True


def repo_ok(r, tools):
    """Repo counts as independent evidence only with a name match to a tool or MIN_STARS."""
    if not r.get("exists"):
        return False
    rn = squash(r["full_name"].split("/")[-1])
    return (r.get("stars") or 0) >= MIN_STARS or any(squash(t) and squash(t) in rn for t in tools)


def pref(url, toks):
    h = host_of(url)
    return 2 if any(h.endswith(d) for d in PREFER) else 1 if any(t in h for t in toks) else 0


def usable(text, toks):
    """Crawled page must mention the tool and not be a login wall."""
    return (
        bool(text)
        and all(t in text.lower() for t in toks)
        and not (len(text) < 1500 and LOGIN_TEXT.search(text))
    )


def is_library(repo):
    (
        """Context7 gate part 1: a GitHub repo was found AND it ships a package manifest or """
        """has a library-ish topic."""
    )
    return bool(
        repo
        and repo.get("exists")
        and (LIB_FILES & set(repo.get("files") or []) or LIB_TOPICS & set(repo.get("topics") or []))
    )


def on_registry(name, repo=None, get=None):
    (
        """Registry package exists AND its repository URL is a GitHub repo matching `repo` """
        """(or, if none known, the tool name)."""
    )
    get = get or http
    n = name.strip().lower()
    if not re.fullmatch(r"[a-z0-9][\w.-]*", n):
        return False
    for u in (f"https://registry.npmjs.org/{n}", f"https://pypi.org/pypi/{n}/json"):
        r = get("GET", u, timeout=10)
        if r is None or r.status_code != 200:
            continue
        try:
            j = r.json()
        except ValueError:
            continue
        urls = [
            str(
                (j.get("repository") or {}).get("url", "")
                if isinstance(j.get("repository"), dict)
                else j.get("repository", "")
            )
        ]
        urls += [str(v) for v in ((j.get("info") or {}).get("project_urls") or {}).values()]
        for m in (GH_RE.search(x) for x in urls):
            if m and (
                (repo and f"{m.group(1)}/{gh_name(m.group(2))}".lower() == repo.lower())
                or (not repo and squash(gh_name(m.group(2))) == squash(n))
            ):
                return True
    return False


def norm(u):
    """Dedup key: scheme + host lowercased, path/query case preserved (bit.ly/Ab1 != bit.ly/ab1)."""
    u = str(u).strip().split("#")[0].rstrip("/")
    m = re.match(r"(https?://)([^/?]*)(.*)$", u, re.I)
    return m.group(1).lower() + m.group(2).lower() + m.group(3) if m else u


def load_have():
    try:
        return [
            line.split("#")[0].strip()
            for line in HAVE.read_text("utf-8").splitlines()
            if line.split("#")[0].strip()
        ]
    except OSError:
        return []


def squash(x):
    return re.sub(r"[^a-z0-9]", "", str(x).lower())


def gen_have():
    rows = [
        "searxng",
        "khoj",
        "mem0",
        "ollama",
        "crawl4ai",
        "obsidian",
        "context7",
        "playwright",
        "apollo",
        "resend",
        "cloudflare",
        "github cli",
        "claude code",
        "yt-dlp",
    ]
    home = Path.home()
    try:
        rows += [
            k.split("@")[0]
            for k, v in (
                json.loads((home / ".claude" / "settings.json").read_text("utf-8")).get(
                    "enabledPlugins"
                )
                or {}
            ).items()
            if v
        ]
    except (OSError, ValueError):
        pass
    try:
        rows += list(json.loads((home / ".claude.json").read_text("utf-8")).get("mcpServers") or {})
    except (OSError, ValueError):
        pass
    rows += [
        c
        for c in ("gh", "codex", "gemini", "ollama", "hs", "apollo", "docker", "yt-dlp")
        if shutil.which(c)
    ]
    rows = list(dict.fromkeys(r.lower() for r in rows))
    HAVE.write_text(
        "# generated by review_new_reels.py --gen-have; names only, one per line\n"
        + "\n".join(rows)
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {HAVE} ({len(rows)} names)")


def context7(name, key):
    """-> (text|None, status). Missing/invalid key or quota => unavailable, never an error."""
    if not key:
        return None, "unavailable(no key)"
    h = {"Authorization": f"Bearer {key}"}
    r = http(
        "GET",
        "https://context7.com/api/v2/libs/search",
        params={"libraryName": name, "query": name},
        headers=h,
    )
    if r is None or r.status_code != 200:
        return None, f"unavailable({r.status_code if r is not None else 'net'})"
    try:
        USED.add("context7")
        lib = r.json()["results"][0]["id"]
        d = http(
            "GET",
            "https://context7.com/api/v2/context",
            params={"libraryId": lib, "query": "install usage", "type": "txt"},
            headers=h,
        )
        return (
            (d.text[:3000], f"ok {lib}")
            if d is not None and d.status_code == 200
            else (None, f"unavailable({d.status_code if d is not None else 'net'})")
        )
    except (ValueError, KeyError, IndexError):
        return None, "unavailable(no match)"


def youtube(url):
    out = _run([str(YTDLP), "--skip-download", "--dump-json", "--no-warnings", url], 90)
    if not out:
        return None
    j = json.loads(out)
    USED.add("yt-dlp")
    desc = j.get("description") or ""
    caps = ""
    with tempfile.TemporaryDirectory() as t:
        _run(
            [
                str(YTDLP),
                "--skip-download",
                "--write-auto-subs",
                "--sub-langs",
                "en.*",
                "--no-warnings",
                "-o",
                str(Path(t, "s")),
                url,
            ],
            90,
        )
        for f in Path(t).glob("*.vtt"):
            caps = re.sub(
                r"<[^>]+>|^\d.*-->.*$|WEBVTT.*", "", f.read_text("utf-8", "replace"), flags=re.M
            )[:6000]
    return {
        "title": j.get("title"),
        "description": desc[:3000],
        "captions": caps,
        "links": URL_RE.findall(desc),
    }


def condense(settings, text, what):
    if len(text) <= 2500:
        return text
    r = http(
        "POST",
        f"{settings.llm.ollama_host}/api/generate",
        timeout=300,
        json={
            "model": OLLAMA_MODEL,
            "stream": False,
            "options": {"num_predict": 500, "num_ctx": 8192},
            "prompt": (
                "Condense to under 1500 characters, facts only (what it is, install, license, "
                f"caveats). Source: {what}\n\n{text[:9000]}"
            ),
        },
    )
    try:
        out = r.json()["response"][:1800]
        USED.add("ollama")
        return out
    except (AttributeError, ValueError, KeyError):
        return text[:1800] + " [ollama condense unavailable; truncated]"


def research(settings, note_text, fm, key, have=()):
    (
        """-> (evidence dict, tools_used list, fetched set, independent set). Every step """
        """degrades to an 'unchecked' record."""
    )
    USED.clear()
    urls, repos, tools = extract(note_text, fm.get("tools_mentioned"))
    hs = {squash(h) for h in have}
    tools = [t for t in tools if squash(t) not in hs]  # already installed: no research needed
    ev = {"repos": [], "sites": [], "youtube": [], "docs": [], "unchecked": [], "fallbacks": []}
    fetched, indep = set(), set()
    src = fm.get("source_url")
    if src and src not in urls:
        urls.append(src)
    queue, ytn, desc_links = (
        list(urls),
        0,
        set(),
    )  # desc_links: YouTube description links, must earn relevance
    for u in list(queue):
        if on_domain(u, YT_DOMAINS):
            y = youtube(u)
            if y:
                fetched.add(
                    norm(u)
                )  # the source video itself: fetched, but never independent evidence
                ev["youtube"].append(
                    {
                        "url": u,
                        "title": y["title"],
                        "description": condense(settings, y["description"], u),
                        "captions": condense(settings, y["captions"], u) if y["captions"] else "",
                    }
                )
                new = [link.rstrip(".,;:") for link in y["links"] if link not in queue]
                ytn += len(new)
                # follow EVERY description link, but each must pass the relevance check below
                queue += new
                desc_links |= set(new)
            else:
                ev["unchecked"].append(f"{u} (yt-dlp failed)")
    tool_repo, cands = {}, []  # tool -> repo name; (url, tokens) search-derived site candidates
    for name in tools[:MAX_TOOLS]:
        toks = tokens(name)
        hits = sorted(
            (h for h in searx(f"{name} official site docs") if relevant(*h, name)),
            key=lambda h: -pref(h[1], toks),
        )
        for full in repos:
            if toks and all(t in full.lower() for t in toks):
                tool_repo[name] = full
        gh = next((GH_RE.search(h[1]) for h in hits if GH_RE.search(h[1])), None)
        if gh and name not in tool_repo:
            tool_repo[name] = f"{gh.group(1)}/{gh_name(gh.group(2))}"
            repos.append(tool_repo[name])
        site = next(
            (h for h in hits if not GH_RE.search(h[1]) and not on_domain(h[1], YT_DOMAINS)),
            None,
        )
        if site:
            cands.append((site[1], toks))
        elif not hits:
            ev["unchecked"].append(f"{name} (no relevant search hit)")
    for u in queue:
        for o, r in GH_RE.findall(u):
            repos.append(f"{o}/{gh_name(r)}")
    for full in list(dict.fromkeys(repos))[: MAX_REPOS + min(ytn, 15)]:
        r = gh_repo(full)
        if r.get("exists"):
            fetched.add(norm(r["url"]))
            if repo_ok(
                r, tools
            ):  # keyword-only / tiny unrelated repos never count as independent evidence
                indep.add(norm(r["url"]))
        r["readme_head"] = (
            condense(settings, r.get("readme_head", ""), full) if r.get("readme_head") else ""
        )
        ev["repos"].append(r)
    for name in tools[:MAX_TOOLS]:  # Context7 only for libraries/SDKs/frameworks/CLIs
        repo = next(
            (r for r in ev["repos"] if r["full_name"].lower() == tool_repo.get(name, "").lower()),
            None,
        )
        if is_library(repo) or (not repo and on_registry(name, tool_repo.get(name))):
            txt, st = context7(name, key)
            ev["docs"].append(
                {
                    "library": name,
                    "context7": st,
                    "text": condense(settings, txt, name) if txt else "",
                }
            )
        else:
            ev["docs"].append({"library": name, "context7": "skipped(not a library)", "text": ""})
    nsites, cap = 0, MAX_SITES + min(ytn, 15)
    for u, toks in [(u, None) for u in queue] + cands:
        host = host_of(u)
        if on_domain(u, ("github.com", *YT_DOMAINS)) or norm(u) in {
            norm(x["url"]) for x in ev["sites"]
        }:
            continue
        if any(host.endswith(d) for d in LOGIN_WALLED):
            ev["unchecked"].append(f"{u} (login-walled, not fetched)")
        elif nsites < cap:
            nsites += 1
            t = crawl(u)
            dl = u in desc_links and toks is None
            if (
                t
                and dl
                and (
                    not any(phrase_in(t, n) for n in tools)
                    if tools
                    else (any(host.endswith(d) for d in SHORTENERS) or AFFILIATE.search(u))
                )
            ):
                ev["unchecked"].append(
                    f"{u} (description link: shortener/affiliate or page does not mention "
                    "the tool; dropped)"
                )
                continue
            if t and tools and all(vendor_generic(u, n) for n in tools):
                ev["unchecked"].append(
                    f"{u} (generic vendor page, not evidence for the tool; dropped)"
                )
                continue
            if (
                t
                and (toks is None or usable(t, toks))
                and not (len(t) < 1500 and LOGIN_TEXT.search(t))
            ):
                src_, txt = "crawl4ai", t
            elif t:
                ev["unchecked"].append(f"{u} (irrelevant or login wall, dropped)")
                continue
            else:  # crawl4ai failed: fall back to SearXNG snippets for that host, and say so
                if dl:  # description links get no SearXNG fallback: unverifiable
                    ev["unchecked"].append(f"{u} (description link, crawl4ai failed)")
                    continue
                sn = " ".join(
                    c
                    for _, hu, c in searx(f"site:{host}")
                    if host_of(hu) == host and (toks is None or all(k in c.lower() for k in toks))
                )
                if not sn:
                    ev["unchecked"].append(f"{u} (crawl4ai failed)")
                    continue
                src_, txt = "searxng-fallback", sn
                ev["fallbacks"].append(f"{u}: crawl4ai failed, used SearXNG snippets")
            ev["sites"].append({"url": u, "source": src_, "text": condense(settings, txt, u)})
            fetched.add(norm(u))
            indep.add(norm(u))
    return ev, sorted(USED), fetched, indep


class LlmBad(Exception):
    pass


def validate(j, names):
    """-> error string or None. names = candidate tool/repo names the first_step must act on."""
    if not isinstance(j, dict):
        return "not an object"
    miss = [f for f in FIELDS if f not in j]
    if miss:
        return f"missing fields {miss}"
    if j["verdict"] not in VERDICTS:
        return f"bad verdict {j['verdict']!r}"
    if not all(isinstance(j[k], list) for k in ("businesses", "evidence", "tools_used")):
        return "businesses/evidence/tools_used must be arrays"
    if (
        not isinstance(j["first_step"], str)
        or not j["first_step"].strip()
        or BAD_STEP.search(j["first_step"])
    ):
        return (
            "first_step must be a concrete action on the tool itself "
            "(not comment/like/follow/watch the reel)"
        )
    toks = {t for n in names for t in tokens(n.split("/")[-1])}
    if toks and j["verdict"] in ("try-now", "later") and not any(t in j["first_step"].lower() for t in toks):
        return f"first_step must name the tool or repo (one of {sorted(names)})"
    return None


def is_catalog(r):
    return bool(
        CATALOG
        & set(
            re.findall(
                r"[a-z0-9]+",
                (
                    r.get("full_name", "").split("/")[-1] + " " + " ".join(r.get("topics") or [])
                ).lower(),
            )
        )
    )


def get_owners():
    """Vault owner's GitHub logins: config default plus the live `gh api user` login."""
    me = gh_api("user")
    return OWNERS | (
        {str(me["login"]).lower()} if isinstance(me, dict) and me.get("login") else set()
    )


def finalize(j, fetched, indep, used, names, have_hits, meta=None, owners=None):
    (
        """Code-enforced rules on validated judge JSON: real evidence only, note-only cap, """
        """already-have, real tools_used."""
    )
    cited = []
    for e in j["evidence"]:
        u = e.get("url") if isinstance(e, dict) else e
        if isinstance(u, str) and norm(u) in fetched and norm(u) not in cited:
            cited.append(norm(u))
    j["evidence"] = cited
    j["tools_used"] = used + ["llm-waterfall"]
    if j["verdict"] == "try-now" and j.get("confidence") == "low":
        j["verdict"], j["cap_reason"] = "later", "low confidence"
    if not any(u in indep for u in cited):
        j["cap_reason"] = "note-only claims, unverified"
        if j["verdict"] == "try-now":
            j["verdict"] = "later"
    owners = OWNERS if owners is None else owners
    crepos = [
        m.group(1).lower() + "/" + gh_name(m.group(2)).lower()
        for m in (GH_RE.search(u) for u in cited)
        if m
    ]
    if crepos and all(r.split("/")[0] in owners for r in crepos):
        j["verdict"], j["cap_reason"] = "already-have", "all cited repos are the vault owner's"
    if j["verdict"] == "try-now" and any(
        is_catalog((meta or {}).get(r) or {"full_name": r}) for r in crepos
    ):
        j["verdict"], j["cap_reason"] = "later", "cited repo is a catalog/index/awesome-list"
    tools = [n for n in names if "/" not in n]
    if tools and len(have_hits) >= len(tools) and j["verdict"] != "skip":
        j["verdict"] = "already-have"
    return j


def parse_json(raw):
    (
        """Tolerant: object may be fenced or truncated mid-string/array; close it and keep """
        """what parses. -> dict or None."""
    )
    i = (raw or "").find("{")
    if i < 0:
        return None
    body = raw[i:]
    cuts = [len(body)] + [m.start() for m in re.finditer(r",\s*\"", body)][::-1][:6]
    for c in cuts:
        for tail in ("", "}", '"}', "]}", '"]}'):
            try:
                j = json.loads(body[:c] + tail)
                return j if isinstance(j, dict) else None
            except ValueError:
                pass
    return None


def bad_install(step, get=None):
    (
        """Reject a first_step whose npx/npm/pip/uv tool/cargo package is absent from its """
        """registry (404). Network errors do not reject."""
    )
    get = get or http
    for cmd, _, pkg in INSTALL_RE.findall(step):
        if pkg.startswith((".", "/", "git+", "http")) or ":" in pkg:
            continue
        cmd = cmd.lower()
        if cmd.startswith(("npx", "npm")):
            pkg = re.sub(r"(?<=.)@[^/@]*$", "", pkg)
            url = f"https://registry.npmjs.org/{pkg.replace('/', '%2F')}"
        elif cmd.startswith("cargo"):
            url = f"https://crates.io/api/v1/crates/{pkg}"
        else:
            pkg = re.split(r"[\[<>=!~]", pkg)[0]
            url = f"https://pypi.org/pypi/{pkg}/json"
        r = get(
            "GET",
            url,
            timeout=10,
            **({"headers": {"User-Agent": "review_new_reels"}} if "crates" in url else {}),
        )
        if r is not None and r.status_code == 404:
            return (
                f"install command uses {pkg!r}, which does not exist on the registry; prefer "
                "`gh repo clone <owner/repo>` or the README's command"
            )
    return None


def judge(settings, note_text, ev, fetched, indep, used, have):
    (
        """-> finalized judge dict, or raises LlmBad after one retry on invalid output (LLM """
        """outage propagates as-is)."""
    )
    from reel_pipeline.llm_client import call_llm

    pre, _, tail = PROMPT.read_text(encoding="utf-8").partition("<!-- CACHE:BOUNDARY -->")
    tools = ev.get("_tools") or []
    names = tools + [r["full_name"] for r in ev["repos"] if r.get("exists")]
    hs = {squash(h) for h in have}
    hits = [t for t in tools if squash(t) in hs]
    meta = {r["full_name"].lower(): r for r in ev["repos"] if r.get("exists")}
    owners = get_owners()
    view = {k: v for k, v in ev.items() if not k.startswith("_")}
    body = (
        f"NOTE:\n{note_text[:7000]}\n\nEVIDENCE (json):\n"
        f"{json.dumps(dict(view, fetched_urls=sorted(fetched)), ensure_ascii=False)[:9000]}"
        f"\n\nALREADY HAVE (installed stack):\n{', '.join(have)}\n"
        f"CANDIDATES MATCHING ALREADY-HAVE: {hits or 'none'}"
    )
    err = None
    for _ in range(2):
        raw = call_llm(
            settings,
            tail
            + body
            + (
                f"\n\nYour previous answer was rejected: {err}. Return corrected JSON only."
                if err
                else ""
            ),
            model="review",
            max_tokens=3000,
            json_mode=True,
            static_prefix=pre,
        )
        j = parse_json(raw)
        err = (
            validate(j, names)
            if j is not None
            else (
                "unparseable or truncated JSON, answer with SHORTER JSON "
                f"(under 1500 chars): {raw[:120]!r}"
            )
        )
        err = err or bad_install(j["first_step"])
        if not err:
            return finalize(j, fetched, indep, used, names, hits, meta, owners)
    raise LlmBad(err)


def render(j, ev, used):
    def s(x):
        return str(x).replace("|", "/").replace("\n", " ")

    ev_lines = []
    for r in ev["repos"]:
        ev_lines.append(
            f"- [{r['full_name']}](https://github.com/{r['full_name']}): "
            + (
                f"{r['stars']} stars, license {r['license']}, archived={r['archived']}, "
                f"last commit {r['last_commit']}, "
                f"release {r['last_release']}, open security issues {r['open_security_issues']}, "
                f"pipe-to-shell in README={r['pipe_to_shell_in_readme']}"
                if r.get("exists")
                else "not found via gh api (unchecked)"
            )
        )
    ev_lines += [f"- <{x['url']}> ({x['source']})" for x in ev["sites"]]
    ev_lines += [f"- <{y['url']}> (yt-dlp: {s(y['title'])})" for y in ev["youtube"]]
    ev_lines += [f"- {d['library']}: context7 {d['context7']}" for d in ev["docs"]]
    ev_lines += [f"- fallback: {u}" for u in ev.get("fallbacks", [])]
    ev_lines += [f"- unchecked: {u}" for u in ev["unchecked"]]
    return (
        f"\n\n## Review (auto, {date.today().isoformat()})\n\n"
        f"- verdict: {j['verdict']}"
        + (f" (capped: {j['cap_reason']})" if j.get("cap_reason") else "")
        + "\n"
        f"- confidence: {s(j.get('confidence'))}\n"
        f"- businesses: {', '.join(j.get('businesses') or []) or 'none'}\n"
        f"- effort: {s(j.get('effort'))}\n- cost: {s(j.get('cost'))}\n- risk: {s(j.get('risk'))}\n"
        f"- first_step: {s(j.get('first_step'))}\n- reasoning: {s(j.get('reasoning'))}\n"
        f"- tools used: {', '.join(used) or 'none'}\n\nEvidence:\n"
        + ("\n".join(ev_lines) or "- none found (unchecked)")
        + "\n"
    )


def replace_review(text, section):
    (
        """Swap only the '## Review (auto,' section (to next '## ' heading or EOF); """
        """everything else stays byte-identical."""
    )
    pat = re.compile(
        r"(?:\r?\n){2}## Review \(auto,.*?(?=(?:\r?\n)## (?!Review \(auto,)|\Z)", re.S
    )  # CRLF-tolerant; duplicate review sections collapse
    first = pat.search(text)
    if not first:
        return text + section
    g = first.group(0)
    lead = re.match(r"(?:\r?\n){2}", g).group(0)  # keep the note's own separator bytes
    tail_nl = re.search(r"(?:\r?\n)*\Z", g).group(0)
    return (
        text[: first.start()]
        + lead
        + section.lstrip("\r\n").rstrip("\r\n")
        + tail_nl
        + pat.sub("", text[first.end() :])
    )


def upsert_digest_row(settings, title, cid, j, note_path):
    digest = settings.vault_dir / DIGEST_NAME
    row = (
        f"| {date.today().isoformat()} | {title.replace('|', '/')} | `{j['verdict']}` | "
        f"{', '.join(j.get('businesses') or [])} | {str(j.get('effort'))[:30]} | `{cid}` | "
        f"`{Path(note_path).name}` |\n"
    )
    sep = "|---|---|---|---|---|---|---|\n"
    head = (
        "# Reel Review Digest\n\nNewest first. Written by `review_new_reels.py`.\n\n"
        "| date | reel | verdict | businesses | effort | id | note |\n" + sep
    )
    text = digest.read_text("utf-8") if digest.exists() else head
    # no duplicate rows
    text = "".join(line for line in text.splitlines(True) if f"| `{cid}` |" not in line)
    i = text.index(sep) + len(sep)
    digest.parent.mkdir(parents=True, exist_ok=True)
    digest.write_text(text[:i] + row + text[i:], encoding="utf-8")


def lock_free(settings):
    import filelock

    lk = filelock.FileLock(str(settings.inbox_dir / "state.run_once.lock"))
    try:
        lk.acquire(timeout=0)
    except filelock.Timeout:
        return None
    return lk


def find_note(settings, s):
    p = Path(s)
    if p.is_file():
        return p
    hits = list(Path(settings.vault_dir).parents[1].rglob(p.name))
    return hits[0] if len(hits) == 1 else None


def analyse(settings, text, fm, key):
    """Full research + judge. -> dict(status reviewed|needs-human, j, ev, used, fetched)."""
    text = text.split("\n\n## Review (auto,")[0].split("\r\n\r\n## Review (auto,")[
        0
    ]  # never research/judge our own previous review (feedback loop)
    have = load_have()
    ev, used, fetched, indep = research(settings, text, fm, key, have)
    ev["_tools"] = extract("", fm.get("tools_mentioned"))[2][:MAX_TOOLS]
    try:
        j = judge(settings, text, ev, fetched, indep, used, have)
    except LlmBad as e:
        return {
            "status": "needs-human",
            "error": f"judge output invalid after retry: {e}",
            "j": None,
            "ev": ev,
            "used": used,
            "fetched": sorted(fetched),
        }
    return {
        "status": "reviewed",
        "j": j,
        "ev": ev,
        "used": j["tools_used"],
        "fetched": sorted(fetched),
        "error": None,
    }


def result_of(a, p):
    ev, j = a["ev"], a["j"]
    r = {
        "status": a["status"],
        "note_path": str(p),
        "tools_used": a["used"],
        "error": a["error"],
        "unchecked": ev["unchecked"],
        "fallbacks": ev["fallbacks"],
        "docs": [{"library": d["library"], "context7": d["context7"]} for d in ev["docs"]],
        "sites": [x["url"] for x in ev["sites"]],
        "repos": [
            {
                k: x.get(k)
                for k in (
                    "full_name",
                    "exists",
                    "stars",
                    "license",
                    "last_commit",
                    "open_security_issues",
                )
            }
            for x in ev["repos"]
        ],
    }
    if j:
        r.update(verdict=j["verdict"], businesses=j.get("businesses"))
    return r


def review_one(settings, cid, rec, apply, key):
    from reel_pipeline.obsidian_writer import read_frontmatter

    p = find_note(settings, rec["note_path"]) or Path(rec["note_path"])
    if not p.is_file():
        return {"status": "skipped", "note_path": str(p), "error": "note not found"}
    text = p.read_bytes().decode("utf-8")
    fm = read_frontmatter(p) or {}
    if "## Review (auto," in text:
        return {"status": "skipped", "note_path": str(p), "error": "already has review section"}
    if not apply:
        urls, repos, tools = extract(text, fm.get("tools_mentioned"))
        print(
            f"  dry-run {cid}: {len(urls)} urls, repos={repos[:MAX_REPOS]}, "
            f"tools={tools[:MAX_TOOLS]}"
        )
        return None
    a = analyse(settings, text, fm, key)
    if a["status"] == "needs-human":
        return result_of(a, p)  # nothing written to the note or digest
    write_review(settings, cid, p, text, fm, a)
    return result_of(a, p)


def write_review(settings, cid, p, text, fm, a, replace=False):
    lk = lock_free(settings)
    if lk is None:
        raise RuntimeError("run_once lock held; retry next run")
    sec = render(a["j"], a["ev"], a["used"])
    sec = sec.replace("\n", "\r\n" if "\r\n" in text else "\n")
    try:
        p.write_bytes((replace_review(text, sec) if replace else text + sec).encode("utf-8"))
    finally:
        lk.release()
    upsert_digest_row(settings, fm.get("title") or p.stem, cid, a["j"], p)


def seed(settings, d, state):
    names = set()
    for f in Path(d).glob("batch-*.json"):
        names |= {Path(r["path"]).name.lower() for r in json.loads(f.read_text("utf-8"))}
    for f in Path(d).glob("recheck-[0-9]*.json"):
        names |= {Path(r["note"]).name.lower() for r in json.loads(f.read_text("utf-8"))}
    m, n = load_manifest(), 0
    for cid, r in state.items():
        if (
            r.get("status") == "done"
            and r.get("note_path")
            and Path(r["note_path"]).name.lower() in names
            and cid not in m["items"]
        ):
            m["items"][cid] = {
                "status": "seeded",
                "note_path": r["note_path"],
                "reviewed_at": now(),
                "note": "covered by 2026-09 deep read/link recheck",
            }
            n += 1
    save_manifest(m)
    print(f"seeded {n} of {len(names)} names")


def research_only(settings, note, key):
    (
        """Full research + judge; prints result JSON (incl. rendered markdown) to stdout. """
        """Writes nothing."""
    )
    from reel_pipeline.obsidian_writer import read_frontmatter

    p = find_note(settings, note)
    if not p:
        return print(f"note not found: {note}")
    a = analyse(settings, p.read_bytes().decode("utf-8"), read_frontmatter(p) or {}, key)
    out = result_of(a, p)
    out.update(
        fetched_urls=a["fetched"],
        judge=a["j"],
        markdown=render(a["j"], a["ev"], a["used"]) if a["j"] else None,
    )
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(out, indent=1, ensure_ascii=False))


def rereview(settings, note, state, key):
    (
        """Replace only the note's existing review section; update its digest row and """
        """manifest entry in place."""
    )
    from reel_pipeline.obsidian_writer import read_frontmatter

    p = find_note(settings, note)
    cid = (
        next(
            (
                c
                for c, r in state.items()
                if r.get("note_path") and Path(r["note_path"]).name.lower() == p.name.lower()
            ),
            None,
        )
        if p
        else None
    )
    if not cid:
        return print(f"note or ledger id not found: {note}")
    text = p.read_bytes().decode("utf-8")
    fm = read_frontmatter(p) or {}
    a = analyse(settings, text, fm, key)
    res = result_of(a, p)
    if a["status"] == "reviewed":
        write_review(settings, cid, p, text, fm, a, replace=True)
    m = load_manifest()
    res.update(reviewed_at=now(), attempts=m["items"].get(cid, {}).get("attempts", 0) + 1)
    m["items"][cid] = res
    save_manifest(m)
    print(f"  {cid}: {res['status']} {res.get('verdict', '')} {res.get('error') or ''}")


def now():
    return datetime.now(UTC).isoformat(timespec="seconds")


def self_check():
    global MANIFEST
    assert on_domain("https://www.youtube.com/watch?v=x", YT_DOMAINS)
    assert on_domain("youtu.be/x", YT_DOMAINS)
    assert on_domain("https://github.com/a/b", ["github.com"])
    assert not on_domain("https://evil.com/?u=youtube.com", YT_DOMAINS)
    assert not on_domain("https://github.com.evil.io/a", ["github.com"])
    assert not on_domain("https://notyoutube.com/x", YT_DOMAINS)
    with tempfile.TemporaryDirectory() as t:
        MANIFEST = Path(t, "review", "reviewed.json")
        m = load_manifest()
        assert m["items"] == {}
        m["items"]["a"] = {"status": "reviewed"}
        m["items"]["b"] = {"status": "review_failed", "attempts": 1}
        m["items"]["c"] = {"status": "review_failed", "attempts": 3}
        save_manifest(m)
        assert not list(MANIFEST.parent.glob("*.tmp")) and load_manifest() == m
        st = {
            k: {"status": "done", "note_path": "x", "added_at": str(i)}
            for i, k in enumerate("abcde")
        }
        st["z"] = {"status": "blocked", "note_path": "x"}
        assert [c for c, _ in select(st, m, 10)] == ["b", "d", "e"]
        assert [c for c, _ in select(st, m, 1)] == ["b"]
    u, r, t = extract("see https://github.com/a/b.git, and https://x.io/y).", ["Foo", 3])
    assert r == ["a/b"] and u[1] == "https://x.io/y" and t == ["Foo"]
    assert (
        context7("x", "")[1].startswith("unavailable")
        and PIPE_SH.search("curl -fsSL x | sh")
        and not PIPE_SH.search("pip install x")
    )
    # Context7 gate
    assert is_library({"exists": True, "files": ["package.json"], "topics": []}) and is_library(
        {"exists": True, "files": [], "topics": ["sdk"]}
    )
    assert (
        not is_library({"exists": True, "files": ["README.md"], "topics": ["saas"]})
        and not is_library(None)
        and not is_library({"exists": False, "files": ["go.mod"]})
    )
    # relevance filter: login wall / unrelated hits dropped, registries preferred
    tk = tokens("Security Sweep")
    SS = "Security Sweep"
    assert not relevant(
        "Delta Air Lines - Log In", "https://www.delta.com/login", "security sweep", SS
    )
    assert not relevant("Security tips", "https://x.com/a", "airport security", SS)
    assert relevant("Security Sweep docs", "https://github.com/a/security-sweep", "", SS) and pref(
        "https://github.com/a/b", tk
    ) > pref("https://blog.io", tk)
    assert not usable("Sign in to continue", tk) and usable("security sweep tool " * 5, tk)
    # first_step / validation / evidence cap / already-have
    good = {
        "verdict": "try-now",
        "businesses": [],
        "effort": "1h",
        "cost": "free",
        "risk": "r",
        "first_step": "Run pip install foo-cli and try foo --help",
        "evidence": ["https://github.com/a/foo/", "https://evil.example/x"],
        "tools_used": [],
        "confidence": "medium",
    }
    assert validate(dict(good), ["foo-cli"]) is None
    assert validate(dict(good, verdict="skip", first_step="Nothing to do; no tool here"), ["foo-cli"]) is None
    assert validate(dict(good, verdict="skip", first_step="Watch the video again"), ["foo-cli"]) and validate(dict(good, verdict="try-now", first_step="Nothing to do"), ["foo-cli"])
    assert validate(dict(good, first_step="Comment on the Instagram reel"), ["foo-cli"])
    assert validate(dict(good, first_step="Watch the video again"), []) and validate(
        dict(good, first_step="Install something else"), ["foo-cli"]
    )
    assert validate({k: v for k, v in good.items() if k != "confidence"}, []) and validate(
        dict(good, verdict="maybe"), []
    )
    f = finalize(
        dict(good),
        {"https://github.com/a/foo"},
        {"https://github.com/a/foo"},
        ["gh"],
        ["foo-cli"],
        [],
    )
    assert (
        f["evidence"] == ["https://github.com/a/foo"]
        and f["verdict"] == "try-now"
        and f["tools_used"] == ["gh", "llm-waterfall"]
    )
    f = finalize(dict(good), {"https://instagram.com/reel/1"}, set(), [], ["foo-cli"], [])
    assert (
        f["verdict"] == "later"
        and f["cap_reason"] == "note-only claims, unverified"
        and f["evidence"] == []
    )
    assert (
        finalize(
            dict(good),
            {"https://github.com/a/foo"},
            {"https://github.com/a/foo"},
            [],
            ["foo-cli"],
            ["foo-cli"],
        )["verdict"]
        == "already-have"
    )
    # replace_review touches only the review section
    doc = "# T\n\nbody\r\n\n## Review (auto, 2026-01-01)\n\n- old\n\n## After\nkeep\n"
    out = replace_review(doc, "\n\n## Review (auto, 2026-09-24)\n\n- new\n")
    assert out == "# T\n\nbody\r\n\n## Review (auto, 2026-09-24)\n\n- new\n\n## After\nkeep\n", (
        repr(out)
    )
    assert (
        replace_review("# T\nx\n\n## Review (auto, d)\n- old\n", "\n\n## Review (auto, e)\n- new\n")
        == "# T\nx\n\n## Review (auto, e)\n- new\n"
    )
    with tempfile.TemporaryDirectory() as t:  # digest upsert: same id twice -> one row

        class S:
            vault_dir = Path(t)

        upsert_digest_row(S, "T", "id1", {"verdict": "later"}, "a/n.md")
        upsert_digest_row(S, "T", "id2", {"verdict": "skip"}, "a/m.md")
        upsert_digest_row(S, "T", "id1", {"verdict": "skip"}, "a/n.md")
        lines = [
            line
            for line in (Path(t) / DIGEST_NAME).read_text("utf-8").splitlines()
            if "`id" in line
        ]
        assert len(lines) == 2 and sum("`id1`" in line for line in lines) == 1
        assert "`skip`" in lines[0]
    assert extract("", '["A", "B"]')[2] == ["A", "B"]
    # 1 trailing dot in repo name
    assert (
        extract("https://github.com/anytype/anytype-ts.", [])[1] == ["anytype/anytype-ts"]
        and gh_name("x.git") == "x"
    )
    # 2 norm keeps path case, lowercases scheme/host only
    assert (
        norm("HTTPS://Bit.LY/AbC1/#f") == "https://bit.ly/AbC1"
        and norm("https://youtu.be/dQw4") == "https://youtu.be/dQw4"
    )
    # 3 description links: shortener/affiliate without tool mention is dropped
    # (host-level predicates)
    assert (
        any("bit.ly".endswith(d) for d in SHORTENERS)
        and AFFILIATE.search("https://x.com/p?ref=abc")
        and not AFFILIATE.search("https://x.com/docs")
    )
    # 4 strict relevance
    assert not relevant(
        "Security Sweep", "https://digitalsweepsheet.delta.com/x", "security sweep", SS
    )  # unrelated domain
    assert not relevant(
        "Sweep", "https://github.com/sperax/sweep", "sweep keyword", SS
    ) and not relevant("Sweep tool", "https://x.io", "security tool", SS)
    assert not relevant(
        "Claude Code security sweep", "https://claude.com/blog", "security sweep", "Security Sweep"
    )
    assert relevant("Claude Code", "https://claude.com/product/claude-code", "", "Claude Code")
    assert (
        not repo_ok({"exists": True, "full_name": "sperax/sweep", "stars": 12}, ["Security Sweep"])
        and repo_ok(
            {"exists": True, "full_name": "a/security-sweep", "stars": 1}, ["Security Sweep"]
        )
        and repo_ok({"exists": True, "full_name": "a/b", "stars": 500}, [])
    )

    # 5 Context7 gate
    class R:
        def __init__(s, c, j):
            s.status_code, s._j = c, j

        def json(s):
            return s._j

    def reg(j):
        def get(m, u, **k):
            return R(200, j) if "npmjs" in u else R(404, {})

        return get

    assert not on_registry(
        "notion", get=reg({"repository": {"url": "git+https://github.com/other/thing.git"}})
    )
    assert on_registry(
        "foo", get=reg({"repository": {"url": "git+https://github.com/a/foo.git"}})
    ) and on_registry("foo", "a/foo", get=reg({"repository": {"url": "https://github.com/a/foo"}}))
    assert (
        not on_registry(
            "foo", "b/foo", get=reg({"repository": {"url": "https://github.com/a/foo"}})
        )
        and {"setup.py", "requirements.txt"} <= LIB_FILES
    )

    # 6 condense: ollama only on success
    class St:
        llm = type("L", (), {"ollama_host": "http://x"})

    global http
    real = http

    def http_none(*a, **k):
        return None

    def http_ok(*a, **k):
        return R(200, {"response": "ok"})

    http = http_none
    try:
        USED.clear()
        condense(St, "x" * 3000, "w")
        assert "ollama" not in USED
        http = http_ok
        condense(St, "x" * 3000, "w")
        assert "ollama" in USED
    finally:
        http = real
        USED.clear()
    # 7 needs-human retried up to 3 attempts total, then parked
    mm = {
        "items": {
            "n1": {"status": "needs-human", "attempts": 1},
            "n2": {"status": "needs-human", "attempts": 2},
            "n3": {"status": "needs-human", "attempts": 3},
        }
    }
    assert [
        c for c, _ in select({k: {"status": "done", "note_path": "x"} for k in mm["items"]}, mm, 9)
    ] == ["n1", "n2"]
    # 8 own-repo -> already-have; catalog cap
    own = finalize(
        dict(good, evidence=["https://github.com/MediaJohnD/foo"]),
        {"https://github.com/MediaJohnD/foo"},
        {"https://github.com/MediaJohnD/foo"},
        [],
        ["foo-cli"],
        [],
    )
    assert own["verdict"] == "already-have"
    cat = finalize(
        dict(good, evidence=["https://github.com/a/awesome-foo"]),
        {"https://github.com/a/awesome-foo"},
        {"https://github.com/a/awesome-foo"},
        [],
        ["foo-cli"],
        [],
        {"a/awesome-foo": {"full_name": "a/awesome-foo", "topics": []}},
    )
    assert (
        cat["verdict"] == "later"
        and is_catalog({"full_name": "a/x", "topics": ["awesome-list"]})
        and not is_catalog({"full_name": "a/playlistr", "topics": []})
    )

    # 9 install validation
    def g404(m, u, **k):
        return R(404, {}) if "nope" in u else R(200, {})

    assert (
        bad_install("Run npx nope-pkg --help", g404)
        and bad_install("pip install nope", g404)
        and bad_install("npm install -g @s/nope@1.2", g404)
    )
    assert (
        not bad_install("Run npx real-pkg", g404)
        and not bad_install("gh repo clone a/nope", g404)
        and not bad_install("pip install nope", lambda *a, **k: None)
    )
    # 10 tolerant JSON repair
    assert (
        parse_json('{"a": 1, "b": "tru') == {"a": 1, "b": "tru"}
        and parse_json('x {"a": [1, 2') == {"a": [1, 2]}
        and parse_json("no json") is None
        and parse_json('{"a": 1}') == {"a": 1}
    )
    print("self-check ok")


def main(argv):
    if "--self-check" in argv:
        return self_check()

    def arg(f, d=None):
        return argv[argv.index(f) + 1] if f in argv else d

    apply, limit = "--apply" in argv, int(arg("--limit", 10))
    sys.path.insert(0, str(ROOT / "src"))
    from reel_pipeline.config import load_settings

    settings = load_settings()
    if "--gen-have" in argv:
        return gen_have()
    key = os.environ.get("CONTEXT7_API_KEY", "")
    if not key and (ROOT / ".env").exists():  # read the one key by name; never printed
        for ln in (ROOT / ".env").read_text("utf-8").splitlines():
            if ln.startswith("CONTEXT7_API_KEY="):
                key = ln.split("=", 1)[1].strip().strip("\"'")
    if arg("--research-only"):
        return research_only(settings, arg("--research-only"), key)
    state = json.loads((settings.inbox_dir / "state.json").read_text("utf-8"))["items"]
    if arg("--rereview"):
        return rereview(settings, arg("--rereview"), state, key)
    if arg("--seed"):
        return seed(settings, arg("--seed"), state)
    if arg("--note"):
        want = Path(arg("--note")).name.lower()
        state = {
            c: r
            for c, r in state.items()
            if r.get("note_path") and Path(r["note_path"]).name.lower() == want
        }
    manifest = load_manifest()
    picks = select(state, manifest, limit)
    print(f"{'APPLY' if apply else 'DRY-RUN'}: {len(picks)} note(s) to review (limit {limit})")
    for cid, rec in picks:
        prev = manifest["items"].get(cid, {})
        try:
            res = review_one(settings, cid, rec, apply, key)
        except Exception as e:  # noqa: BLE001 - one bad item must not stop the batch
            res = {
                "status": "review_failed",
                "note_path": rec["note_path"],
                "error": f"{type(e).__name__}: {str(e)[:200]}",
            }
        if (
            res is None or not apply or res.get("error") == "note not found"
        ):  # dry-run and missing notes leave no trace
            if res and res.get("error"):
                print(f"  {cid}: {res['status']} {res['error']}")
            continue
        res.update(reviewed_at=now(), attempts=prev.get("attempts", 0) + 1)
        manifest["items"][cid] = res
        save_manifest(manifest)
        print(f"  {cid}: {res['status']} {res.get('verdict', '')} {res.get('error') or ''}")


if __name__ == "__main__":
    main(sys.argv[1:])
