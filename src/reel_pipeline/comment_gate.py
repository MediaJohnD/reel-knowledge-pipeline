"""Comment-gated links: follow the creator, post the keyword, read the DM.

Many reels hide the real resource behind a ManyChat-style call to action
("comment SCRAPE and I'll send you the link"). This module follows the creator,
posts exactly that keyword on the reel, reads the creator's DM reply, and hands
any links to the normal ingestion queue.

This is the only place in the pipeline allowed to act on an account - a scoped,
owner-approved (2026-09-26) exception to the no-browser-automation guardrail in
CLAUDE.md. See docs/superpowers/specs/2026-09-26-comment-gated-links-design.md.
Hard rules enforced here:
- Instagram only, the owner's own account, logged in by hand in a dedicated
  profile (`cli comment-queue login`). Code never sees a password or reuses the
  yt-dlp/gallery-dl cookies.
- Allowed actions: follow, one keyword comment per reel, read DMs, press an
  allowlisted quick-reply button. Never type a DM, like, unfollow or browse.
- Dry-run unless `apply=True`. Rate limits are far below any published estimate.
- Any challenge, login wall or "action blocked" halts everything for 24 h. It is
  never worked around, and there is no anti-detection tooling.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, unquote, urlparse

from reel_pipeline.config import CommentGateConfig, Settings
from reel_pipeline.logging_setup import get_logger

logger = get_logger(__name__)

QUEUE_PAGE_NAME = "Reel Comment Queue.md"
GATED_HEADING = "## Gated content (DM)"
REVIEW_MARKER = "\n\n## Review (auto,"

# Statuses. "following"/"commenting" are written *before* the click (write-ahead),
# so a crash can never lead to a second comment on the same reel.
NEEDS_KEYWORD = "needs-keyword"
PENDING = "pending"
FOLLOWING = "following"
FOLLOWED = "followed"
COMMENTING = "commenting"
COMMENTED = "commented"
VERIFY_COMMENT = "verify-comment"
LINK_RECEIVED = "link-received"
NEEDS_REREVIEW = "needs-rereview"
REREVIEWED = "rereviewed"  # done: the review now sees the real links
DM_TIMEOUT = "dm-timeout"
FAILED = "failed"

TO_ACT = (PENDING, FOLLOWING, FOLLOWED)
AWAITING_DM = (COMMENTED, VERIFY_COMMENT)
OPEN = (COMMENTING, COMMENTED, VERIFY_COMMENT)


# --------------------------------------------------------------------------- detection

_VERB = r"\b(?P<verb>comment|drop|type|reply)\b"
_TRIGGER = (
    r"(?:\band\s+i'?ll\b|\bi'?ll\s+(?:send|dm|give|share)\b|\bi\s+will\s+(?:send|dm)\b"
    r"|\bwe'?ll\s+send\b|\bto\s+(?:get|receive|grab|access|unlock)\b"
    r"|\bfor\s+(?:a|the|my)?\s*(?:free\s+)?(?:link|guide|repo|access|list|template|prompts?)\b"
    r"|\bsend\s+(?:it|you)\b|\bvia\s+(?:direct\s+message|dm)\b|\bin\s+(?:your|my)\s+dms?\b)"
)
_GATE = re.compile(_VERB + r"(?P<mid>[^.!?\n]{0,60}?)" + _TRIGGER, re.IGNORECASE)
_QUOTED = re.compile(r"[\"'“”‘’]([^\"'“”‘’\n]{1,30})[\"'“”‘’]")
_STOP = {
    "with", "the", "word", "keyword", "below", "down", "in", "comments", "comment",
    "now", "and", "or", "if", "a", "an", "my", "your", "on", "this", "post", "reel",
}  # fmt: skip


def _norm(text: str) -> str:
    # I’ll -> I'll, so the trigger regex sees one apostrophe; quote marks around
    # a keyword are left alone.
    return re.sub(r"(\w)[’‘](\w)", r"\1'\2", text)


def detect_comment_gate(text: str) -> str | None:
    """Return the keyword a reel asks viewers to comment, "" when a gate is
    present but no keyword can be isolated, or None when there is no gate.

    The keyword comes back as written (quotes stripped, case kept).
    """
    found_blank = False
    for m in _GATE.finditer(_norm(text)):
        mid, verb = m.group("mid"), m.group("verb").lower()
        quoted = _QUOTED.search(mid)
        if quoted:
            return quoted.group(1).strip()
        tokens = re.findall(r"[\w#@]+", mid)
        while tokens and tokens[0].lower() in _STOP:
            tokens.pop(0)
        while tokens and tokens[-1].lower() in _STOP:
            tokens.pop()
        if verb in ("type", "reply"):
            # "type a prompt to get..." is an instruction, not a gate: only accept
            # an explicit keyword (quoted, handled above, or ALL CAPS).
            if tokens and len(tokens) <= 3 and all(t.isupper() and len(t) > 1 for t in tokens):
                return " ".join(tokens)
            continue
        if not tokens:
            found_blank = True
        elif len(tokens) <= 3:
            return " ".join(tokens)
    return "" if found_blank else None


# --------------------------------------------------------------------------- DM parsing

_URL = re.compile(r"https?://[^\s<>\"'\])]+")
_QUICK_REPLY = re.compile(r"\b(send|link|get|yes|access|guide|here)\b", re.IGNORECASE)
HALT_URL_PARTS = ("/challenge/", "/accounts/login", "/accounts/suspended")
HALT_PHRASES = (
    "action blocked",
    "try again later",
    "we restrict certain activity",
    "we limit how often",
    "couldn't post comment",
    "confirm it's you",
)


def unwrap_link(url: str) -> str:
    """Decode Instagram's l.instagram.com/?u=<target> redirect wrapper."""
    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname.endswith("l.instagram.com"):
        target = parse_qs(parsed.query).get("u")
        if target:
            return unquote(target[0])
    return url


