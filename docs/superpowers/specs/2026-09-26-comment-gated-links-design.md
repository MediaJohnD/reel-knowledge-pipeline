<!-- /autoplan restore point: "C:\\Users\\media\\.gstack\\projects\\MediaJohnD-reel-knowledge-pipeline\\master-autoplan-restore-20260926-124903.md" -->
# Comment-gated links: follow, comment, read the DM

Date: 2026-09-26. Owner sign-off: given in chat on 2026-09-26 ("go with A; include DM reading").

## Implementation plan

### Problem

Many reels hide the real resource behind a ManyChat-style call to action: "comment SCRAPE
and I'll send you the link". Of 334 reviewed reels, 98 contain one, and 42 of those were
marked `skip` by `scripts/review_new_reels.py` (a third of all skips). The prompt
(`config/prompts/review_reel.md`) calls these lead magnets and says skip, and the research
step cannot find the gated resource, so it gives up. Example: `18e249779fa5a650` ("comment
scrape") was skipped as "no free tool", but ScrapeGraphAI is a real open-source repo.

Owner direction: never skip these. The pipeline should follow the creator, post the keyword
comment, read the DM the creator's bot sends back, and ingest what it contains. Carefully:
follow before commenting, and only a handful of actions per 15 minutes.

### Guardrail change (CLAUDE.md)

`CLAUDE.md` currently says no interactive browser automation and no account actions. Add a
second scoped exception, dated 2026-09-26 with owner sign-off, worded like the 2026-08-10
one:

- Confined to a new module `src/reel_pipeline/comment_gate.py`.
- Allowed actions, Instagram only, the owner's own account: follow the creator of a queued
  reel, post exactly the keyword the creator asked for on that reel, read the DM thread with
  that creator, and press a DM quick-reply button only when its label is on a short
  allowlist (see DM reading). Nothing else: no likes, no other comments, no DMs typed by us (one exception since 2026-10-02: the reel's keyword, typed back once when the bot asks for it, `reply_keyword`),
  no unfollows, no browsing feeds.
- Login is done by the owner by hand, once, in a dedicated persistent browser profile. Code
  never sees or types a password and does not reuse the yt-dlp/gallery-dl cookie config.
- No anti-detection or fingerprint-spoofing tooling. A challenge, "action blocked" or login
  wall halts all actions for 24 hours; it is never worked around.
- Dry-run by default; real actions need `--apply`.
- The owner accepts that Instagram's terms prohibit automated actions.
- The owner accepts that this is the same account yt-dlp and gallery-dl use for
  ingestion, so a checkpoint here also stalls Instagram ingestion. A halt is shown on the
  queue page and in the worker log.
- The follow list grows permanently, by about one follow per gated creator. There are
  no unfollows.

### Components

1. **Detection (review stage, `scripts/review_new_reels.py` + `config/prompts/review_reel.md`)**
   - New pure function `detect_comment_gate(text) -> str | None` in `comment_gate.py`
     (shared by the review script and the backfill). Runs on the note body above the
     review section (transcript and summary). Matches "comment|type|drop|reply" + an
     optional quoted or ALL-CAPS keyword + "and I'll send|to get|to receive|for the link|DM"
     within one sentence. Returns the keyword exactly as written (quotes stripped, case
     kept). Returns `""` when a gate is clearly present but no keyword can be isolated
     ("comment below for the link"); `None` when no gate.
   - New verdict `comment-for-link`. It is kept out of the LLM's `VERDICTS`, so the judge
     can never emit it; a separate display set includes it. When a gate is detected, the
     review still runs the normal research, and `finalize()` overrides `skip` to
     `comment-for-link`. The override runs after the already-have rule, and only while the
     item's comment-queue status is not `link-received`. That way a re-review with the real
     links can land a real verdict. `try-now`/`later`/`already-have` stay as judged (the reel may be
     good on its own), and the reel is still queued for the comment.
   - Prompt: the lead-magnet rule gets an exception: "A comment-for-link call to action is not
     by itself a reason to skip; the gated resource is fetched separately."
   - `BAD_STEP` stays: the judge still must not suggest social actions as `first_step`.
     Commenting is done by code, never recommended by the LLM.
   - Enqueue only when research found no independent evidence for the resource (no
     `indep` hit). A gated reel whose tool the research already found is not queued.
     Enqueue is idempotent: it never resets an existing record's status. New records get
     status `pending`, or `needs-keyword` when the keyword is `""`. The first read-only
     visit reads the caption and fills in the keyword, because transcripts come out
     lowercase and unquoted.
   - Move the `sys.path.insert` for `src/` above the `--self-check` branch, so the
     self-check can import `detect_comment_gate`.

2. **Queue (`data/review/comment_queue.json`)**
   - One record per content_id: `reel_url`, `note_path`, `keyword`, `creator` (filled on
     first visit), `status`, `events` (timestamped list), `links`, `dm_text`, `attempts`.
   - Statuses: `needs-keyword` → `pending` → `following` → `followed` → `commenting` →
     `commented` → `link-received` | `dm-timeout` | `failed`, plus `verify-comment` and
     `needs-rereview`.
   - `following` and `commenting` are written before the click (write-ahead). An item
     found in `commenting` on restart is never commented on again. It goes to
     `verify-comment`: the DM thread is read, and if nothing is there the item is shown to
     the owner.
   - Terminal states never re-run.
   - Records store the `content_id`, not a note path. The note path is looked up in
     state.json at write time, because the nightly organizer renames notes.
   - At most one open (`commenting` or `commented`) item per creator. Two gated reels from
     the same creator would otherwise share one DM thread and the replies could not be
     told apart.
   - Atomic write (tmp + `os.replace`), same pattern as `save_manifest`.
   - A global `halted_until` timestamp and a `daily` counter block live in the same file.

3. **Actions (`src/reel_pipeline/comment_gate.py`, CLI `comment-queue`)**
   - Playwright `launch_persistent_context` on the profile dir from
     `REEL_IG_BROWSER_PROFILE` (outside the repo, never committed), headed by default.
   - `cli comment-queue login`: opens the profile at instagram.com and waits for the owner
     to log in and close the window. That's the only way a session gets created.
   - `cli comment-queue run [--apply] [--max N]`: processes pending items within budget,
     then polls DMs for `commented` items. Without `--apply` it only reports what it
     would do (navigates read-only, no clicks).
   - Per item: open the reel URL, read the author handle, check the Follow button state.
     Not following → click Follow, confirm the button became "Following", wait a random
     60 to 180 s (during the first week, 20 to 90 min with jitter; see Rate limits).
     Before the comment, snapshot the last message in the creator's DM thread, if one
     exists, as the "after" marker. Then focus the comment box, type the keyword with per-character delay,
     submit, and confirm our comment is visible. Never comment twice: the queue record is
     written `commenting` before the click (see Queue). Scanning the page for an existing
     comment from our handle is only a best-effort extra check.
   - Rate limits (in `config/settings.yaml`, `comment_gate:` block): max 3 write actions
     (follow or comment) per rolling 15 minutes, random 3 to 6 minute gap between write
     actions, max 15 write actions and 8 comments per day (the day resets at `day_start_hour`, 07:00 local; rolling 24 h until 2026-09-28). For the first 7 days
     after the first `--apply`, max 3 comments per rolling 24 h. A follow counts toward the
     limit. Dry-run consumes no budget.
   - Browser: the installed Chrome via `channel="chrome"`, not stock Playwright Chromium.
     This is not spoofing, and nothing hides the automation. The run sleeps inside the budget and exits once the budget is spent.
   - Before every write, positively confirm that the logged-in handle equals
     `REEL_IG_OWNER_HANDLE`. Anything else, such as a login modal or the wrong account, is a
     halt.
   - Halt detection after every navigation and action. It fires when:
     - the URL contains `/challenge/`, `/accounts/login` or `/accounts/suspended`; or
     - text inside a `role=dialog` or toast (never captions or comments) matches "Action
       Blocked", "Try Again Later", "We restrict certain activity", "We limit how often",
       "Couldn't post comment" or "confirm it's you". On a hit: set
     `halted_until = now + 24h`, mark the item back to its prior status, log, exit code 2.

4. **DM reading**
   - For each `commented` item, at most once per run and only after 2 minutes since the
     comment: open `instagram.com/direct/inbox/`, and the "Requests" folder too (bot DMs
     from accounts we just followed often land there). Find the thread whose participant
     handle equals `creator`. The inbox list shows display names, not handles, so open each
  candidate thread and read its header link. Read the messages after the snapshot marker
  taken before the comment; do not rely on timestamps. Opening a request may mark it
  read. That is the only side effect, and the exception names it.
   - Extract URLs from message text and link buttons. Decode Instagram's
     `l.instagram.com/?u=<encoded>` redirect wrappers to the real target. Keep all message
     text in `dm_text`.
   - Quick replies: many ManyChat flows send "Tap below to get the link" with a button.
     Press a button only when its label matches the allowlist (`send`, `link`, `get`,
     `yes`, `access`, `guide`, `here`, case-insensitive, whole label under 30 chars), at most
     2 presses per thread, each counted as a write action. Never type a DM. Accepting a
     message request is not needed to read it, and we never accept one.
   - Results when URLs are found:
     - The item becomes `link-received`.
     - Each URL is registered with `QueueManager(settings).add_url(url, source)`, which
       takes the lock. Appending to queue.txt directly would race with
       `sync_queue_file_into_state` and could lose URLs.
     - The returned content_ids are stored on the record, so the queue page shows each
       link's ingest status, including blocked links.
     - The reel note gets a `## Gated content (DM)` section, inserted above the
       `## Review (auto,` section, because `analyse()` drops everything below that heading.
     - The item is marked `needs-rereview`, and the review script picks it up. This is a
       pull model, because `src/` never imports `scripts/`. The re-review's evidence then
       includes the real resource. No URL but message text → same section with the text, status
     `link-received`. Nothing after 48 h → `dm-timeout`, listed in the digest for the owner.

5. **Backfill**
   - `review_new_reels.py --backfill-comment-gates`: runs `detect_comment_gate` over every
     reviewed note, in this order:
     1. Ship the prompt exception.
     2. Re-review the 42 skipped gated reels through the normal re-review path, so the
        note, digest and manifest stay consistent.
     3. Enqueue only the reels still without independent evidence.
   - This should take well under 98 account actions.

6. **Queue page**
   - The queue gets its own vault file, `Reel Comment Queue.md`, next to the digest. It is
     regenerated whole from the JSON, because the digest's `upsert_digest_row` would
     delete rows keyed by cid.
   - It shows each reel's keyword, status, links and the links' ingest status, plus a
     halt banner.
   - Digest writes become atomic (tmp + `os.replace`), since two commands can now write
     it.

### Error handling

- Every per-item failure is caught, logged with the item id and step, and counted in
  `attempts`; after 3 attempts the item goes `failed`. Never raises out of the run loop,
  except the halt (exit 2) which stops everything.
- Playwright not installed or profile not logged in → clear message naming the fix
  (`uv run playwright install chromium`, `cli comment-queue login`), no actions taken.
- The worker's `state.run_once.lock` is respected when writing notes, same as the review
  script.

### Testing

- Unit (pytest, no browser): `detect_comment_gate` on real phrases from the 98 notes
  (quoted, ALL-CAPS, "comment, kimi," comma style, "drop X in the comments", negative
  cases like "comment strategy" in prose); rate limiter windows with an injected clock;
  queue state transitions and never-comment-twice; halt detection strings and URLs;
  `l.instagram.com` decoding; quick-reply allowlist.
- Flow test with a fake page object (same interface as the thin Playwright wrapper) covering
  follow-then-comment ordering, already-following skip, halt mid-run, DM with link, DM with
  button, DM timeout.
- Live verification (owner confirms each step in chat): `run` dry-run against 1 real queued
  reel; then `--apply --max 1` on one reel; then DM check. No live `--apply` without
  that explicit yes.
- `make check` (lock, ruff, pyright, pytest, uv audit).

### Out of scope

- Typing free-text DMs, answering bot questions that need typed input (email capture):
  those items go `failed` with reason "bot wants typed input" and show in the digest.
- Other platforms (TikTok/YouTube "comment for link").
- Running comment-gate from the webhook process.
- Scheduling via Task Scheduler (the machine is off at night; run by hand or add later).

## Review record

- Rate-limit research (2026-09-26): Instagram publishes no follow, comment or DM limits.
  Third-party numbers differ from each other by about 3x (for example, one source gives
  100 to 150 follows a day and another 300 to 500). The sources agree on two things.
  Detection looks at patterns, so a burst gets flagged even when it is far below any daily
  total. An action block lasts 24 to 48 h, and retrying during one makes it longer.
  Decision: keep the limits in Actions. They are at least 10x under every published
  estimate. They also give no bursts, because of the random 3 to 6 min gap and the 60 to
  180 s wait between follow and comment. A halt still means no retries for 24 h. Outside
  review through Codex was not run: the probe hung twice.
- Adversarial plan review (fresh-context subagent, 2026-09-26): 5 critical findings and
  8 should-fix findings, all accepted into the body above:
  - C1: the DM section goes above the review section.
  - C2: the override is gated on queue status.
  - C3: write-ahead `commenting` status, with `verify-comment` on restart.
  - C4: `add_url` instead of appending to queue.txt.
  - C5: a separate queue page, and atomic digest writes.
  - S1: research-first backfill.
  - S2/S3: owner-handle check, halt phrases matched only in dialogs, first-week cap,
    `channel="chrome"`.
  - S4: threads matched by header link and snapshot marker, one open item per creator.
  - S5: keyword read from the caption.
  - S6: note path looked up in state.json, pull-model re-review.
  - S7: `comment-for-link` kept out of `VERDICTS`.
  - S8: backfill keeps note, digest and manifest consistent.
  Two taste calls are left to the owner: whether quick-reply presses are in v1, and the
  first-week wait between follow and comment.
- Owner decisions (2026-09-26): quick-reply presses stay in v1, limited to the allowlist;
  first-week follow-to-comment wait is 20 to 90 min with jitter; plan approved for
  implementation.
