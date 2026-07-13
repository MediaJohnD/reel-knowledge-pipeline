You are writing a reusable Claude/Codex skill artifact (a `SKILL.md` file)
distilled from a single piece of source content. This is only invoked for
content already judged high-signal - your job is to turn its reusable
workflow into a standalone, actionable skill.

Title: {{title}}
Summary: {{summary}}
Why this is skill-worthy: {{skill_candidate_reason}}
Key takeaways:
{{key_takeaways}}

Source transcript (for grounding - do not quote it at length):
"""
{{transcript}}
"""

Respond with **only** the raw contents of a `SKILL.md` file (no markdown
fences, no commentary before or after). It must follow this exact shape:

```
---
name: <short-kebab-case-skill-name>
description: <one sentence: when to use this skill, written so another agent
  can decide relevance from this line alone>
---

# <Human-readable skill title>

<1-2 sentence framing of what this skill helps you do.>

## When to use this

<Bullet list of concrete trigger situations.>

## Steps

<Numbered, concrete, actionable steps derived from the source content's
reusable workflow. Write these as instructions to an agent or person
following the skill, not as a summary of the video.>

## Notes / gotchas

<Any caveats, edge cases, or things that commonly go wrong, if the source
material mentions them. Omit this section if there are none.>
```

Rules:
- The skill name must be a short, descriptive kebab-case slug (no spaces, no source-specific IDs).
- Write the steps as reusable instructions, not as "in this video, the creator...".
- Do not fabricate steps that aren't grounded in the transcript or key takeaways.
- Keep the whole file under ~400 words.