def extract_links(texts: Iterable[str], hrefs: Iterable[str] = ()) -> list[str]:
    """URLs from message text and link buttons, unwrapped, in order, no duplicates."""
    found = [u.rstrip(".,;:!?") for t in texts for u in _URL.findall(t)]
    found += [h for h in hrefs if h.startswith("http")]
    out: list[str] = []
    for u in (unwrap_link(u) for u in found):
        if u not in out:
            out.append(u)
    return out


def quick_reply_allowed(label: str) -> bool:
    label = label.strip()
    return 0 < len(label) < 30 and bool(_QUICK_REPLY.search(label))


def halt_reason(url: str, dialog_texts: Iterable[str]) -> str | None:
    """Why the account must stop, or None. Only dialog/toast text is checked,
    never captions or comments, so a reel saying "try again later" is not a halt."""
    for part in HALT_URL_PARTS:
        if part in url:
            return f"url {part}"
    for text in dialog_texts:
        low = _norm(text).lower()
        for phrase in HALT_PHRASES:
            if phrase in low:
                return f"dialog: {phrase}"
    return None


# --------------------------------------------------------------------------- queue store


def queue_path(settings: Settings) -> Path:
    return settings.resolve(settings.comment_gate.queue_file)


def load_queue(path: Path) -> dict[str, Any]:
    q = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    q.setdefault("items", {})
    q.setdefault("writes", [])
    q.setdefault("halted_until", None)
    q.setdefault("first_apply_at", None)
    q.setdefault("next_write_at", None)
    return q


def save_queue(path: Path, q: dict[str, Any]) -> None:
    atomic_write(path, json.dumps(q, indent=2, ensure_ascii=False))


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _iso(t: datetime) -> str:
    return t.isoformat()


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def enqueue(q: dict[str, Any], cid: str, reel_url: str, keyword: str, now: datetime) -> bool:
    """Add a gated reel. Idempotent: never touches an existing record."""
    if cid in q["items"]:
        return False
    q["items"][cid] = {
        "reel_url": reel_url,
        "keyword": keyword,
        "creator": None,
        "status": PENDING if keyword else NEEDS_KEYWORD,
        "events": [],
        "links": [],
        "link_ids": [],
        "dm_text": "",
        "attempts": 0,
        "thread_url": None,
        "dm_marker": None,
        "presses": 0,
    }
    _event(q["items"][cid], now, "enqueued")
    return True


def queue_status(q: dict[str, Any], cid: str) -> str | None:
    rec = q["items"].get(cid)
    return rec["status"] if rec else None


def _event(rec: dict[str, Any], now: datetime, what: str) -> None:
    rec["events"].append(f"{_iso(now)} {what}")


def _set(rec: dict[str, Any], status: str, now: datetime, note: str = "") -> None:
    rec["status"] = status
    _event(rec, now, status + (f": {note}" if note else ""))


# --------------------------------------------------------------------------- rate limits


def first_week(q: dict[str, Any], now: datetime, cfg: CommentGateConfig) -> bool:
    start = q["first_apply_at"]
    return start is None or now < _dt(start) + timedelta(days=cfg.first_week_days)


def write_wait(q: dict[str, Any], now: datetime, cfg: CommentGateConfig, kind: str) -> float | None:
    """Seconds to wait before a write of `kind` ("follow"/"comment"/"button"),
    or None when the rolling 24 h budget is spent (the run should stop)."""
    writes = [(_dt(w["at"]), w["kind"]) for w in q["writes"]]
    day = [w for w in writes if w[0] > now - timedelta(hours=24)]
    if len(day) >= cfg.daily_max_writes:
        return None
    comment_cap = (
        cfg.first_week_daily_max_comments if first_week(q, now, cfg) else cfg.daily_max_comments
    )
    if kind == "comment" and sum(k == "comment" for _, k in day) >= comment_cap:
        return None
    wait = 0.0
    window = sorted(t for t, _ in writes if t > now - timedelta(minutes=cfg.window_minutes))
    if len(window) >= cfg.window_max_writes:
        oldest = window[-cfg.window_max_writes]
        wait = (oldest + timedelta(minutes=cfg.window_minutes) - now).total_seconds()
    if q["next_write_at"]:
        wait = max(wait, (_dt(q["next_write_at"]) - now).total_seconds())
    return max(wait, 0.0)


