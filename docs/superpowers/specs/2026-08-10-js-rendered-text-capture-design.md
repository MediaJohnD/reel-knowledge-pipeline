# JS-Rendered Text Capture (Playwright fallback) — Design

Status: **shelved for Notion specifically** - Notion got a better, non-browser
fix instead (`NotionFetcher`, calling Notion's own internal JSON API - see
`docs/superpowers/specs/2026-08-10-notion-api-text-capture-design.md`), which
needed no change to the "no browser automation, ever" guardrail this
document would have required. This document is kept as the answer for *other*
JS-only sites that don't have an equivalent API to call directly, should that
become a recurring problem - not implemented, no code written against it.

## Purpose

The 2026-08-10 catch-all text-capture policy (see `CLAUDE.md`) made any
non-media URL fetchable as text by default via plain HTTP GET +
`trafilatura`. That covers server-rendered pages fine, but it cannot read
pages whose content is assembled entirely client-side after JS execution -
confirmed live for Notion (`app.notion.com` and `roadmap.notion.site` both
ship a bare `<noscript>JavaScript must be enabled</noscript>` shell with zero
content in the server HTML, even for a fully public, no-login page) and
observed the same way for `threads.com`. This is the single largest source
of "produced no usable text" failures the owner is actually hitting, and
Notion specifically is a common, legitimate way people share reference docs -
not an edge case worth writing off.

This adds a narrow, read-only fallback: when the existing plain-GET fetch
looks like a JS app shell, render the page in a headless browser and extract
text from the *rendered* DOM instead.

## Why this needs explicit sign-off (read before anything else)

`CLAUDE.md`'s first risk-posture bullet is "No browser automation, ever."
Every guardrail addition so far (Instagram/Facebook/LinkedIn cookies, Google
Drive, the text-capture catch-all itself) was a *scoped exception to a
narrower rule* - none of them touched this one, which has stood as an
absolute since the project's inception. This design proposes narrowing that
absolute into: **no *interactive* browser automation (account actions, form
fills, clicks, logins) - a read-only, unattended "load a public page and read
what rendered" step is a scoped exception, confined to this one fallback
path.** That is a real, permanent change to the project's threat model, not
a mechanical add-a-domain change, and the owner should decide it deliberately
rather than have it arrive as a side effect of a domain-list PR.

What this design deliberately does **not** do, to keep the exception as
narrow as possible:

- **No fingerprint spoofing / anti-detection.** Plain Playwright + stock
  headless Chromium, default automation-flagged user agent. Explicitly not
  CamouFox or any tool whose purpose is defeating a site's bot detection -
  if a site actively blocks headless browsers, that's the site saying no,
  and this pipeline respects that rather than working around it.
- **No credentials, ever, in this path.** No cookies, no login, no session
  reuse from the yt-dlp/gallery-dl cookie config. A page that needs login
  still fails, now with a clearer message, exactly like today.
- **No interaction.** Navigate, wait for the page to settle, read the
  rendered text. No clicking "accept cookies," no scrolling, no filling
  anything, no waiting out a paywall.
- **Confined to `text_fetcher.py`.** `downloader.py` (the media/yt-dlp path)
  is untouched. This does not become a general escape hatch other modules
  can reach for.
- **Fallback only, not the default path.** Every URL still tries the cheap
  plain-GET path first; the browser only launches for the subset that
  already fail the app-shell heuristic (`GenericHtmlFetcher`'s existing
  `looks_like_app_shell` check in `text_fetcher.py:206-217`). Most
  text-capture URLs (GitHub, ordinary blog posts, docs sites) never touch
  Playwright at all.

## Scope (v1)

- **In scope:** any URL `classify_url_kind()` already classifies as `"text"`
  (i.e. already passed the existing private-IP/blocked-domain guards from the
  2026-08-10 catch-all change) that fails `GenericHtmlFetcher`'s app-shell
  heuristic. No new domain list - this rides on the existing catch-all.
