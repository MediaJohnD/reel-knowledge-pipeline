from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta

import pytest

from reel_pipeline import comment_gate as cg
from reel_pipeline.comment_gate import Message, Runner, detect_comment_gate
from reel_pipeline.config import CommentGateConfig

# Real phrasings sampled from the vault's reel notes.
POSITIVE = [
    ("COMMENT 'SKILLS' FOR A LINK", "SKILLS"),
    ("COMMENT TRADER I'LL SEND YOU THE FULL SETUP", "TRADER"),
    ("Comment cloud to get it", "cloud"),
    ("Comment the word cloud to get 22 free prompts", "cloud"),
    ('comment "REPLACE" to receive the full list via direct message', "REPLACE"),
    ("Comment 'free' to get direct access to the repository", "free"),
    ("Comment “video” to receive", "video"),
    ("comment “YES” to receive the free toolkit", "YES"),
    ("comment graph it I'll send it to you", "graph it"),
    ("if you comment files, I'll send you the whole breakdown", "files"),
    ('comment with "Skills" to receive', "Skills"),
    ("comment “Claude” to receive more details", "Claude"),
    ('comment "Carpathi" to receive the repo link', "Carpathi"),
    ("comment scrape and i’ll send you the link", "scrape"),
    ("comment, kimi, and I'll send it over", "kimi"),
    ("drop AI in the comments and I'll send it", "AI"),
    ("type YES below to get the guide", "YES"),
    ("just comment below or I'll send it over", ""),
    ("comment for a free link", ""),
    ("Comment on this post to get the template", ""),
]

NEGATIVE = [
    "type a prompt, get a full app",
    "type a request, get a working agent",
    'type "slash cro," paste their homepage link',
    "reply (day 5) → send follow-up",
    "It sets up comment-to-DM automation for your page",
    "comment or DM for implementation",
    "Your comment strategy matters more than posting time.",
    "type a prompt to get a response",
]


@pytest.mark.parametrize(("text", "keyword"), POSITIVE)
def test_detects_gates(text: str, keyword: str) -> None:
    assert detect_comment_gate(text) == keyword


@pytest.mark.parametrize("text", NEGATIVE)
def test_ignores_non_gates(text: str) -> None:
    assert detect_comment_gate(text) is None


def test_keyword_preferred_over_blank_gate() -> None:
    assert detect_comment_gate("comment below for the link. Comment 'AGENT' to get it") == "AGENT"


def test_unwrap_and_extract_links() -> None:
    wrapped = "https://l.instagram.com/?u=https%3A%2F%2Fgithub.com%2Fa%2Fb&e=AT0"
    assert cg.unwrap_link(wrapped) == "https://github.com/a/b"
    links = cg.extract_links(
        ["here you go: https://github.com/a/b.", "again https://github.com/a/b"], [wrapped]
    )
    assert links == ["https://github.com/a/b"]


def test_quick_reply_allowlist() -> None:
    assert cg.quick_reply_allowed("Send me the link")
    assert cg.quick_reply_allowed("YES")
    assert not cg.quick_reply_allowed("Unsubscribe")
    assert not cg.quick_reply_allowed("Get " + "x" * 40)


def test_halt_reason() -> None:
    assert cg.halt_reason("https://www.instagram.com/challenge/123/", [])
    assert cg.halt_reason("https://www.instagram.com/accounts/login/?next=", [])
    assert cg.halt_reason("https://www.instagram.com/reel/x/", ["Action Blocked\nTry again"])
    assert cg.halt_reason("https://www.instagram.com/reel/x/", ["We limit how often you can"])
    assert cg.halt_reason("https://www.instagram.com/reel/x/", ["Couldn’t post comment"])
    assert cg.halt_reason("https://www.instagram.com/reel/x/", []) is None


T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _q() -> dict:
    return cg.load_queue(__import__("pathlib").Path("does-not-exist.json"))


