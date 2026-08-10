# Architecture

## Goal

Turn a video/reel/photo-post URL into a structured Obsidian note (and, for
high-signal content, a reusable Claude/Codex skill artifact) with no browser
automation and no hardcoded secrets.

## Data flow

```
data/inbox/queue.txt ---\
                         >-- QueueManager --> data/inbox/state.json (source of truth)
webhook POST /webhook --/                 \-> data/inbox/needs-attention.txt (blocked/invalid URLs)

WorkerPipeline.run_once(), for each actionable state.json record:
  validate/dedup (already done by QueueManager)
    -> classify_url_kind() decided text vs. media at registration time (validators.py):
         text  -> TextFetcher.fetch(url)                     -> TranscriptResult
         media -> Downloader.download(url) -> DownloadResult -> branch on media_type:
                    VIDEO -> Transcriber.transcribe(media_paths[0])      -> TranscriptResult
                    IMAGE -> ImageDescriber.describe(media_paths)        -> TranscriptResult (vision-model description)
    -> Enricher.enrich(transcript)     -> EnrichmentResult (LLM + config/prompts/enrich_transcript.md)
    -> obsidian_writer.write_note()    -> data/notes/<slug>.md   (or REEL_VAULT_DIR)
    -> SkillWriter.generate()          -> data/generated_skills/<slug>/SKILL.md, only if high_signal
  state.json record updated to DONE (or FAILED/FAILED_PERMANENT, with reason appended to needs-attention.txt)
```

Video and image posts converge back into the same `TranscriptResult` shape
after extraction, so enrichment, note-writing, and skill generation are
identical regardless of which branch produced the text.

## Modules (`src/reel_pipeline/`)

| Module | Responsibility |
| --- | --- |
| `config.py` | Typed `Settings`, loaded from `config/settings.yaml` + env overrides + secrets |
| `models.py` | Pydantic models shared by every stage (the enrichment output contract lives here) |
| `validators.py` | URL normalization, deterministic `content_id` hashing, allow/block-list checks |
| `queue_manager.py` | Reads `queue.txt`, persists `state.json`, writes `needs-attention.txt` |
| `downloader.py` | Media download - dispatches Instagram to gallery-dl (cookie-authenticated), everything else (YouTube, Facebook, LinkedIn, TikTok, X, Vimeo, Google Drive) to yt-dlp, optionally cookie-authenticated. Detects photo-only posts and returns `media_type=IMAGE` with all carousel image paths. A downloaded ".mp4" with no audio track (Instagram sometimes serves a "photo" as a silent motion-photo clip) is probed via `ffprobe` and reclassified as `IMAGE` too - a frame is extracted via `ffmpeg` so it goes through the vision path instead of failing transcription with "no audio track" |
| `transcriber.py` | Pluggable transcription backends (local faster-whisper / OpenAI API) - video/audio only |
| `image_describer.py` | Turns photo posts/carousels into a text description via a vision-capable LLM (`config/prompts/describe_image_post.md`), producing the same `TranscriptResult` shape as `transcriber.py` |
| `text_fetcher.py` | Text-content capture for non-media sources - GitHub (public REST API: repo metadata + README, or a specific file for a `blob/` URL), Notion (its own internal JSON API, `loadPageChunk` - see below), Drive documents (unauthenticated direct-download endpoint - see below), everything else via plain HTTP GET + `trafilatura` extraction (no JS execution). Produces the same `TranscriptResult` shape `transcriber.py`/`image_describer.py` do |
| `enricher.py` | Calls an LLM (via `llm_client.py`) with `config/prompts/enrich_transcript.md`, parses `EnrichmentResult` |
| `obsidian_writer.py` | Deterministic markdown note writer |
| `skill_writer.py` | Conditional `SKILL.md` generator for high-signal content |
| `worker.py` | Orchestrates one pass end-to-end (`WorkerPipeline.run_once`) |
| `webhook_server.py` | FastAPI app: shared-secret-authenticated `/webhook`, background processing |
| `cli.py` | Typer entry point: `run-once`, `serve-webhook` |
| `logging_setup.py` | Structured JSON logging to console + `data/logs/pipeline.log` |
| `llm_client.py` | Shared LLM call, dispatched by `llm.provider`: `anthropic` (Claude API) and `ollama` (local instance) support text and vision; `groq` and `gemini` support text only (enricher, skill_writer) - used by enricher, skill_writer, and image_describer |