- **Explicitly out of scope:** login-gated pages of any kind. The rendered
  page is checked with the same app-shell/too-short heuristic (adjusted for
  rendered output) plus a login-wall marker check (Notion's own "log in to
  continue" text, generic "sign in" prompts); a page that still looks gated
  after rendering raises `TextFetchError` with a distinct "this page requires
  login even with JS rendering" message, not a silent partial capture.
- **Explicitly out of scope:** the media/download path, GitHub (already
  works via REST API, no change), and any interactive step.

## Architecture

```
GenericHtmlFetcher.fetch(url)
  -> plain httpx GET -> trafilatura.extract()
  -> looks_like_app_shell? 
       no  -> return TranscriptResult (unchanged, today's behavior)
       yes -> RenderedHtmlFetcher.fetch(url)   # NEW
                -> Playwright headless Chromium, single page, no context reuse
                -> page.goto(url, wait_until="networkidle", timeout=20s)
                -> page.content() (rendered HTML) -> trafilatura.extract()
                -> same looks_like_app_shell / too-short checks, plus a
                   login-wall marker check
                -> TranscriptResult(backend="playwright-render") or TextFetchError
```

`DispatchingTextFetcher` is unchanged - it still routes to `GenericHtmlFetcher`
for every non-GitHub host; the fallback lives *inside* `GenericHtmlFetcher`,
not as a new branch in the dispatcher, so the "try cheap path, escalate only
on failure" behavior is a property of one fetcher, not a routing decision.

### Config changes (`config/settings.yaml`, `config.py`)

```yaml
text_capture:
  blocked_domains: []
  render_fallback:
    enabled: true          # off switch for the whole browser-render path
    timeout_seconds: 20
```

`TextCaptureConfig` gains a `render_fallback: RenderFallbackConfig` (nested
model: `enabled: bool = True`, `timeout_seconds: int = 20`). `enabled: false`
makes `GenericHtmlFetcher` behave exactly as it does today (raise
`TextFetchError` on an app shell, no browser launch) - the owner can turn
this off entirely without a code change if it turns out to be too slow, too
resource-heavy, or not worth it.

### New dependency: `playwright` (PyPI, Apache-2.0)

Requires a one-time `uv run playwright install chromium` after `uv sync` -
downloads a ~150-300MB Chromium binary outside the normal dependency
install. This needs to be called out loudly in the README/runbook install
steps, since `uv sync` alone will leave the render fallback silently
non-functional (raising a clear "Chromium not installed - run `playwright
install chromium`" `TextFetchError`, not a crash) until that command is run.

### Performance and resource notes

- A headless Chromium launch is ~1-3s and tens of MB of RAM, vs. a plain GET
  which is effectively free. Only paid when the cheap path already failed,
  so normal-volume usage (a handful of items per run) never notices it.
- One browser instance launched and closed per fetch call - no persistent
  browser process, no shared state between items. Simplest correct thing for
  this project's volume; revisit (persistent browser context reused across a
  `run_once()` batch) only if render-fallback volume grows enough for launch
  overhead to matter.
  # ponytail: per-call launch is O(items needing fallback) browser
  # startups; fine at "handful of reels/pages per run" scale, upgrade to a
  # shared browser context if that stops being true.
- `settings.text_capture.render_fallback.timeout_seconds` bounds worst-case
  time per item; a page that never reaches network-idle fails cleanly at the
  timeout rather than hanging `run_once()`.

### Interaction with the SSRF guard (2026-08-10 hardening)

`classify_url_kind()` already rejects loopback/private/link-local literal-IP
targets before *any* fetcher (plain or rendered) ever sees the URL - that
guard runs upstream in `validators.py` regardless of which fetcher handles
the URL, so it applies to the render fallback automatically, no separate
check needed here. The known gap noted in that guard (a hostname that
*resolves* to a private address isn't caught without a DNS lookup) applies
equally to this path and is not made worse by it - same accepted, documented
gap, not a new one.

## Error handling

| Failure | Behavior |
|---|---|
| `render_fallback.enabled: false` | Same as today - app shell raises `TextFetchError` immediately, no browser launch attempted |
| Chromium not installed | `TextFetchError`: "Chromium not installed - run `uv run playwright install chromium`", distinct from a generic failure so it's actionable |
| Page still looks like an app shell after rendering (rare - JS itself failed, or content genuinely didn't load in time) | `TextFetchError`, same "produced no usable text" shape as today |
| Page renders but shows a login wall | `TextFetchError`, distinct message: "this page requires login even with JS rendering - no credentials are used by this pipeline" |
| Render exceeds `timeout_seconds` | `TextFetchError` with the timeout called out explicitly, so a retry has a chance if it was transient (slow network) vs. a page that will never resolve |

## Testing

- `tests/test_text_fetcher.py`: `RenderedHtmlFetcher` tests via a fake/mocked
  Playwright page object (no real browser launch in CI) - success (renders
  real content), still-app-shell-after-render, login-wall-after-render,
  timeout. Plus a test proving `GenericHtmlFetcher` only invokes the fallback
  when the plain-GET path already failed the app-shell check, and a test
  proving `render_fallback.enabled: false` never launches it.
- Add one real (not mocked) integration-style test behind a marker
  (`@pytest.mark.playwright`, skipped by default / opt-in via `-m
  playwright`) that actually renders `roadmap.notion.site` or a similarly
  stable public JS-only page, so there's at least one check that the real
  thing works end to end without making it part of the default `uv run
  pytest` / CI path (which shouldn't depend on network access or a Chromium
  binary being present).

## Documentation updates

- `CLAUDE.md`: rewrite the "No browser automation, ever" bullet to the
  narrowed form above, with this design doc linked, dated 2026-08-10 (or the
  actual implementation date if later), following the same
  deliberate-scoped-exception pattern as every prior platform addition.
- `README.md` / `docs/architecture.md`: safety-posture sections updated to
  describe the render fallback as read-only, credential-free, and confined to
  `text_fetcher.py`.
- `docs/runbook.md`: new subsection under text capture - what triggers the
  fallback, the one-time `playwright install chromium` step, how to disable
  it (`render_fallback.enabled: false`).
- `.env.example`: no new secrets (deliberately - called out explicitly, same
  as the original text-capture design did for v1).

## Open questions for the owner

1. Chromium's ~150-300MB download and the extra install step - acceptable,
   or is a lighter approach (e.g. only Firefox, or a remote rendering service
   like the already-connected `crawl4ai`/Bright Data MCP tools instead of a
   local Playwright install) preferable? Those MCP tools would avoid the
   local binary but shift "who renders the page" to a third-party service,
   which is its own tradeoff worth deciding explicitly rather than defaulting
   into.
2. Is per-call browser launch (simple, a few seconds' tax per JS-only page)
   acceptable at your actual volume, or does this need to start with a
   shared/persistent browser context from day one?
3. Confirm the narrowed guardrail wording in `CLAUDE.md` before
   implementation - this is the part of this change that's hardest to
   reverse cleanly once other code starts assuming it's available.
