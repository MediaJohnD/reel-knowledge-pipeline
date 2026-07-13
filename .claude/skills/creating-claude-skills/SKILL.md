---
name: creating-claude-skills
description: Use when changing config/prompts/create_skill.md, src/reel_pipeline/skill_writer.py, or the format of generated SKILL.md artifacts from ingested content.
---

# Creating Claude Skills From Ingested Content

Governs how `skill_writer.py` turns high-signal transcripts into standalone
`SKILL.md` artifacts under `data/generated_skills/` - distinct from this
repo's own meta-skills under `.claude/skills/`.

## When to use this

- Changing `config/prompts/create_skill.md`.
- Changing the skill-generation trigger condition in `should_generate_skill()`.
- Changing where/how generated skill files are named or stored.

## The generation rule (do not weaken silently)

A skill artifact is generated **only** when both are true:
1. `EnrichmentResult.high_signal` is `true`.
2. `EnrichmentResult.skill_candidate_reason` is non-empty.

This is enforced in `should_generate_skill()` in `skill_writer.py` - if you
need to change the rule, change it there (and in
`config/prompts/enrich_transcript.md`'s instructions for `high_signal`), not
by adding a second check somewhere else.

## Required SKILL.md shape

Generated files must have:
- YAML frontmatter with `name` (kebab-case) and `description` (one sentence, written so another agent can judge relevance from it alone).
- A body with: framing, "When to use this", numbered "Steps", and optional "Notes / gotchas".

This mirrors the shape real Claude Code skills use (see this repo's own
`.claude/skills/*/SKILL.md` files for reference examples) - keep them
consistent so generated skills are actually usable by an agent later.

## Steps to change the generation logic

1. Edit the prompt in `config/prompts/create_skill.md` if the issue is
   content quality; edit `skill_writer.py` if it's the trigger condition or
   file placement.
2. Keep the output path deterministic: `skill_path(settings, title)` ==
   `settings.skills_dir / slugify(title) / "SKILL.md"`.
3. Update `tests/test_skill_writer.py` to cover the new behavior, including
   a case that asserts the Claude API is *not* called when the trigger
   condition isn't met (skill generation must stay cheap by default).
4. Update `docs/architecture.md` and `CLAUDE.md` if the contract changes.

## Notes / gotchas

- Never point generated skills at `.claude/skills/` - that directory is
  reserved for this repository's own authoring skills, listed explicitly in
  the required repo layout.
- Don't fabricate steps not grounded in the transcript; the prompt already
  instructs this, but review generated output if you change the prompt.
