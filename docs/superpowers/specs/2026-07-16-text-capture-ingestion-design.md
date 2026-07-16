# Text-Capture Ingestion Path — Design

Status: proposed, pending user review.

## Purpose

The pipeline currently only understands one kind of source: video/photo
media, downloaded via yt-dlp/gallery-dl and turned into text via
transcription or vision description. Creators regularly also share GitHub
repos and Notion pages, which aren't media at all — a download+transcribe
attempt on these fails outright (as observed with `github.com`/`notion.so`
links routed to `needs-attention.txt`, and demonstrated live by the
`findarepo.com` case, which is a GitHub-discovery site, not a video host).

This adds a second, parallel ingestion path that captures the page's text
content directly and reuses the existing enrichment → Obsidian note →
skill-artifact pipeline unchanged.

## Scope (v1)

- **In scope:** `github.com` (repo README + metadata, via GitHub's public
  REST API) and `notion.so`/`notion.site` (publicly-shared pages, via a plain
  HTTP fetch + text extraction).
- **Explicitly out of scope for v1:** Airtable. Its public share views are
  React apps requiring JS execution to render real content, which conflicts
  with this project's no-browser-automation rule. Airtable links continue to
  route to `needs-attention.txt` like any other unsupported domain.
- **Explicitly out of scope for v1:** private/authenticated content on any
  platform. Only publicly-shared links are supported — no new OAuth or API-key
  credential surface, consistent with how the rest of this pipeline avoids
  account automation beyond the owner's own already-configured cookie
  sessions.
- **Explicitly out of scope for v1:** a generic catch-all for arbitrary
  websites/blogs. Only the two named platforms.

## Architecture

A new module, `text_fetcher.py`, sits alongside `downloader.py` +
`transcriber.py` + `image_describer.py`. It produces a `TranscriptResult` —
the same model those three already produce — so `Enricher`, `write_note()`,
and `SkillWriter` need no changes to their output contract.

```
QueueManager (validate/dedup)
  -> classify_url_kind(url, settings) -> "media" | "text" | None
       "media" -> Downloader -> Transcriber/ImageDescriber -> TranscriptResult
       "text"  -> TextFetcher                              -> TranscriptResult
  -> Enricher (prompt selected by TranscriptResult.content_kind)
  -> write_note() / SkillWriter (unchanged)
```

### Config changes (`config/settings.yaml`, `config.py`)

A new top-level `text_capture` section, parallel to `download`:

```yaml
text_capture:
  allowed_domains:
    - github.com
    - notion.so
    - notion.site
```

`validators.py` gains `classify_url_kind(url, settings) -> Literal["media",
"text"] | None`, checked by `QueueManager` at registration time (same point
`validate_url()` already runs) instead of only accepting `download.
allowed_domains`. `blocked_domains` still applies globally, ahead of either
check. A URL matching neither list is rejected exactly as today (routed to
`needs-attention.txt` with "not in the configured allow-list"). The two
allow-lists are expected to stay disjoint (no domain in both) since the
platforms don't overlap in practice; `classify_url_kind()` checks `media`
first as a defensive tie-break if that assumption is ever violated, and this
is called out explicitly so it's a documented decision, not an accident.

`StateRecord` gains a `content_kind: Literal["media", "text"]` field so a
retried/resumed item doesn't need to re-derive its kind from the URL (mirrors
how `media_type` is already known once a `DownloadResult` exists, but text
items never produce one).

### `text_fetcher.py`

```python
class TextFetcher:
    def fetch(self, url: str, content_id: str) -> TranscriptResult: ...
```

Dispatches on host, same pattern as `DispatchingDownloader`:

- **GitHub** (`_fetch_github_repo`): parses the URL path into `owner/repo`
  plus an optional `blob/<ref>/<path>` suffix — creators share both repo-root
  links and links to one specific file (e.g. a blocked URL seen during this
  project's own testing pointed at `.../blob/main/Anthropic/some-file.md`,
  not a repo root). Two shapes, both ending in the same `TranscriptResult`:
  - **Repo root** (`github.com/owner/repo` or `.../tree/<ref>`): calls
    `GET api.github.com/repos/{owner}/{repo}` (metadata: description, stars,
    language, topics) and `GET .../readme` (base64-decoded). Combines
    metadata + README into `.text`.
  - **Specific file** (`.../blob/<ref>/<path>`): calls
    `GET api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}`
    (base64-decoded file content) plus the same repo metadata call for
    context. `.text` is the file content, not the README — matches what the
    sharer actually pointed at.

  Both shapes together cost at most 2 unauthenticated API calls per URL;
  the 60 req/hr unauthenticated rate limit is comfortably enough for personal
  low-volume sharing. A 404 (private/nonexistent repo or file) raises
  `TextFetchError` with a clear message, same shape as `DownloadError` today.