def test_budget_window_and_gap() -> None:
    cfg, q, rng = CommentGateConfig(), _q(), random.Random(1)
    assert cg.write_wait(q, T0, cfg, "follow") == 0
    cg.record_write(q, T0, "follow", cfg, rng)
    wait = cg.write_wait(q, T0, cfg, "comment")
    assert wait is not None and 180 <= wait <= 360
    # three writes inside 15 min -> the fourth waits for the window
    q["writes"] = [
        {"at": (T0 + timedelta(minutes=m)).isoformat(), "kind": "follow"} for m in (0, 1, 2)
    ]
    q["next_write_at"] = None
    assert cg.write_wait(q, T0 + timedelta(minutes=3), cfg, "follow") == pytest.approx(12 * 60)


def test_budget_daily_caps_and_first_week() -> None:
    cfg, q = CommentGateConfig(), _q()
    q["first_apply_at"] = T0.isoformat()
    q["writes"] = [
        {"at": (T0 + timedelta(hours=h)).isoformat(), "kind": "comment"} for h in (0, 1, 2)
    ]
    now = T0 + timedelta(hours=3)
    assert cg.write_wait(q, now, cfg, "comment") is None  # 3/day in week 1
    assert cg.write_wait(q, now, cfg, "follow") == 0
    later = T0 + timedelta(days=8)
    q["writes"] = [{"at": (later - timedelta(hours=1)).isoformat(), "kind": "comment"}] * 3
    assert cg.write_wait(q, later, cfg, "comment") == 0  # week 1 over: cap is 8
    q["writes"] = [{"at": (later - timedelta(hours=1)).isoformat(), "kind": "follow"}] * 15
    assert cg.write_wait(q, later, cfg, "follow") is None


def test_enqueue_is_idempotent() -> None:
    q = _q()
    assert cg.enqueue(q, "c1", "https://instagram.com/reel/A", "", T0)
    assert q["items"]["c1"]["status"] == cg.NEEDS_KEYWORD
    q["items"]["c1"]["status"] = cg.COMMENTED
    assert not cg.enqueue(q, "c1", "https://instagram.com/reel/A", "X", T0)
    assert q["items"]["c1"]["status"] == cg.COMMENTED


def test_insert_gated_section_goes_above_review() -> None:
    note = (
        "---\ntitle: t\n---\n\n# T\n\n## Transcript\nhi\n\n"
        "## Review (auto, 2026-09-26)\n\n- verdict: skip\n"
    )
    out = cg.insert_gated_section(note, ["https://github.com/a/b"], "here it is")
    assert out.index("## Gated content (DM)") < out.index("## Review (auto,")
    assert "- https://github.com/a/b" in out and "> here it is" in out
    again = cg.insert_gated_section(out, ["https://x.dev"], "")
    assert again.count("## Gated content (DM)") == 1 and "github.com/a/b" not in again
    assert cg.insert_gated_section("# T\n\nbody\n", [], "txt").endswith("> txt\n")


# --------------------------------------------------------------------------- flow


class FakePage:
    def __init__(self) -> None:
        self.current = ""
        self.owner = "me"
        self.author = "creator"
        self.caption = "Comment 'SCRAPE' and I'll send you the link"
        self.following = False
        self.comments: list[str] = []
        self.thread: list[Message] = [Message("old chat")]
        self.bot_reply: list[Message] = [Message("here: https://github.com/s/g")]
        self.dialogs: list[str] = []
        self.actions: list[str] = []
        self.halt_on: str | None = None
        self.crash_on_comment = False

    def goto(self, url: str) -> None:
        self.current = url

    def url(self) -> str:
        return self.current

    def dialog_texts(self) -> list[str]:
        return self.dialogs

    def owner_handle(self) -> str | None:
        return self.owner

    def reel_info(self) -> tuple[str | None, str]:
        return self.author, self.caption

    def follow_state(self) -> str:
        return "following" if self.following else "follow"

    def click_follow(self) -> None:
        self.actions.append("follow")
        self.following = True
        if self.halt_on == "follow":
            self.dialogs = ["Action Blocked"]

    def post_comment(self, text: str) -> None:
        self.actions.append(f"comment:{text}")
        if self.crash_on_comment:
            raise KeyboardInterrupt  # simulates the process dying mid-click
        self.comments.append(text)
        self.thread = self.thread + self.bot_reply

    def comment_visible(self, handle: str, text: str) -> bool:
        return text in self.comments

    def open_thread(self, creator: str, known_url: str | None) -> str | None:
        return "https://www.instagram.com/direct/t/1/" if self.thread else None

    def read_thread(self) -> list[Message]:
        return list(self.thread)

    def press_button(self, label: str) -> None:
        self.actions.append(f"press:{label}")
        self.thread = self.thread + [Message("https://l.instagram.com/?u=https%3A%2F%2Fx.dev")]


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def now(self) -> datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += timedelta(seconds=s)