def record_write(
    q: dict[str, Any], now: datetime, kind: str, cfg: CommentGateConfig, rng: random.Random
) -> None:
    q["writes"].append({"at": _iso(now), "kind": kind})
    q["writes"] = [w for w in q["writes"] if _dt(w["at"]) > now - timedelta(days=2)]
    gap = rng.uniform(cfg.gap_min_seconds, cfg.gap_max_seconds)
    q["next_write_at"] = _iso(now + timedelta(seconds=gap))
    if q["first_apply_at"] is None:
        q["first_apply_at"] = _iso(now)


# --------------------------------------------------------------------------- browser seam


@dataclass
class Message:
    text: str
    hrefs: list[str] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    sender: str = ""  # username; "" when unknown (DOM fallback)
    ts: int = 0  # epoch ms; 0 when unknown
    id: str = ""
    taps: list[str] = field(default_factory=list)  # bot buttons only the app can press

    @property
    def key(self) -> str:
        return self.id or hashlib.sha1(self.text.encode("utf-8")).hexdigest()[:12]


def _walk(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def parse_slide_messages(bodies: Iterable[str], thread_key: str) -> list[Message]:
    """Messages of one DM thread from the page's own /api/graphql responses.

    instagram.com never draws bot cards (ManyChat "Access Here" links, quick
    replies): only their title shows. The card data, links included, is in the
    graphql payload the page already loaded (seen 2026-09-26). Oldest first.
    """
    seen: dict[str, Message] = {}
    for body in bodies:
        try:
            docs = [json.loads(body)]
        except ValueError:
            docs = []
            for line in body.splitlines():
                try:
                    docs.append(json.loads(line))
                except ValueError:
                    pass
        for d in (d for doc in docs for d in _walk(doc)):
            if str(d.get("thread_key")) != thread_key or not isinstance(
                d.get("slide_messages"), dict
            ):
                continue
            for edge in d["slide_messages"].get("edges") or []:
                n = (edge or {}).get("node") or {}
                c = n.get("content") or {}
                if c.get("__typename") == "SlideMessageAdminText":
                    continue  # "... messaged you about a comment" log line
                xma = c.get("xma") or {}
                ctas = xma.get("cta_buttons") or []
                text = c.get("text_body") or n.get("text_body") or xma.get("title_text") or ""
                hrefs = [b["action_url"] for b in ctas if b.get("action_url")]
                if xma.get("target_url"):
                    hrefs.append(xma["target_url"])
                sender = ((n.get("sender") or {}).get("user_dict") or {}).get("username") or ""
                mid = n.get("message_id") or n.get("id") or ""
                seen[mid] = Message(
                    text, hrefs, sender=sender, ts=int(n.get("timestamp_ms") or 0), id=mid,
                    taps=[b["title"] for b in ctas if b.get("title") and not b.get("action_url")],
                )  # fmt: skip
    return sorted(seen.values(), key=lambda m: m.ts)


class Page(Protocol):
    """What the run loop needs from a browser. `IgBrowser` is the Playwright
    version; tests use a fake. Every method is read-only except click_follow,
    post_comment and press_button."""

    def goto(self, url: str) -> None: ...
    def url(self) -> str: ...
    def dialog_texts(self) -> list[str]: ...
    def owner_handle(self) -> str | None: ...
    def reel_info(self) -> tuple[str | None, str]: ...
    def follow_state(self) -> str: ...
    def click_follow(self) -> None: ...
    def post_comment(self, text: str) -> None: ...
    def comment_visible(self, handle: str, text: str) -> bool: ...
    def open_thread(self, creator: str, known_url: str | None) -> str | None: ...
    def read_thread(self) -> list[Message]: ...
    def press_button(self, label: str) -> None: ...


class Halted(Exception):
    """Instagram pushed back. Stop every action for cfg.halt_hours."""


@dataclass
class RunReport:
    lines: list[str] = field(default_factory=list)
    halted: str | None = None

    def say(self, line: str) -> None:
        self.lines.append(line)
        logger.info("comment-gate: " + line)


def _new_replies(msgs: list[Message], rec: dict[str, Any], owner: str) -> list[Message]:
    """The creator's messages since our comment. Timestamps when the page gave
    them (a marker from an older reader version may never match), else the
    snapshot marker. The owner's own messages never count."""
    at = rec.get("commented_at")
    if at and msgs and all(m.ts for m in msgs):
        # Slack: the snapshot is taken before posting, commented_at after.
        cutoff = (_dt(at) - timedelta(minutes=2)).timestamp() * 1000
        new = [m for m in msgs if m.ts >= cutoff]
    else:
        new = _after_marker(msgs, rec["dm_marker"])
    return [m for m in new if m.sender.lower() != owner]


def _after_marker(msgs: list[Message], marker: str | None) -> list[Message]:
    if marker:
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].key == marker:
                return msgs[i + 1 :]
    return msgs


