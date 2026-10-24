# Running your own copy

This repo is one person's working setup, published so others can run their own
copy. Your copy is fully separate: your own keys, your own notes folder, your own
machine. Nothing you run talks to the owner's machine, vault, or accounts.

## 1. Install

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/) and `ffmpeg` (yt-dlp uses it
to pull audio: `winget install ffmpeg`, `brew install ffmpeg` or
`apt install ffmpeg`).

```bash
git clone https://github.com/MediaJohnD/reel-knowledge-pipeline.git
cd reel-knowledge-pipeline
uv sync --extra local-whisper
uv run playwright install chromium
cp .env.example .env
```

## 2. Fill in `.env`

Only three things are needed to start:

| Variable | What to put |
| --- | --- |
| `REEL_WEBHOOK_SECRET` | Any long random string you make up. Only matters if you run the webhook. |
| `GROQ_API_KEY` | A free key from [console.groq.com](https://console.groq.com). Enrichment uses Groq first. |
| `REEL_VAULT_DIR` | Full path to your Obsidian vault (or a subfolder of it). Leave unset to write to `data/notes/`. |

`GEMINI_API_KEY`, `OPENROUTER_API_KEY` and `MISTRAL_API_KEY` are optional free
fallbacks; the pipeline tries the next one when a key is missing or rate-limited.
Leave any key you don't have blank rather than keeping the placeholder.

`--extra local-whisper` transcribes on your own CPU (no GPU needed). To use
OpenAI's hosted Whisper instead, skip the extra and set
`REEL_TRANSCRIPTION_BACKEND=openai` plus `OPENAI_API_KEY`.

**If you use Ollama**, add `REEL_OLLAMA_HOST=http://127.0.0.1:11434` (Ollama's
normal port; this repo's default, 11435, is specific to the owner's machine).
The Ollama model tags in `config/settings.yaml` (`gpt-oss:20b-64k`,
`gemma4:12b`) are the owner's; pull your own models and edit those names, or
skip Ollama and let the hosted keys do the work.

## 3. Run it

```bash
echo "https://www.youtube.com/watch?v=dQw4w9WgXcQ" >> data/inbox/queue.txt
uv run python -m reel_pipeline.cli run-once
```

A note appears in your vault folder. Add URLs to `data/inbox/queue.txt` and run
`run-once` again whenever you want.

## Parts you can ignore

These are wired to the owner's own accounts and machine. They do nothing unless
you set them up yourself:

- `scripts/review_new_reels.py`, `config/prompts/review_reel.md`, `config/have.txt`:
  the owner's personal reel-review step (their businesses, their gear list).
- `comment_gate.py` / `cli comment-queue`: needs a hand-logged-in Instagram
  browser profile (`REEL_IG_BROWSER_PROFILE`). Off unless you configure it.
- `run-pipeline.bat`, `run-review.bat`, `organize-vault.bat`,
  `scripts/start_*_hidden.vbs`: Windows Task Scheduler launchers. They work from
  any clone location, but you only need them if you want scheduled runs.
- Instagram, Facebook and LinkedIn need your own browser cookies
  (`REEL_YTDLP_COOKIES_BROWSER` in `.env`); see [runbook.md](runbook.md).
  YouTube, TikTok, X and plain web pages work without any.

## Keep it private

- Never commit `.env`. It is already in `.gitignore`.
- The webhook binds to `127.0.0.1` by default, so only your own machine can reach
  it. Change `REEL_WEBHOOK_HOST` only if you know you want other devices in.
- To get updates: `git pull`. Your `.env`, `data/` and vault are untouched by it.