## Prompt caching

All four prompt templates under `config/prompts/` put shared, repeated
instruction text first and per-call variable content (source URL, transcript,
image count, etc.) last, split by a `<!-- CACHE:BOUNDARY -->` marker that
`enricher.render_and_split()` parses into `(static_prefix, prompt)`. This
ordering is what makes prefix-keyed caching actually hit:

- **Groq**: automatic prefix-match caching, no code required - only benefits
  if the shared instructions are a literal prefix of the request, which the
  reordering guarantees.
- **Gemini**: automatic implicit caching (2.5+ models), same prefix
  requirement, subject to a minimum input-token threshold.
- **Anthropic**: not automatic - `llm_client.call_llm()`'s `static_prefix`
  param is sent as a separate `system` block with `cache_control: {"type":
  "ephemeral"}` (see `_call_claude`/`_call_claude_vision`), which is the
  actual mechanism Claude requires.
- **Ollama**: no caching; `static_prefix` is just prepended to the prompt
  text for consistent behavior across providers.

If a provider's model is changed, prefer Gemini's `gemini-3.5-flash-lite`
(GA, same price as the previously-used `gemini-2.5-flash`) for the
enrichment/skill-writer extraction task - it's the vendor's current
recommendation for high-throughput structured extraction.

## Why state.json, not just the queue file

`queue.txt` is a disposable inbox - a human (or the webhook) appends URLs to
it. `state.json` is the durable record keyed by a deterministic `content_id`
(sha256 of the normalized URL, truncated). Every ingestion path - queue file
or webhook - converges on `state.json` before any processing begins, so:

- Re-adding the same URL is a no-op (dedup by `content_id`).
- A crash mid-processing leaves an accurate `status` (e.g. `transcribing`)
  in `state.json`; the next `run-once` picks it back up via
  `QueueManager.get_actionable_items()`, which returns every record not yet
  `done`, `blocked`, or `failed_permanent`.
- `queue.txt` itself is drained (consumed lines removed) once its content is
  durably represented in `state.json` or `needs-attention.txt`.

### Retries and resumability

- A stage failure increments `attempt_count` and schedules the next attempt
  via `retry.backoff_schedule_minutes` in `config/settings.yaml` (exponential
  backoff, capped at `retry.max_attempts`). Once attempts are exhausted the
  item becomes `failed_permanent` and stops being retried automatically -
  `uv run python -m reel_pipeline.cli retry <content_id>` (or
  `--all-failed-permanent`) resets it once the underlying cause is fixed.
- Each pipeline stage (download, transcribe/describe, enrich) records
  `last_completed_stage` and caches its output artifact under
  `data/tmp/<content_id>/` (the downloaded media file(s), plus
  `transcript.json`/`enrichment.json`). A crash mid-pipeline resumes from the
  most-advanced cached artifact still present on disk instead of redoing
  already-finished work - falling back one stage at a time if an expected
  artifact is missing rather than trusting the stage marker blindly. This
  cache is cleaned up alongside the rest of `data/tmp/<content_id>/` once the
  item succeeds.
- Two independent file locks guard `state.json`: a coarse, whole-pass lock
  (`state.run_once.lock`) so two `run_once()` calls never interleave and
  double-process the same item, and a fine-grained per-mutation lock
  (`state.lock`) around every individual read-modify-write, so webhook
  registration (`add_url()`) is never blocked waiting behind an in-progress
  backlog pass. See `docs/superpowers/specs/2026-07-29-state-reliability-design.md`
  for the full design and the tradeoffs considered.