- **Notion** (`_fetch_notion_page`): plain `httpx.get()` of the public share
  URL, then `trafilatura.extract()` to pull the main text content. Notion's
  public pages render enough server-side HTML for this to work without JS
  execution — no browser automation involved. If the fetched page looks like
  a login wall (heuristic: response contains Notion's auth-redirect markers,
  or `trafilatura.extract()` returns empty/near-empty text), raises
  `TextFetchError` explaining the page isn't actually public, rather than
  silently enriching near-nothing.

**New dependency:** `trafilatura` (PyPI, Apache-2.0, actively maintained —
confirmed current release 2.1.0). This is the one new third-party dependency
this design introduces; everything else uses `httpx`, already a dependency.

### `worker.py` changes

`process_item()` branches once, right after the existing `DOWNLOADING` stage
would have started, based on `record.content_kind`:

```python
if record.content_kind == "text":
    record.status = ItemStatus.TRANSCRIBING  # reused; "extracting" is the same slot
    transcript = self.text_fetcher.fetch(record.url, record.content_id)
else:
    # existing Downloader -> Transcriber/ImageDescriber path, unchanged
```

No new `ItemStatus` values — reusing `DOWNLOADING`/`TRANSCRIBING` for the
fetch step keeps `state.json` schema stable and `get_actionable_items()`
untouched.

### Enrichment prompt selection

New `config/prompts/enrich_text_capture.md`, tuned for README/reference
content ("this is a tool/reference doc, not a spoken transcript — summarize
what it does, who it's for, whether it's a skill-worthy tool") instead of
`enrich_transcript.md`'s transcript-shaped framing. Still emits the same
`EnrichmentResult` contract.

`Enricher.__init__` loads both templates; `Enricher.enrich()` picks one based
on `transcript.content_kind` (added to `TranscriptResult`, defaulting to
`"media"` so every existing call site is unaffected).

## Data flow example

1. User shares `https://github.com/owner/some-cli-tool`.
2. Webhook → `QueueManager` → `classify_url_kind()` → `"text"` →
   `StateRecord(content_kind="text", status=PENDING)`.
3. `worker.run_once()` picks it up → `TextFetcher.fetch()` → README +
   metadata as `.text` → `TranscriptResult(content_kind="text", backend="github-api")`.
4. `Enricher.enrich()` uses `enrich_text_capture.md` → `EnrichmentResult`
   (title, summary, tags, tools_mentioned, high_signal, skill_candidate_reason
   — e.g. `high_signal=true` if it's an interesting dev tool).
5. `write_note()` / `SkillWriter.generate()` — unchanged, same as any other
   item.

## Error handling

| Failure | Behavior |
|---|---|
| Private/deleted GitHub repo (404) | `TextFetchError`, item → `failed`, logged to `needs-attention.txt` (same as any other processing failure today) |
| GitHub rate limit exceeded | `TextFetchError` with the rate-limit reset time from response headers, so a retry knows when it'll work |
| Notion page requires login | `TextFetchError`, distinct message ("this Notion page isn't public") so it's clear no amount of retrying will help without the user re-sharing a public link |
| `trafilatura.extract()` returns `None`/empty | Treated the same as a login wall — raised as `TextFetchError`, not silently enriched from empty text |

## Testing

- `tests/test_text_fetcher.py`: fake-`httpx`-response tests (same pattern as
  the fake-`yt_dlp` injection already in `test_downloader.py`) for GitHub
  success/404/rate-limit and Notion success/login-wall cases.
- `tests/test_validators.py`: extend with `classify_url_kind()` cases
  (github.com → text, notion.so → text, youtube.com → media, airtable.com →
  None/unsupported).
- `tests/test_worker_flow.py`: one new case proving a `github.com` URL routes
  through `TextFetcher` and never touches `Downloader`.
- `tests/test_enricher.py` (new or extended): proves `content_kind="text"`
  selects `enrich_text_capture.md`, `"media"` (or unset) selects
  `enrich_transcript.md`.

## Documentation updates

Same pattern as every prior platform addition: `CLAUDE.md` risk-posture
bullet, `README.md` safety posture, `docs/architecture.md` module table +
guardrails section, `docs/runbook.md` new "GitHub and Notion text capture"
section (no cookies needed — public-only), `docs/acceptance-tests.md`
checklist items, `.env.example` (no new secrets needed for v1, but note the
absence explicitly so it's clear this was a deliberate choice, not an
oversight).