class Runner:
    def __init__(
        self,
        page: Page,
        q: dict[str, Any],
        cfg: CommentGateConfig,
        owner: str,
        apply: bool,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        save: Callable[[], None] = lambda: None,
    ):
        self.page, self.q, self.cfg, self.owner = page, q, cfg, owner.lstrip("@").lower()
        self.apply, self.now, self.sleep, self.save = apply, now, sleep, save
        self.rng = rng or random.Random()
        self.report = RunReport()

    # -- guards

    def _check(self) -> None:
        reason = halt_reason(self.page.url(), self.page.dialog_texts())
        if reason:
            raise Halted(reason)

    def _goto(self, url: str) -> None:
        self.page.goto(url)
        self._check()

    def _owner_ok(self) -> None:
        handle = (self.page.owner_handle() or "").lstrip("@").lower()
        if handle != self.owner:
            raise Halted(f"logged-in handle {handle or 'unknown'!r} is not the owner")

    def _wait_for(self, kind: str) -> bool:
        """Sleep until a write of `kind` fits the budget. False = budget spent."""
        wait = write_wait(self.q, self.now(), self.cfg, kind)
        if wait is None:
            return False
        if wait > 0:
            self.report.say(f"waiting {wait:.0f}s for the rate limit")
            self.sleep(wait)
        return True

    def _wrote(self, kind: str) -> None:
        record_write(self.q, self.now(), kind, self.cfg, self.rng)
        self.save()

    # -- main loop

    def run(self, max_items: int) -> RunReport:
        halted = self.q["halted_until"]
        if halted and self.now() < _dt(halted):
            self.report.halted = f"halted until {halted}"
            self.report.say(self.report.halted)
            return self.report
        try:
            self._recover()
            done = 0
            for cid, rec in list(self.q["items"].items()):
                if done >= max_items:
                    break
                if rec["status"] in TO_ACT and not self._creator_busy(cid, rec):
                    if not self._act(cid, rec):
                        break
                    done += 1
            for cid, rec in list(self.q["items"].items()):
                if rec["status"] in AWAITING_DM:
                    self._poll_dm(cid, rec)
        except Halted as exc:
            until = self.now() + timedelta(hours=self.cfg.halt_hours)
            self.q["halted_until"] = _iso(until)
            self.report.halted = str(exc)
            self.report.say(f"HALT ({exc}); no actions until {_iso(until)}")
        self.save()
        return self.report

    def _recover(self) -> None:
        # A crash mid-comment: we can't know whether it posted, so never retry it.
        for rec in self.q["items"].values():
            if rec["status"] == COMMENTING:
                _set(rec, VERIFY_COMMENT, self.now(), "found mid-comment on restart")

    def _creator_busy(self, cid: str, rec: dict[str, Any]) -> bool:
        # Two open reels from one creator share one DM thread; replies couldn't be
        # told apart. Hold the second back until the first is resolved.
        if not rec["creator"]:
            return False
        return any(
            other_cid != cid and other["creator"] == rec["creator"] and other["status"] in OPEN
            for other_cid, other in self.q["items"].items()
        )

    def _fail(self, cid: str, rec: dict[str, Any], prior: str, exc: Exception) -> None:
        if self.apply:  # a dry-run read error never burns an attempt
            rec["attempts"] += 1
        status = FAILED if rec["attempts"] >= self.cfg.max_attempts else prior
        _set(rec, status, self.now(), f"{type(exc).__name__}: {exc}")
        self.report.say(f"{cid}: error ({exc}); attempts={rec['attempts']}")
        self.save()

    def _act(self, cid: str, rec: dict[str, Any]) -> bool:
        """Follow then comment on one reel. False = budget spent, stop the run."""
        prior = rec["status"]
        try:
            self._goto(post_url(rec["reel_url"]))
            author, caption = self.page.reel_info()
            if not author:
                raise RuntimeError("could not read the reel's author")
            rec["creator"] = author.lstrip("@").lower()
            caption_kw = detect_comment_gate(caption)
            if caption_kw:
                rec["keyword"] = caption_kw
            if self._creator_busy(cid, rec):
                self.report.say(f"{cid}: held back, another reel from @{rec['creator']} is open")
                self.save()
                return True
            if not self.apply:
                return self._dry_run(cid, rec)

            self._owner_ok()
            if rec["status"] in (PENDING, FOLLOWING) and not self._follow(cid, rec):
                return False
            return self._comment(cid, rec)
        except Halted:
            if rec["status"] == COMMENTING:
                _set(rec, VERIFY_COMMENT, self.now(), "halt after comment click")
            raise
        except Exception as exc:  # per-item: log, count, move on
            # A failure after the COMMENTING write-ahead may have posted: never retry.
            posted = COMMENTING in (prior, rec["status"])
            self._fail(cid, rec, VERIFY_COMMENT if posted else prior, exc)
            return True

    def _follow_wait(self) -> tuple[float, float]:
        if first_week(self.q, self.now(), self.cfg):
            return self.cfg.first_week_follow_wait_seconds
        return self.cfg.follow_wait_seconds

    def _dry_run(self, cid: str, rec: dict[str, Any]) -> bool:
        owner = self.page.owner_handle()
        self._goto(f"https://www.instagram.com/{rec['creator']}/")
        state = self.page.follow_state()
        self.report.say(
            f"{cid}: [dry-run] logged in as @{owner}; creator @{rec['creator']} "
            f"({state}); would comment {rec['keyword']!r} on {rec['reel_url']}"
        )
        self.save()
        return True

    def _follow(self, cid: str, rec: dict[str, Any]) -> bool:
        self._goto(f"https://www.instagram.com/{rec['creator']}/")
        state = self.page.follow_state()
        if state in ("following", "requested"):
            _set(rec, FOLLOWED, self.now(), f"already {state}")
            self.save()
            return True
        if not self._wait_for("follow"):
            self.report.say("24 h budget spent, stopping")
            return False
        self._owner_ok()
        _set(rec, FOLLOWING, self.now())
        self.save()
        self.page.click_follow()
        self._wrote("follow")
        self._check()
        state = self.page.follow_state()
        if state not in ("following", "requested"):
            raise RuntimeError(f"follow did not stick (button says {state!r})")
        _set(rec, FOLLOWED, self.now())
        self.save()
        self.report.say(f"{cid}: followed @{rec['creator']}")
        wait = self.rng.uniform(*self._follow_wait())  # only after a real follow
        self.report.say(f"{cid}: waiting {wait:.0f}s between follow and comment")
        self.sleep(wait)
        return True

    def _comment(self, cid: str, rec: dict[str, Any]) -> bool:
        if not self._wait_for("comment"):
            self.report.say("24 h comment budget spent, stopping")
            return False
        # Snapshot the DM thread so only replies after our comment count.
        thread = self.page.open_thread(rec["creator"], rec["thread_url"])
        self._check()
        if thread:
            rec["thread_url"] = thread
            msgs = self.page.read_thread()
            rec["dm_marker"] = msgs[-1].key if msgs else None
        self._goto(post_url(rec["reel_url"]))
        self._owner_ok()
        if self.page.comment_visible(self.owner, rec["keyword"]):
            _set(rec, COMMENTED, self.now(), "our comment was already there")
            rec["commented_at"] = _iso(self.now())
            self.save()
            return True
        _set(rec, COMMENTING, self.now())
        self.save()
        self.page.post_comment(rec["keyword"])
        self._wrote("comment")
        self._check()
        rec["commented_at"] = _iso(self.now())
        if not self.page.comment_visible(self.owner, rec["keyword"]):
            _set(rec, VERIFY_COMMENT, self.now(), "comment not visible after posting")
        else:
            _set(rec, COMMENTED, self.now())
        self.save()
        self.report.say(f"{cid}: commented {rec['keyword']!r} on {rec['reel_url']}")
        return True

    def _poll_dm(self, cid: str, rec: dict[str, Any]) -> None:
        at = rec.get("commented_at")
        since = self.now() - _dt(at) if at else timedelta(hours=self.cfg.dm_timeout_hours)
        if since < timedelta(seconds=self.cfg.dm_min_wait_seconds) or not rec["creator"]:
            return
        try:
            thread = self.page.open_thread(rec["creator"], rec["thread_url"])
            self._check()
            new: list[Message] = []
            if thread:
                rec["thread_url"] = thread
                new = _new_replies(self.page.read_thread(), rec, self.owner)
                new = self._press_buttons(rec, new)
            links = extract_links([m.text for m in new], [h for m in new for h in m.hrefs])
            text = "\n".join(m.text for m in new if m.text.strip())
            taps = ", ".join(repr(t) for m in new for t in m.taps)
            tap = f"tap {taps}" if taps else "tap its button"
            if links:
                rec["links"], rec["dm_text"] = links, text
                _set(rec, LINK_RECEIVED, self.now(), f"{len(links)} link(s)")
                self.report.say(f"{cid}: DM from @{rec['creator']}: {', '.join(links)}")
            elif text and text != rec.get("dm_text"):
                # Quick replies are postbacks the web client can't send: keep
                # waiting and have the owner tap it in the app; the link comes next.
                rec["dm_text"] = text
                _event(rec, self.now(), "DM without a link yet")
                self.report.say(
                    f"{cid}: @{rec['creator']} replied without a link; {tap} "
                    "in the Instagram app, the next run picks up the link"
                )
            elif since > timedelta(hours=self.cfg.dm_timeout_hours):
                _set(rec, DM_TIMEOUT, self.now())
                self.report.say(f"{cid}: no DM after {self.cfg.dm_timeout_hours} h")
            elif taps:
                self.report.say(f"{cid}: still waiting: {tap} in @{rec['creator']}'s DM (app)")
            self.save()
        except Halted:
            raise
        except Exception as exc:
            _event(rec, self.now(), f"dm read error: {exc}")
            self.report.say(f"{cid}: DM read error ({exc})")
            self.save()

    def _press_buttons(self, rec: dict[str, Any], new: list[Message]) -> list[Message]:
        """Press an allowlisted quick reply when the bot sent no link yet."""
        while (
            self.apply
            and rec["presses"] < self.cfg.max_button_presses
            and not extract_links([m.text for m in new], [h for m in new for h in m.hrefs])
        ):
            labels = [b for m in new for b in m.buttons if quick_reply_allowed(b)]
            if not labels or not self._wait_for("button"):
                break
            self._owner_ok()
            rec["presses"] += 1
            self.page.press_button(labels[-1])
            self._wrote("button")
            self._check()
            _event(rec, self.now(), f"pressed {labels[-1]!r}")
            self.sleep(self.rng.uniform(20, 40))
            new = _new_replies(self.page.read_thread(), rec, self.owner)
        return new


