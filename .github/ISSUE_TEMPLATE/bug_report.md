---
name: Bug report
about: Something isn't working as expected
title: ""
labels: bug
---

**What happened**

A clear description of the bug.

**Command run**

```bash
uv run python -m reel_pipeline.cli run-once
```

**Log output**

Paste the relevant structured log line(s) from `data/logs/pipeline.log`, or
the `error` field from the item's `state.json` record. Redact any private
URLs.

**Environment**

- OS:
- Python version (`python --version`):
- `uv --version`:
- Relevant `config/settings.yaml` values (transcription backend, LLM provider):

**Expected behavior**

What you expected to happen instead.