def _runner(page: FakePage, q: dict, apply: bool = True, clock: Clock | None = None) -> Runner:
    clock = clock or Clock()
    return Runner(page, q, CommentGateConfig(), "me", apply, now=clock.now, sleep=clock.sleep,
                  rng=random.Random(0))  # fmt: skip


def _queued() -> dict:
    q = _q()
    cg.enqueue(q, "c1", "https://www.instagram.com/reel/A/", "scrape", T0)
    return q


def test_follow_then_comment_then_dm_link() -> None:
    page, q, clock = FakePage(), _queued(), Clock()
    _runner(page, q, clock=clock).run(max_items=1)
    rec = q["items"]["c1"]
    assert page.actions == ["follow", "comment:SCRAPE"]  # caption keyword wins
    assert rec["status"] == cg.COMMENTED
    # follow -> comment wait is the first-week 20-90 min
    assert clock.t - T0 >= timedelta(minutes=20)
    clock.sleep(300)
    _runner(page, q, clock=clock).run(max_items=1)
    assert rec["status"] == cg.LINK_RECEIVED
    assert rec["links"] == ["https://github.com/s/g"] and "old chat" not in rec["dm_text"]


def test_already_following_skips_follow() -> None:
    page, q = FakePage(), _queued()
    page.following = True
    _runner(page, q).run(max_items=1)
    assert page.actions == ["comment:SCRAPE"]


def test_dry_run_takes_no_action_and_no_budget() -> None:
    page, q = FakePage(), _queued()
    report = _runner(page, q, apply=False).run(max_items=1)
    assert page.actions == [] and q["writes"] == [] and q["first_apply_at"] is None
    assert q["items"]["c1"]["status"] == cg.PENDING
    assert any("would comment 'SCRAPE'" in line for line in report.lines)


def test_halt_stops_everything_for_24h() -> None:
    page, q, clock = FakePage(), _queued(), Clock()
    page.halt_on = "follow"
    report = _runner(page, q, clock=clock).run(max_items=1)
    assert report.halted and q["halted_until"]
    assert page.actions == ["follow"]
    page.dialogs = []
    again = _runner(page, q, clock=clock).run(max_items=1)
    assert again.halted and page.actions == ["follow"]


def test_wrong_account_halts_before_any_write() -> None:
    page, q = FakePage(), _queued()
    page.owner = "someone_else"
    report = _runner(page, q).run(max_items=1)
    assert report.halted and page.actions == []


def test_crash_mid_comment_is_never_retried() -> None:
    page, q = FakePage(), _queued()
    page.following = True
    page.crash_on_comment = True
    with pytest.raises(KeyboardInterrupt):
        _runner(page, q).run(max_items=1)
    assert q["items"]["c1"]["status"] == cg.COMMENTING
    page.crash_on_comment = False
    _runner(page, q).run(max_items=1)
    assert q["items"]["c1"]["status"] == cg.VERIFY_COMMENT
    assert page.actions == ["comment:SCRAPE"]


def test_already_following_comments_without_the_follow_wait() -> None:
    page, q, clock = FakePage(), _queued(), Clock()
    page.following = True
    _runner(page, q, clock=clock).run(max_items=1)
    assert page.actions == ["comment:SCRAPE"]
    assert clock.t - T0 < timedelta(minutes=1)


