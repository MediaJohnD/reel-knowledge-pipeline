## What this changes and why

## Checklist

- [ ] `make check` passes locally (`uv lock --check`, lint, typecheck, test, `uv audit`)
- [ ] Tests added/updated for any behavior change (fakes only - no real network calls)
- [ ] If this touches ingestion/downloading/credentials, I've reviewed `CLAUDE.md`'s
      "Risk posture" and the safety checklist in `.claude/skills/reviewing-pipeline-safety/`
- [ ] Docs updated (`README.md`, `docs/architecture.md`, or `docs/runbook.md`) if behavior
      or configuration changed
- [ ] No hardcoded secrets, cookies, or personal absolute paths in the diff