# --------------------------------------------------------------------------- Playwright

_RESERVED = {"explore", "reels", "reel", "direct", "accounts", "p", "stories", "about", "legal"}
_HANDLE_HREF = re.compile(r"^/([A-Za-z0-9._]{1,30})/$")


_SHORTCODE = re.compile(r"/(?:p|reels?|tv)/([A-Za-z0-9_-]+)")
_OG = re.compile(r' - ([A-Za-z0-9._]{1,30}) on [^:]+: "(.*)"\.?\s*$', re.S)


def post_url(url: str) -> str:
    """The single-post page for a reel. /reel/ links redirect into the Reels feed,
    which shows other creators' reels (and comment boxes) on the same page."""
    m = _SHORTCODE.search(urlparse(url).path)
    if not m:
        raise ValueError(f"no post shortcode in {url}")
    return f"https://www.instagram.com/p/{m.group(1)}/"


def parse_og_description(og: str) -> tuple[str | None, str]:
    """'N likes, N comments - handle on July 15, 2026: "caption".' -> (handle, caption)"""
    m = _OG.search(og)
    return (m.group(1), m.group(2)) if m else (None, og)


class BrowserSetupError(RuntimeError):
    """Chrome, Playwright or the logged-in profile is missing."""


class IgBrowser:
    """The Playwright `Page` for Runner: installed Chrome (`channel="chrome"`),
    visible window, the owner's dedicated persistent profile. No stealth
    plugins, no user-agent changes, no cookie import.

    Selectors target instagram.com as of 2026-09 and are unverified until the
    first dry-run, whose report shows what each one read.
    """

    def __init__(self, profile_dir: str, settle_ms: int = 2500):
        self.profile_dir, self.settle_ms = profile_dir, settle_ms
        self._pw: Any = None
        self._ctx: Any = None
        self._page: Any = None
        self._gql: list[str] = []

    def __enter__(self) -> IgBrowser:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                self.profile_dir, channel="chrome", headless=False, viewport=None
            )
        except PlaywrightError as exc:
            self._pw.stop()
            raise BrowserSetupError(
                f"could not start Google Chrome with profile {self.profile_dir!r} "
                f"(is Chrome installed, and is that profile already open?): {exc}"
            ) from exc
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        # Keep the DM payloads the page loads anyway (read_thread); no requests
        # of our own.

        def keep(res: Any) -> None:
            if "/api/graphql" not in res.url:
                return
            try:
                body = res.text()
            except PlaywrightError:
                return
            if "slide_messages" in body:
                self._gql.append(body)

        self._page.on("response", keep)
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self._ctx.close()
        finally:
            self._pw.stop()

    # -- read

    def goto(self, url: str) -> None:
        self._gql.clear()
        self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        self._page.wait_for_timeout(self.settle_ms)

    def url(self) -> str:
        return self._page.url

    def dialog_texts(self) -> list[str]:
        loc = self._page.locator("[role=dialog], [role=alert], [role=alertdialog]")
        return [t for t in loc.all_inner_texts() if t.strip()]

    def owner_handle(self) -> str | None:
        # The sidebar's own-profile link (there is no link named "Profile" as of
        # 2026-09-26); feed authors' links all sit inside <main>.
        hrefs = self._page.locator("a[href]").evaluate_all(
            "els => els.filter(e => !e.closest('main')).map(e => e.getAttribute('href'))"
        )
        for href in hrefs:
            m = _HANDLE_HREF.match(href or "")
            if m and m.group(1).lower() not in _RESERVED:
                return m.group(1)
        return None

    def reel_info(self) -> tuple[str | None, str]:
        # og:description names the post's own author; page links can belong to
        # other creators (suggested posts), so they are never used to guess.
        meta = self._page.locator('meta[property="og:description"]')
        return parse_og_description(
            meta.first.get_attribute("content") or "" if meta.count() else ""
        )

    def _follow_button(self) -> tuple[Any, str] | None:
        # Matched on innerText: the accessible name and textContent both carry
        # the chevron icon's SVG title ("FollowingDown chevron icon", 2026-09-26).
        btns = self._page.locator("header button")
        texts = btns.evaluate_all("els => els.map(e => e.innerText.trim())")
        for i, text in enumerate(texts):
            if text in ("Follow", "Follow Back", "Following", "Requested"):
                return btns.nth(i), "follow" if text == "Follow Back" else text.lower()
        return None

    def follow_state(self) -> str:
        found = self._follow_button()
        return found[1] if found else "unknown"

    def comment_visible(self, handle: str, text: str) -> bool:
        return bool(
            self._page.evaluate(
                """([handle, text]) => {
                    const want = text.toLowerCase();
                    for (const a of document.querySelectorAll(`a[href="/${handle}/"]`)) {
                        let el = a;
                        // Live DOM (2026-09-26): the comment text sits 6 levels up.
                        for (let i = 0; i < 8 && el; i++, el = el.parentElement) {
                            if ((el.innerText || '').toLowerCase().includes(want)) return true;
                        }
                    }
                    return false;
                }""",
                [handle, text],
            )
        )

    def open_thread(self, creator: str, known_url: str | None) -> str | None:
        if known_url:
            self.goto(known_url)
            return known_url if self._thread_is(creator) else None
        # The profile's Message button opens a chat panel that links to the
        # existing thread (2026-09-26). Only this creator's thread is ever opened;
        # searching the inbox would open (and mark seen) unrelated threads. No
        # link yet = no thread yet: every later message is then new.
        self.goto(f"https://www.instagram.com/{creator}/")
        btn = self._page.locator("header [role=button]")
        labels = btn.evaluate_all("els => els.map(e => e.innerText.trim())")
        if labels.count("Message") != 1:
            return None
        btn.nth(labels.index("Message")).click()
        self._page.wait_for_timeout(self.settle_ms)
        hrefs = self._page.locator('a[href^="/direct/t/"]').evaluate_all(
            "els => els.map(e => e.getAttribute('href'))"
        )
        if len(set(hrefs)) != 1:
            return None
        url = "https://www.instagram.com" + hrefs[0]
        self.goto(url)
        return url if self._thread_is(creator) else None

    def _thread_is(self, creator: str) -> bool:
        return self._page.locator(f'a[href="/{creator}/"]').count() > 0

    def read_thread(self) -> list[Message]:
        m = re.search(r"/direct/t/(\d+)", self._page.url)
        for _ in range(10):  # the payload can land after domcontentloaded
            msgs = parse_slide_messages(self._gql, m.group(1)) if m else []
            if msgs:
                return msgs
            self._page.wait_for_timeout(1000)
        # Fallback: the rendered rows (no bot links, no sender). One evaluate, so
        # a re-render mid-read can't time out a per-row locator.
        rows = self._page.evaluate(
            """() => [...document.querySelectorAll('main [role=article]')].map(r => ({
                text: r.innerText.trim(),
                hrefs: [...r.querySelectorAll('a[href]')].map(a => a.href),
                buttons: [...r.querySelectorAll('[role=button]')]
                    .map(b => b.innerText.trim()).filter(Boolean),
            }))"""
        )
        out: list[Message] = []
        for r in rows:
            # Profile/post links inside a row are Instagram chrome, not the resource;
            # l.instagram.com is the outbound-link wrapper and is kept.
            hrefs = [h for h in r["hrefs"] if "instagram.com/" not in h or "l.instagram.com" in h]
            if r["text"] or hrefs:
                out.append(Message(r["text"], hrefs, r["buttons"]))
        return out

    # -- write (the only three)

    def click_follow(self) -> None:
        found = self._follow_button()
        if not found or found[1] != "follow":
            raise RuntimeError(f"no Follow button (state {found[1] if found else 'unknown'})")
        found[0].click()
        self._page.wait_for_timeout(self.settle_ms)

    def post_comment(self, text: str) -> None:
        box = self._page.locator('textarea[aria-label^="Add a comment"]').first
        box.click()
        box.fill(text)
        # Post appears only after typing, inside the textarea's own form
        # (2026-09-26); never click a page-wide "Post".
        post = box.locator("xpath=ancestor::form[1]").get_by_role("button", name="Post", exact=True)
        post.wait_for(timeout=10_000)
        if post.count() != 1:
            raise RuntimeError(
                f"expected one Post button in the comment form, found {post.count()}"
            )
        post.click()
        self._page.wait_for_timeout(self.settle_ms * 2)

    def press_button(self, label: str) -> None:
        self._page.get_by_role("button", name=label, exact=True).last.click()
        self._page.wait_for_timeout(self.settle_ms)


