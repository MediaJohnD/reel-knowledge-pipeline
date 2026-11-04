# Project memory for Claude Code

## Mission
Build and maintain a safe-by-default short-form and video knowledge ingestion system that turns URLs into structured Obsidian notes and optional Claude/Codex skill artifacts.

## Primary workflow
The system supports:
1. URL ingestion from a queue file (`data/inbox/queue.txt`) and webhook (`POST /webhook`)
2. media download (`downloader.py`, via yt-dlp or gallery-dl for Instagram) - detects photo-only posts/carousels vs. video
3. content extraction: audio transcription for video (`transcriber.py`, local faster-whisper or OpenAI API) or vision-model description for photo posts (`image_describer.py`) - both produce the same `TranscriptResult` shape
4. transcript enrichment (`enricher.py`, LLM via `llm_client.py` - local Ollama by default, or Claude - + `config/prompts/enrich_transcript.md`)
5. Obsidian markdown note writing (`obsidian_writer.py`)
6. optional skill generation for high-signal content (`skill_writer.py`)
7. logging, idempotency, and recovery (`logging_setup.py`, `queue_manager.py`, `worker.py`)

## Risk posture
- No *interactive* browser automation, ever - no account actions, form fills,
  clicks, or logins. This was an unqualified "no browser automation, ever"
  until 2026-08-10, when it was deliberately narrowed (owner sign-off given
  explicitly, not inferred) to add one scoped exception: `text_fetcher.py`'s
  `RenderedHtmlFetcher`, a read-only headless-Chromium fallback used only
  when the plain-GET + `trafilatura` text-capture path already failed on a
  client-rendered app shell (`text_capture.render_fallback` in
  `config/settings.yaml`, default on). It navigates, waits for the page to
  settle, and reads the rendered DOM - no clicks, no scrolling, no form
  fills, no cookies/login/session reuse from the yt-dlp/gallery-dl cookie
  config, and no anti-detection/fingerprint-spoofing tooling (a site that
  blocks headless Chromium is respected, not worked around - confirmed live
  against `roadmap.notion.site`, which sits behind a bot-detection challenge
  the fallback correctly refuses to try to pass, raising a clear error
  instead of capturing the challenge page as content). Confined to
  `text_fetcher.py`; `downloader.py` (yt-dlp/gallery-dl) is untouched and
  still the only thing that ever uses cookies. See
  `docs/superpowers/specs/2026-08-10-js-rendered-text-capture-design.md`.
