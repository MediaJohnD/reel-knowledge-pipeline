# Notion API Text Capture (`NotionFetcher`) — Design

Status: implemented 2026-08-10.

## Purpose

The catch-all text-capture policy (see `CLAUDE.md`'s 2026-08-10 entry) routes
any non-media URL to `GenericHtmlFetcher` (plain HTTP GET + `trafilatura`).
That fails on Notion unconditionally: direct inspection of the raw HTML for
both a fully public `notion.site` page and a private `app.notion.com` share
link showed Notion's current frontend ships **zero page content server-side,
for any page** - just a `<noscript>JavaScript must be enabled</noscript>`
shell. No domain-list change fixes that; it's an architectural fact about how
Notion renders, not an access-control gap.

Two alternatives were evaluated and rejected before landing on this design:

- **crawl4ai** (a connected MCP tool, headless-browser-backed): tested live
  against the two real failing URLs. One attempt returned Notion's own
  "Your browser is not compatible with Notion" bot-detection wall; a retry
  with a spoofed desktop user-agent got blocked by crawl4ai's own SSRF
  protection instead. Never got real content either way.
- **A local Playwright fallback** (see the shelved
  `2026-08-10-js-rendered-text-capture-design.md`): tested live via an
  already-connected Playwright MCP tool and it *did* work - full page content
  rendered, no bot wall. But building this into the pipeline would have
  required narrowing `CLAUDE.md`'s absolute "no browser automation, ever"
  guardrail, a real and permanent change to the project's threat model.

Before committing to that guardrail change, GitHub research turned up a
better option: **Notion's own internal JSON API** - the same API endpoint
Notion's web client itself calls to fetch page data, reverse-engineered and
documented by the open-source `react-notion-x`/`notion-client` project. This
needs no browser at all: a plain HTTP POST, same trust/risk profile as
`GitHubFetcher` already calling GitHub's REST API. That's what got built.

## API contract (confirmed by live testing against a real page, not just
documentation)

- `POST https://app.notion.com/api/v3/loadPageChunk`
- Headers: `Content-Type: application/json` (no auth for public pages)
- Body: `{"pageId": "<dashed-uuid>", "limit": 100, "chunkNumber": 0, "cursor": {"stack": []}, "verticalColumns": false}`
- Success: `{"recordMap": {"block": {"<block-uuid>": {"value": {"value": {"id", "type", "properties": {"title": [...]}, "content": [...]}}}, ...}}}`
  - Double-nested `value.value` - a real API shape change Notion made at some
    point (see `react-notion-x` issue #682); unwrapped generically by
    descending through however many `"value"` layers are present rather than
    assuming exactly one.
  - `properties.title` is a "rich text" array of `[text, formatting]` pairs;
    plain text = concatenation of each pair's `text`, dropping mention
    placeholder characters (`‣`, `⁍`) since the mention's real target isn't
    in that slot at all.
- Failure (verified with an all-zeros page id): **HTTP 200**, not 404 -
  `{"cursor": {...}, "recordMap": {"__version__": 3}}`, i.e. no `"block"` key
  at all. Detected as "not found or not public" by checking the requested
  page id is actually a key in the returned block map, not by status code.
- Page id extraction: regex for a bare-or-dashed 32-hex-char run anywhere in
  the URL path (every real share link ends in `...Some-Title-<id>`).
  Confirmed working for `app.notion.com/p/<slug>-<id>` URLs live. **Not**
  solved: a bare custom-subdomain root with no id in the path at all (e.g.
  `https://someworkspace.notion.site/` pointing at that workspace's home) -
  raises a clear `TextFetchError` rather than guessing.

## Algorithm

1. Extract page id from the URL. No id found -> `TextFetchError`.
2. `POST loadPageChunk`.
3. Requested page id missing from the response's block map -> `TextFetchError`
   ("not found or not public").
4. Unwrap the root block, take its `properties.title` as the page title.
5. Depth-first walk the root's `content` (ordered child block ids), for each
   child: unwrap, append its title text if present and not a filename-only
   block type (`image`/`file`/`video`/`audio`/`pdf` store a filename in
   `title`, not prose - skipped as noise), note-but-don't-expand
   `collection_view`/`collection_view_page` blocks (linked databases - row
   data needs a separate collection-query API call this version doesn't
   make), then recurse into that child's own `content` before the next
   sibling. A `visited` set guards against a cyclic content reference
   (shouldn't happen in real Notion data, but cheap insurance).
6. Join into `"# {title}\n{block text, one per line}"`.

## Known v1 limitations (accepted, not solved now)

- No pagination: only `chunkNumber: 0`'s first 100 blocks. A very large page
  may be missing tail content.
- Linked database rows aren't fetched, just noted by title.
- Bare custom-subdomain root URLs with no page id in the path fail cleanly
  rather than being resolved.

## Testing

`tests/test_text_fetcher.py`: `respx`-mocked `loadPageChunk` responses
covering title + ordered block-text extraction, double-nested value
unwrapping, image-block filename filtering, linked-database noting,
cyclic-reference safety, the "not found" 200-with-no-block-key shape, and the
no-id-in-url error path. `DispatchingTextFetcher` routing test updated to
route `notion.so`/`notion.site`/`notion.com` to `NotionFetcher`, not the
generic HTML fetcher.

## What this does *not* change

`CLAUDE.md`'s "no browser automation, ever" guardrail is untouched - this
design was chosen specifically because it doesn't need that guardrail
narrowed. The shelved Playwright design remains the fallback answer if a
*different* JS-only site (one without an equivalent internal API) becomes a
recurring problem.