def login(
    profile_dir: str,
    wait: Callable[[], object],
    say: Callable[[str], object] = print,
    browser: Callable[[str], Any] = IgBrowser,
    tries: int = 5,
) -> str | None:
    """Open the login page in the dedicated profile; the owner logs in by hand.
    Returns the handle the profile is logged in as afterwards.

    Never navigates while the window is off instagram.com: "Continue with
    Facebook" runs its 2FA on facebook.com, and a goto there cuts it off."""
    from playwright.sync_api import Error as PlaywrightError

    with browser(profile_dir) as b:
        b.goto("https://www.instagram.com/accounts/login/")
        for _ in range(tries):
            wait()
            host = (urlparse(b.url()).hostname or "").lower()
            if host == "instagram.com" or host.endswith(".instagram.com"):
                try:
                    b.goto("https://www.instagram.com/")
                    if handle := b.owner_handle():
                        return handle
                except PlaywrightError:
                    pass  # a login redirect raced our navigation; ask again
            say(
                "Not logged in yet. Finish every step in the Chrome window "
                "(including any Facebook / 2FA check) until you see your Instagram feed, "
                "then press Enter again."
            )
        return None


# --------------------------------------------------------------------------- delivery


def insert_gated_section(note: str, links: list[str], dm_text: str) -> str:
    """Put the DM content above the review section (analyse() drops what's below)."""
    note = re.sub(r"\n\n## Gated content \(DM\)\n.*?(?=\n\n## |\Z)", "", note, flags=re.S)
    body = [GATED_HEADING, ""]
    body += [f"- {u}" for u in links] or ["- (no links)"]
    if dm_text:
        body += ["", "DM text:", ""] + [f"> {line}" for line in dm_text.splitlines()]
    section = "\n\n" + "\n".join(body)
    head, sep, tail = note.partition(REVIEW_MARKER)
    return head.rstrip("\n") + section + (sep + tail if sep else "\n")