- `state.json` is fully reparsed and rewritten on every mutation - an
  intentional, monitored tradeoff at this project's scale (see
  `maintenance.state_size_warning_threshold` in `config/settings.yaml`), not
  a scalability guarantee for large backlogs.

## Safety guardrails (see `docs/runbook.md` for details)

- No browser automation, ever. Only `download.allowed_domains` in
  `config/settings.yaml` are attempted, via yt-dlp or gallery-dl (both
  cookie-authenticated CLI tools, never a driven browser session).
  `download.blocked_domains` (if any are added later) is enforced up front:
  those URLs are routed to `needs-attention.txt` with a clear reason instead
  of being downloaded.
- Instagram, Facebook, LinkedIn, and Google Drive are each a deliberate, scoped
  exception to `CLAUDE.md`'s "no social platform by default" baseline - enabled
  here for low-volume personal use with the account owner's own real account, via
  gallery-dl + explicit cookie config (`REEL_INSTAGRAM_COOKIES_FILE`/`_BROWSER`)
  for Instagram, and yt-dlp + optional cookie config
  (`REEL_YTDLP_COOKIES_FILE`/`_BROWSER`) for Facebook/LinkedIn/Drive. Drive is
  allow-listed as the specific subdomain `drive.google.com`, not `google.com` -
  `validate_url` in `validators.py` matches allow-list entries against the full
  host rather than a reduced two-label domain, so this doesn't open other Google
  properties (Docs, Sheets, Photos, Search). YouTube/TikTok/X/Vimeo stay
  anonymous. See `docs/runbook.md` and the research note in the Obsidian vault
  (`20-Resources/Tools/`).
- Any URL not matched by `download.allowed_domains` is captured as text via a
  separate, public-links-only path (GitHub's REST API for `github.com`,
  Notion's own internal JSON API for `notion.so`/`notion.site`/`notion.com`,
  plain HTTP GET + `trafilatura` extraction for everything else) - no OAuth,
  no API keys, no browser automation, no JS execution. As of 2026-08-10 this
  is a catch-all (`classify_url_kind()` in `validators.py`), not an
  enumerated allow-list - see `CLAUDE.md`'s 2026-08-10 entry for why. Notion
  specifically needed its own fetcher (`NotionFetcher`) rather than the
  generic path: Notion's frontend ships zero content in server-sent HTML for
  any page, so `trafilatura` can never extract anything from it regardless of
  domain policy - see `CLAUDE.md`'s Notion entry and
  `docs/superpowers/specs/2026-08-10-notion-api-text-capture-design.md`. A
  page that's a JS-rendered app shell with no server-side content and no
  equivalent API to call directly still fails cleanly with a clear error
  rather than falling back to browser automation - a hard no per this file's
  first guardrail above. `drive.google.com` is a special case even among
  these: unlike every other domain, the same URL shape covers both a video
  file (media path, above) and an arbitrary shared document (text path,
  here), so `classify_url_kind()` runs a live yt-dlp metadata-only probe just
  for that one host to tell them apart before deciding - see `CLAUDE.md`'s
  Drive entry and
  `docs/superpowers/specs/2026-08-10-drive-text-capture-design.md`. See
  `docs/superpowers/specs/2026-07-16-text-capture-ingestion-design.md` for the
  original scoped-allow-list design this superseded.
- Secrets (`ANTHROPIC_API_KEY`, `REEL_WEBHOOK_SECRET`, `OPENAI_API_KEY`,
  Instagram/yt-dlp cookies) are only ever read from the environment / `.env` -
  never from `config/settings.yaml`, never hardcoded.

## Skill generation vs. this repo's own skills

`skill_writer.py` writes generated artifacts under `data/generated_skills/`
(configurable via `REEL_SKILLS_DIR`), deliberately separate from
`.claude/skills/`, which holds this repository's own authoring/maintenance
skills (see `docs/runbook.md`).
