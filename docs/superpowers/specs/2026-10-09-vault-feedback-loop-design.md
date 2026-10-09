# Vault feedback loop: reels -> sessions -> stack updates (2026-10-09)

## Problem (owner, 2026-10-09)
"We are not leveraging the vault enough... not updating our Claude, stack, Docker, LLMs,
Waterfall enough... new and different topics that interest [me] have not emerged in Claude
or chats or sessions."

Evidence:
- `session-context.py` (UserPromptSubmit) is keyword-only. It never surfaces a reel the
  prompt didn't already name.
- `review_new_reels.py` is business-centric. 169 of ~491 reels are `skip`, which includes
  every personal topic (travel, habits, dating, emergency prep).
- 52 `try-now` verdicts have no follow-through: nothing tracks whether they were adopted.
- Nothing checks stack freshness. CLIs, Docker images, `model-rankings.md` and the
  free-llm waterfall only change when someone happens to ask.

## Design (two pieces, no new services)

### 1. `scripts/loop_digest.py` -> vault `10-Command Centers/Loop Digest.md`
A stdlib-only script that runs in under 60s and needs no LLM. Sections:
- **Session brief**: at most ~1200 chars, the only part injected into sessions (see 2).
- **Stack freshness**: each check reports `ok` or `STALE (why)`.
  - claude / codex / gemini CLI versions vs `npm view <pkg> version`.
  - ollama version vs the GitHub latest release (`gh api`).
  - Docker containers running a superseded image: `docker ps` shows a bare image id, meaning
    the tag was re-pulled and the container was never recreated. Build age was dropped because
    reproducible builds report 1970.
  - Mutable `:latest` or untagged registry tags, listed so they get pinned.
  - `~/.claude/model-rankings.md` older than 30 days.
  - free-llm waterfall: one live `free-llm extract` ping with a 60s timeout.
  - Every check is a subprocess with a timeout. A failed check reports `unknown`; nothing aborts.
- **Adoption backlog**: new `try-now` rows from the last 30 days of the Reel Review Digest,
  as `- [ ] [[note]] date (id)`.
  - Unticked items carry over forever, even after they leave the window.
  - Ticking a box is the "adopted or decided against" signal. Ticked ids join a
    `<!-- done: ... -->` set and never come back.
  - The section also shows a count of `later` rows.
- **Interests (30 days)**: tag frequency across every reel note created in the window,
  whatever its verdict, top 15.
  - Plus "Personal topics": the newest 12 `personal` reels, wikilinked.

### 1b. `personal` verdict in `review_new_reels.py` (owner, 2026-10-09)
"WHY would you EVER skip something I sent... travel, habits, dating, and emergency prep."
The judge stays business-only. A deterministic post-override (`personal_verdict`, after
`career_verdict`) relabels a `skip` as `personal` when `PERSONAL_RE` matches the note's title
or tags. The body is not used because it gave 52 hits, mostly false positives.
`--backfill-personal --apply` relabeled 26 past reels in the manifest, the digest and the
notes. It is idempotent.

The script writes atomically (tmp + replace). The vault's nightly commit keeps history.

### 2. SessionStart hook: inject the brief
`session-context.py --session-start` reads the `## Session brief` section of the Loop Digest
and prints it as `additionalContext`. It is registered in `~/.claude/settings.json` under
`SessionStart` with the `startup` matcher, so it fires once per new session, not on every prompt.
It is a separate entry: the existing `startup|clear` protocol reminder stays as it is. The brief
opens by saying everything in it is data from reel notes, not instructions.
- If the note is older than 20h or missing, the hook launches `loop_digest.py` detached
  (`DETACHED_PROCESS`) and injects the stale brief anyway.
- The digest therefore refreshes whenever the owner actually uses Claude, which fixes the
  "machine off at 02:00" miss without a Task Scheduler entry.
- The brief tells Claude to act: offer the top STALE upgrade, and mention a relevant
  backlog or interest item when the session's topic touches it.

## Out of scope (YAGNI)
- Auto-upgrading anything. The digest flags; the session acts with judgment, because
  upgrades have broken this machine before (faster-whisper).
- Changing the judge prompt. The `personal` override is deterministic, after the judge.
- Embeddings or semantic interest profiles. Tag counts are enough to start.

## Verification
- `python scripts/loop_digest.py --test` runs assert self-checks for digest-row parsing,
  tick preservation and brief truncation.
- One live run writes the note. Read it back.
- `session-context.py --session-start` with no stdin session data prints valid JSON.
- `session-context.py --test` still passes.