def find_note(settings: Settings, cid: str, state_items: dict[str, Any]) -> Path | None:
    """state.json's note_path, or a vault search by content_id when the nightly
    organizer has renamed the file since."""
    rec = state_items.get(cid) or {}
    if rec.get("note_path") and Path(rec["note_path"]).is_file():
        return Path(rec["note_path"])
    needle = f"content_id: {cid}"
    reels = Path(settings.vault_dir)
    # Notes get sorted by hand into topic folders, so fall back to the vault root
    # (vault_dir is <vault>/20-Resources/Reels).
    for root in (reels, reels.parents[1]):
        for p in root.rglob("*.md"):
            try:
                if needle in p.read_text(encoding="utf-8")[:1000]:
                    return p
            except OSError:
                continue
    return None


def deliver(settings: Settings, q: dict[str, Any], now: datetime) -> list[str]:
    """Register DM links for ingestion and write them into the reel note.

    link-received -> needs-rereview. The review script picks those up (src/ never
    imports scripts/)."""
    from filelock import FileLock

    from reel_pipeline.models import QueueSource
    from reel_pipeline.queue_manager import QueueManager

    ready = [(cid, r) for cid, r in q["items"].items() if r["status"] == LINK_RECEIVED]
    if not ready:
        return []
    qm = QueueManager(settings)
    state = json.loads(settings.state_file.read_text(encoding="utf-8")).get("items", {})
    out: list[str] = []
    with FileLock(str(settings.inbox_dir / "state.run_once.lock"), timeout=600):
        for cid, rec in ready:
            rec["link_ids"] = [
                qm.add_url(u, QueueSource.COMMENT_GATE).content_id for u in rec["links"]
            ]
            note = find_note(settings, cid, state)
            if note is None:
                _event(rec, now, "note not found; links queued only")
            else:
                text = note.read_text(encoding="utf-8")
                atomic_write(note, insert_gated_section(text, rec["links"], rec["dm_text"]))
            _set(rec, NEEDS_REREVIEW, now)
            out.append(f"{cid}: {len(rec['links'])} link(s) queued")
    return out