def test_error_after_comment_click_is_never_retried() -> None:
    class PostTimesOut(FakePage):
        def post_comment(self, text: str) -> None:
            self.actions.append(f"comment:{text}")
            raise RuntimeError("Post button click timed out")

    page, q = PostTimesOut(), _queued()
    page.following = True
    _runner(page, q).run(max_items=1)
    assert q["items"]["c1"]["status"] == cg.VERIFY_COMMENT
    _runner(page, q).run(max_items=1)
    assert page.actions == ["comment:SCRAPE"]


def test_dm_button_press_then_link() -> None:
    page, q, clock = FakePage(), _queued(), Clock()
    page.following = True
    page.bot_reply = [Message("Tap below to get it", buttons=["Send me the link", "Unsubscribe"])]
    _runner(page, q, clock=clock).run(max_items=1)
    clock.sleep(600)
    _runner(page, q, clock=clock).run(max_items=1)
    rec = q["items"]["c1"]
    assert "press:Send me the link" in page.actions
    assert rec["links"] == ["https://x.dev"] and rec["status"] == cg.LINK_RECEIVED


def test_dm_without_link_keeps_waiting_for_the_link() -> None:
    # Live 2026-09-26: the bot's button doesn't render on the web, only its text.
    page, q, clock = FakePage(), _queued(), Clock()
    page.following = True
    page.bot_reply = [Message("Click below and I'll send you the setup")]
    _runner(page, q, clock=clock).run(max_items=1)
    clock.sleep(600)
    report = _runner(page, q, clock=clock).run(max_items=0)
    rec = q["items"]["c1"]
    assert rec["status"] == cg.COMMENTED and any("without a link" in x for x in report.lines)
    page.thread = page.thread + [Message("https://github.com/s/g")]  # owner tapped it
    _runner(page, q, clock=clock).run(max_items=0)
    assert rec["status"] == cg.LINK_RECEIVED and rec["links"] == ["https://github.com/s/g"]


def _node(mid: str, ts: int, user: str, content: dict) -> dict:
    return {"node": {"message_id": mid, "timestamp_ms": str(ts), "content": content,
                     "sender": {"user_dict": {"username": user}}}}  # fmt: skip


def test_parse_slide_messages_reads_bot_cards() -> None:
    # Shape captured live 2026-09-26 (newest first); the web draws none of the buttons.
    card = {"__typename": "SlideMessageXMAContent", "xma": {
        "title_text": "Here's your SWAP guide!", "target_url": None,
        "cta_buttons": [{"title": "Access Here", "cta_type": "xma_web_url",
                         "action_url": "https://my.manychat.com/r?act=1"}]}}  # fmt: skip
    quick = {"__typename": "SlideMessageXMAContent", "xma": {
        "title_text": "Want it?", "cta_buttons": [
            {"title": "Send the FULL setup!", "cta_type": "postback",
             "action_url": None}]}}  # fmt: skip
    thread = {"thread_key": "42", "slide_messages": {"edges": [
        _node("m3", 3000, "me", {"__typename": "SlideMessageText", "text_body": "links?"}),
        _node("m2", 2000, "creator", card),
        _node("m1", 1000, "creator", quick),
        _node("m0", 900, "", {"__typename": "SlideMessageAdminText"}),
    ]}}  # fmt: skip
    other = {"thread_key": "7", "slide_messages": {"edges": [_node("x", 1, "z", card)]}}
    body = json.dumps({"data": {"a": thread, "b": other}})
    msgs = cg.parse_slide_messages([body, "not json"], "42")
    assert [m.id for m in msgs] == ["m1", "m2", "m3"]
    assert msgs[0].taps == ["Send the FULL setup!"] and msgs[0].hrefs == []
    assert msgs[1].hrefs == ["https://my.manychat.com/r?act=1"] and msgs[1].sender == "creator"