- Second scoped exception, 2026-09-26 (owner sign-off given explicitly):
  `comment_gate.py` drives a headed Chrome on a dedicated persistent profile
  (`REEL_IG_BROWSER_PROFILE`) that the owner logs into by hand, to get links
  from "comment KEYWORD for the link" reels: follow the creator, comment the
  keyword, read the DM, press allowlisted quick-reply buttons, and type the reel's keyword back once if the bot asks for it (added 2026-10-02, `reply_keyword`). It is
  owner-run only (`cli comment-queue run`, never the worker or webhook; since
  2026-10-02 the owner-created Task Scheduler task "Reel Comment Queue" runs it
  daily at 14:00 UTC (10:00 EDT, 09:00 EST), the rate limits still apply) and
  dry-run by default (`--apply` to act). Hard rate limits live in
  `comment_gate:` in `config/settings.yaml`: 3 writes per 15 min, random
  3-6 min gaps, 8 comments per day (the day resets at 07:00 local) in the first week (raised from 3 on 2026-09-28, to 8 on 2026-10-02, at the owner's request). It halts for 24 h on any
  challenge or action-blocked dialog. Timed-out DM threads get read again (10 per run, weekly, 6 times at most, added 2026-11-01); those are reads and use no write slots. Code never sees or types the password,
  never reuses the yt-dlp/gallery-dl cookies, and uses no anti-detection
  tooling. Confined to `comment_gate.py`. See
  `docs/superpowers/specs/2026-09-26-comment-gated-links-design.md`.
- Instagram is explicitly enabled as of 2026-07-11 - a deliberate, scoped exception to the
  general "no Instagram by default" baseline, made for the project owner's own real account
  (no burner) at low volume ("a handful of reels that matter", not bulk/scheduled scraping).
  See `docs/runbook.md`'s "Instagram setup" section and the research note in the Obsidian
  vault (`20-Resources/Tools/Instagram Reel Ingestion Research (2026-07-11).md`).
- Facebook and LinkedIn are enabled as of 2026-07-14 under the same terms as Instagram -
  cookie-authenticated via yt-dlp (`REEL_YTDLP_COOKIES_FILE`/`REEL_YTDLP_COOKIES_BROWSER`,
  optional since public YouTube works without them), owner's own real account, low volume.
  LinkedIn cookies are effectively inert as of yt-dlp 2026.07.04 (the pinned version), which
  removed LinkedIn login support entirely (upstream commit a5e0f87, issue #17039). Separately,
  yt-dlp's LinkedIn extractor only recognizes `linkedin.com/posts/...` and
  `linkedin.com/feed/update/urn:li:activity:<id>` URLs - `urn:li:groupPost:...` URLs (Group
  posts) aren't matched at all and 404 via the generic extractor. Both are verified, specific
  gaps (see `src/reel_pipeline/downloader.py`'s module docstring), not a config problem or
  vague "historically unreliable" hand-wave. YouTube/TikTok/X/Vimeo remain anonymous
  (no cookies configured/required) since they're public by default.
- `drive.google.com` is enabled as of 2026-07-16, scoped to that exact subdomain (not all of
  `google.com` - Docs/Sheets/Photos/Search stay unreachable) for video files shared via Google
  Drive, using yt-dlp's existing GoogleDrive extractor. Same optional cookie treatment as
  Facebook/LinkedIn. `validate_url` in `validators.py` matches `allowed_domains` against the
  full host, not a reduced two-label domain, specifically so a subdomain entry like this one
  doesn't accidentally open the whole parent domain. As of 2026-08-10, `drive.google.com` also
  handles shared *documents* (text, markdown, PDF, PPT, ...), not just video - a real failure
  surfaced this: a shared `.md` skill file failed yt-dlp's video path with an opaque "400 Bad
  Request" despite being fully public (verified via the Drive MCP's `get_file_permissions`
  before assuming otherwise). `classify_url_kind()` in `validators.py` now runs a metadata-only
  yt-dlp probe (`download=False`, no bytes fetched) specifically for `drive.google.com` URLs to
  tell video from document - the only domain in this pipeline where the URL shape alone can't
  say which. A non-video Drive URL routes to the new `DriveFetcher` (`text_fetcher.py`), which
  fetches the file via Drive's unauthenticated direct-download endpoint - no OAuth, no API key,
  same posture as GitHub/Notion. See
  `docs/superpowers/specs/2026-08-10-drive-text-capture-design.md`. As of 2026-11-01 a shared
  *folder* URL works too: `DriveFetcher` reads the public embedded-folder listing and combines
  up to 25 direct files into one note. A file that fails is noted and skipped; subfolders and
  native Docs are linked by name, not recursed into.
- Text-capture ingestion (`text_fetcher.py`: GitHub's public REST API, or plain HTTP GET +
  `trafilatura` extraction for everything else) was introduced 2026-07-16 as a scoped
  allow-list (`github.com`, `notion.so`, `notion.site`), then broadened piece by piece as
  specific public-doc hosts (`docs.google.com`, `airtable.com`, `findarepo.com`,
  `thefounderos.com`) turned out to have the same low-risk profile - a plain unauthenticated
  GET with no JS execution, same as any RSS reader. As of 2026-08-10, at the project owner's
  explicit direction (personal knowledge vault - "not just Reels and YouTube
  Shorts... any knowledge or topics I find interesting"), text-capture is a **catch-all**:
  `classify_url_kind()` in `validators.py` routes any http(s) URL not matched by
  `download.allowed_domains` to the text-capture path by default, unless it's listed in
  `text_capture.blocked_domains` (empty by default). This does not touch the media
  (yt-dlp/gallery-dl) allow-list above, which stays scoped - only the no-auth, no-JS text
  path was opened up, since per-domain enumeration there was pure friction with no real
  safety benefit (the risk profile is identical for every domain: an outbound GET to a URL
  the user themself submitted). See
  `docs/superpowers/specs/2026-07-16-text-capture-ingestion-design.md` for the original
  (now superseded) scoped-allow-list rationale. As of 2026-11-01 the catch-all refuses
  loopback, private, link-local, and other non-public targets, on every redirect hop and on
  every request the headless fallback makes (`is_private_or_local_host` in `validators.py`).
  A submitted URL can no longer read a service on this machine or its network. The plain-HTTP
  fetchers (`_guarded_client` in `text_fetcher.py`) resolve once and connect to the address
  they checked, so DNS rebinding can't get past them; the client also ignores proxy
  environment variables, because a proxy would resolve the name itself. The headless fallback
  sends all its traffic through a local proxy (`_PublicOnlyProxy` in `text_fetcher.py`) that
  applies the same check and connects to the address it checked, so rebinding is closed there
  too. WebRTC can't use a proxy, so Chromium is told not to send it outside one.
- `scripts/recapture_carousels.py` (2026-11-01) re-captures carousel posts saved before the
  `img_index` fix. Owner-run only, never scheduled: at most 2 posts per day, halts 24 h on a
  failure, dry-run unless `--apply`. See `docs/runbook.md`.
- Monthly audits (2026-11-01), run from `run-review.bat` once 30 days have passed.
  `scripts/recheck_links.py` requests note URLs through the same private-target guard as
  text capture, without cookies. `scripts/vet_repos.py` reads repos through the GitHub API
  as text only and never clones, installs, or runs them. Neither edits a reel note. See
  `docs/runbook.md`.
- Notion pages (`notion.so`/`notion.site`/`notion.com`) get a dedicated `NotionFetcher` as
  of 2026-08-10, instead of falling through to the generic HTML fetcher. Reason: Notion's
  current frontend ships zero page content in the server-sent HTML for *any* page, public
  or private - confirmed by direct inspection, not an assumption - so `trafilatura` can
  never extract anything from a Notion URL no matter how permissive the domain policy is.
  `NotionFetcher` instead calls Notion's own internal JSON API (`POST
  https://app.notion.com/api/v3/loadPageChunk`) directly over plain HTTP - the same API
  call Notion's own web client makes, just called without rendering a page around it. No
  browser, no JS execution, no credentials for public pages; this does **not** touch the
  "no browser automation, ever" guardrail above (a headless-browser fallback was
  considered and explicitly rejected in favor of this - see
  `docs/superpowers/specs/2026-08-10-notion-api-text-capture-design.md`). The endpoint is
  unofficial/undocumented (reverse-engineered by the open-source community, e.g.
  `react-notion-x`) and could change or break without notice - an accepted tradeoff,
  since Notion has no official public read API for pages the requester doesn't own.
  Known gaps, documented rather than silently wrong: pagination stops after 10 chunks
  (about 1000 blocks; added 2026-11-01, the note says when it stopped early),
  linked-database rows aren't expanded (noted by title only), and a bare
  custom-subdomain root URL with no page id in the path (as opposed to the standard
  `.../Some-Title-<id>` share-link shape) isn't resolvable.
- More formats and fallbacks, 2026-11-01, at the owner's direction ("accept more formats and
  links, and fix the issues before discarding or erroring them"). See
  `docs/superpowers/specs/2026-11-01-more-formats-and-fallbacks-design.md`.
  - `download.allowed_domains` adds `loom.com`, `reddit.com`, `v.redd.it`, `soundcloud.com`,
    `twitch.tv`, `rumble.com`, `dailymotion.com`, `ted.com`, `streamable.com`, `wistia.com`,
    `wistia.net`. No cookies were added for them, and `download.anonymous_domains` lists the
    same hosts, so `REEL_YTDLP_COOKIES_*` never reaches them even when set. Checked live
    2026-11-01: TED, SoundCloud, Streamable, Loom, Twitch VODs and Wistia work. Three don't
    work anonymously today: Reddit (yt-dlp wants an account, the page blocks automation),
    Dailymotion (yt-dlp needs impersonation tooling, which is refused as anti-detection), and
    Rumble (403 to yt-dlp and to text capture). Reddit cookies stay off: on 2026-11-01 none of
    the 773 items, the queue, or the needs-attention history was a Reddit link. Reconsider if
    Reddit links start arriving; turning them on is a new login-gated platform decision.
  - Text capture now reads `.xlsx`, `.epub`, `.odt`/`.ods`/`.odp` (`document_text.py`, standard
    library, zip-bomb guard), PDF, DOCX and PPTX, CSV/TSV/YAML/JSON, RSS and Atom (regex, no
    XML parser, no entity expansion), and Sheets exports. A scanned PDF goes to the vision
    path, up to 40 images and 256 MB, skipping an image it can't use before decoding it. Downloads
    are capped by `text_capture.max_document_mb` and `max_media_mb`.
  - Embedded media on a thin page (direct file, allow-listed iframe or `og:video`, one guarded
    hop for a `text/html` player, HLS) is downloaded. HLS playlists and segments stay on the
    guarded client and never go to yt-dlp, and an encrypted stream (any `EXT-X-KEY` other than
    NONE) is refused, not worked around. yt-dlp and gallery-dl only get hosts that pass
    `validate_url`.
  - A thin page whose render failed or is off keeps its title and a description of at least 60
    chars, labeled as such. Never after a challenge or on login text.
  - Wayback fallback (`fetch_archived`): 404/410 go to the archive at once, 5xx and connection
    errors only on the last attempt. 403/401/429 and challenge pages never go to the render or
    the archive: a site that blocks automation is still respected. A URL with a login in it, or
    whose query or fragment looks private (token, key, secret, signature, auth, sig, session,
    credential and variants), is never sent to the archive. Text capture still sends no cookies.
  - Media-to-text fallback: `NoMediaError` at once, other download errors on the last attempt,
    and an empty transcript go to text capture, only for
    `text_capture.media_text_fallback_domains`, with at least 200 chars.
    `PostUnavailableError` stays terminal. gallery-dl falls to yt-dlp once. A failure that reads
    as a refusal (401, 403, 429, 451, rate limit, challenge, checkpoint, login; `is_refusal` in
    `downloader.py`) goes to neither: it keeps its normal retries and nothing else.
  - Pastes: bare `domain.tld/path`, `<https://...>`, markdown links and several links in one
    paste are accepted. Known wrappers (`l.instagram.com`, `l.facebook.com`, `lm.facebook.com`,
    `google.com/url`, `linkedin.com/safety/go`, `youtube.com/redirect`) are unwrapped
    (`unwrap_redirect` in `validators.py`), and the target is still validated.
- Any `download.blocked_domains` entries are still enforced up front - blocked URLs route
  to `data/inbox/needs-attention.txt`, never downloaded.
- Treat any other third-party account automation as out of scope unless explicitly requested.
- Never hardcode secrets, cookies, or personal absolute paths. Secrets (including the
  Instagram/yt-dlp cookies file/browser choice) live only in environment variables (`.env`,
  never committed) - see `.env.example`.

## Architecture rules
- Capture, queue, downloader, transcriber, enricher, note writer, and skill writer stay in separate modules under `src/reel_pipeline/` (see `docs/architecture.md`).
- Prompts live in versioned markdown files in `config/prompts/`, never as inline strings.
- Use typed config (`config.py`) and typed models (`models.py`).
- The worker (`worker.py`) is restart-safe and idempotent: `state.json` is the single source of truth for dedup and in-flight status, keyed by a deterministic `content_id`.
- Note and skill filenames are the title slug alone (`<title-slug>`), with no content_id prefix - a hex ID in every filename read as noise in Obsidian's file explorer and graph view. Idempotency on re-processing comes from `state.json` tracking each item's previous note/skill path and `worker.py` deleting it when a re-run's title (and thus filename) changes - not from the filename itself. A content_id is only consulted to disambiguate a genuine slug collision between two different items.

## Output contracts
Each Obsidian note includes:
- title
- source_url
- content_id
- created_at
- tags
- tools_mentioned
- summary
- key_takeaways
- full transcript

Skill artifacts (`SKILL.md`, under `data/generated_skills/`) are created only when enrichment marks the content `high_signal` **and** supplies a `skill_candidate_reason`.

## Verification
Do not claim success without:
- lockfile in sync (`uv lock --check`)
- lint (`uv run ruff check .`)
- typecheck (`uv run pyright`)
- tests (`uv run pytest`)
- dependency vulnerability scan (`uv audit`)
- at least one realistic worker-flow verification (see `tests/test_worker_flow.py`, or a manual `run-once` with real credentials)

`make check` runs all of the above except the worker-flow verification.

## Commands
- Install: `uv sync` (add `--extra local-whisper` for the local transcription backend; dev tooling installs by default via `[dependency-groups]`, no `--extra` needed)
- Test: `uv run pytest`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .`
- Typecheck: `uv run pyright`
- Audit dependencies: `uv audit`
- Run worker once: `uv run python -m reel_pipeline.cli run-once`
- Run webhook server: `uv run python -m reel_pipeline.cli serve-webhook`

## Completion checklist
- request addressed directly
- code follows repo patterns
- tests and checks run
- diff reviewed for accidental scope creep
- risks summarized