def render_queue_page(q: dict[str, Any], state_items: dict[str, Any]) -> str:
    lines = ["# Reel Comment Queue", "", "Regenerated by `cli comment-queue run`; edits are lost."]
    lines.append("")
    if q["halted_until"]:
        lines += [f"> [!warning] Halted until {q['halted_until']}. No actions until then.", ""]
    lines += ["| Reel | Keyword | Creator | Status | Links |", "|---|---|---|---|---|"]
    for cid, r in q["items"].items():
        links = ", ".join(
            f"{u} ({(state_items.get(i or '') or {}).get('status', '?')})"
            for u, i in zip(r["links"], r["link_ids"] or [None] * len(r["links"]), strict=False)
        )
        lines.append(
            f"| [{cid}]({r['reel_url']}) | {r['keyword'] or '?'} | {r['creator'] or '?'} "
            f"| {r['status']} | {links} |"
        )
    return "\n".join(lines) + "\n"


def write_queue_page(settings: Settings, q: dict[str, Any]) -> Path:
    state = {}
    if settings.state_file.exists():
        state = json.loads(settings.state_file.read_text(encoding="utf-8")).get("items", {})
    path = Path(settings.vault_dir) / QUEUE_PAGE_NAME
    atomic_write(path, render_queue_page(q, state))
    return path
