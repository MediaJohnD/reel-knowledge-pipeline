---
name: writing-obsidian-notes
description: Use when changing the Obsidian note format, frontmatter fields, or filename scheme produced by src/reel_pipeline/obsidian_writer.py.
---

# Writing Obsidian Notes

Defines the conventions `obsidian_writer.py` must follow so generated notes
stay consistent, idempotent, and Obsidian-compatible.

## When to use this

- Adding or renaming a frontmatter field.
- Changing the note body layout (sections, ordering).
- Changing the filename/slug scheme.
- Debugging why a note wasn't created or was duplicated.

## Required frontmatter fields (the output contract)

Every note's YAML frontmatter must include exactly these keys, sourced from
`ContentItem`/`EnrichmentResult` - do not drop or rename any of them without
also updating `docs/architecture.md`, `CLAUDE.md`, and
`tests/test_obsidian_writer.py`:

- `title`
- `source_url`
- `content_id`
- `created_at` (ISO 8601)
- `tags` (list)
- `tools_mentioned` (list)
- `high_signal` (bool)

## Required body sections

In order: `# <title>` heading, `## Summary`, `## Key Takeaways` (bulleted),
`## Tools Mentioned` (bulleted), optionally `## Skill Candidate` (only when
`high_signal` and a reason exist), then `## Transcript` with the full text.

## Steps to change the format safely

1. Edit `_render_frontmatter` / `_render_body` in `obsidian_writer.py`.
2. Keep filenames deterministic: `note_filename(content_id, title)` must
   return the same string every time for the same inputs - it's what makes
   re-running the pipeline overwrite instead of duplicate.
3. Update `tests/test_obsidian_writer.py` to assert the new shape.
4. Update the "Output contracts" section in `CLAUDE.md` and
   `docs/architecture.md` to match.

## Notes / gotchas

- Use `yaml.safe_dump` for frontmatter, never manual string concatenation -
  titles/summaries can contain colons, quotes, or newlines that break naive YAML.
- `slugify()` truncates to 60 chars and strips non-alphanumerics - don't
  assume the slug matches the title exactly when writing tests or docs.
- Notes are written under `settings.vault_dir` (`REEL_VAULT_DIR` override) -
  never hardcode `data/notes` in new code, always go through `Settings`.