def test_dm_ignores_owner_messages_and_reports_taps() -> None:
    page, q, clock = FakePage(), _queued(), Clock()
    page.following = True
    page.thread = []
    page.bot_reply = []
    _runner(page, q, clock=clock).run(max_items=1)
    clock.sleep(600)
    ms = int(clock.t.timestamp() * 1000)
    page.thread = [
        Message("old https://old.dev", sender="creator", ts=ms - 10**9, id="o"),
        Message("Want it?", sender="creator", ts=ms, id="a", taps=["Send the FULL setup!"]),
        Message("https://mine.dev", sender="me", ts=ms + 1, id="b"),
    ]
    report = _runner(page, q, clock=clock).run(max_items=0)
    rec = q["items"]["c1"]
    assert rec["status"] == cg.COMMENTED
    assert any("'Send the FULL setup!'" in x for x in report.lines)
    page.thread.append(
        Message("t", ["https://my.manychat.com/r?x"], sender="creator", ts=ms + 2, id="c")
    )
    _runner(page, q, clock=clock).run(max_items=0)
    assert rec["links"] == ["https://my.manychat.com/r?x"]


def test_max_zero_only_checks_dms() -> None:
    page, q = FakePage(), _queued()
    _runner(page, q).run(max_items=0)
    assert page.actions == [] and q["items"]["c1"]["status"] == cg.PENDING


def test_dm_timeout_after_48h() -> None:
    page, q, clock = FakePage(), _queued(), Clock()
    page.following = True
    page.bot_reply = []
    _runner(page, q, clock=clock).run(max_items=1)
    clock.sleep(3600)
    _runner(page, q, clock=clock).run(max_items=1)
    assert q["items"]["c1"]["status"] == cg.COMMENTED
    clock.sleep(49 * 3600)
    _runner(page, q, clock=clock).run(max_items=1)
    assert q["items"]["c1"]["status"] == cg.DM_TIMEOUT


def test_same_creator_is_held_back() -> None:
    page, q = FakePage(), _queued()
    page.following = True
    page.bot_reply = []
    cg.enqueue(q, "c2", "https://www.instagram.com/reel/B/", "scrape", T0)
    _runner(page, q).run(max_items=5)
    assert q["items"]["c1"]["status"] == cg.COMMENTED
    assert q["items"]["c2"]["status"] == cg.PENDING
    assert page.actions == ["comment:SCRAPE"]


class _LoginBrowser:
    """Chrome sits on Facebook's 2FA page for the first Enter."""

    def __init__(self, urls: list[str]):
        self.urls, self.gotos = urls, []

    def __call__(self, profile_dir: str) -> _LoginBrowser:
        return self

    def __enter__(self) -> _LoginBrowser:
        return self

    def __exit__(self, *a: object) -> None:
        pass

    def goto(self, url: str) -> None:
        self.gotos.append(url)

    def url(self) -> str:
        return self.urls.pop(0)

    def owner_handle(self) -> str:
        return "mediajohnd"


def test_login_never_navigates_away_from_facebook_2fa() -> None:
    b = _LoginBrowser(
        ["https://www.facebook.com/two_step_verification/two_factor/", "https://www.instagram.com/"]
    )
    said: list[str] = []
    assert cg.login("p", lambda: None, said.append, b) == "mediajohnd"
    assert len(said) == 1  # asked to finish 2FA once
    assert b.gotos == ["https://www.instagram.com/accounts/login/", "https://www.instagram.com/"]


def test_post_url_avoids_the_reels_feed() -> None:
    assert (
        cg.post_url("https://instagram.com/reel/Da0HEBruHzG")
        == "https://www.instagram.com/p/Da0HEBruHzG/"
    )
    assert (
        cg.post_url("https://www.instagram.com/reels/Da0HEBruHzG/?x=1")
        == "https://www.instagram.com/p/Da0HEBruHzG/"
    )
    assert (
        cg.post_url("https://instagram.com/p/Dbh2V4pj-m0?img_index=1")
        == "https://www.instagram.com/p/Dbh2V4pj-m0/"
    )


def test_parse_og_description_reads_the_posts_own_author() -> None:
    og = "3,653 likes, 5,250 comments - zachdoesai_ on July 15, 2026: "
    og += '"Comment “TRADE” for the link\n\n#ai".'
    assert cg.parse_og_description(og) == ("zachdoesai_", "Comment “TRADE” for the link\n\n#ai")
    assert cg.parse_og_description("Instagram") == (None, "Instagram")
