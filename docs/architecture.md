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
    -> Downloader.download(url)  -> DownloadResult(media_type, media_paths=[...])
    -> branch on media_type:
         VIDEO -> Transcriber.transcribe(media_paths[0])      -> TranscriptResult
         IMAGE -> ImageDescriber.describe(media_paths)        -> TranscriptResult (vision-model description)
    -> Enricher.enrich(transcript)     -> EnrichmentResult (LLM + config/prompts/enrich_transcript.md)
    -> obsidian_writer.write_note()    -> data/notes/<content_id>-<slug>.md   (or REEL_VAULT_DIR)
    -> SkillWriter.generate()          -> data/generated_skills/<slug>/SKILL.md, only if high_signal
  state.json record updated to DONE (or FAILED, with reason appended to needs-attention.txt)
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
| `downloader.py` | Media download - dispatches Instagram to gallery-dl (cookie-authenticated), everything else (YouTube, Facebook, LinkedIn, TikTok, X, Vimeo, Google Drive) to yt-dlp, optionally cookie-authenticated. Detects photo-only posts and returns `media_type=IMAGE` with all carousel image paths |
| `transcriber.py` | Pluggable transcription backends (local faster-whisper / OpenAI API) - video/audio only |
| `image_describer.py` | Turns photo posts/carousels into a text description via a vision-capable LLM (`config/prompts/describe_image_post.md`), producing the same `TranscriptResult` shape as `transcriber.py` |
| `enricher.py` | Calls an LLM (via `llm_client.py`) with `config/prompts/enrich_transcript.md`, parses `EnrichmentResult` |
| `obsidian_writer.py` | Deterministic markdown note writer |
| `skill_writer.py` | Conditional `SKILL.md` generator for high-signal content |
| `worker.py` | Orchestrates one pass end-to-end (`WorkerPipeline.run_once`) |
| `webhook_server.py` | FastAPI app: shared-secret-authenticated `/webhook`, background processing |
| `cli.py` | Typer entry point: `run-once`, `serve-webhook` |
| `logging_setup.py` | Structured JSON logging to console + `data/logs/pipeline.log` |
| `llm_client.py` | Shared LLM call (text and vision), dispatched by `llm.provider` (`anthropic` Claude API or a local `ollama` instance), used by enricher, skill_writer, and image_describer |

## Why state.json, not just the queue file

`queue.txt` is a disposable inbox - a human (or the webhook) appends URLs to
it. `state.json` is the durable record keyed by a deterministic `content_id`
(sha256 of the normalized URL, truncated). Every ingestion path - queue file
or webhook - converges on `state.json` before any processing begins, so:

- Re-adding the same URL is a no-op (dedup by `content_id`).
- A crash mid-processing leaves an accurate `status` (e.g. `transcribing`)
  in `state.json`; the next `run-once` picks it back up via
  `QueueManager.get_actionable_items()`, which returns every record not yet
  `done` or `blocked`.
- `queue.txt` itself is drained (consumed lines removed) once its content is
  durably represented in `state.json` or `needs-attention.txt`.

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
- Secrets (`ANTHROPIC_API_KEY`, `REEL_WEBHOOK_SECRET`, `OPENAI_API_KEY`,
  Instagram/yt-dlp cookies) are only ever read from the environment / `.env` -
  never from `config/settings.yaml`, never hardcoded.

## Skill generation vs. this repo's own skills

`skill_writer.py` writes generated artifacts under `data/generated_skills/`
(configurable via `REEL_SKILLS_DIR`), deliberately separate from
`.claude/skills/`, which holds this repository's own authoring/maintenance
skills (see `docs/runbook.md`).
