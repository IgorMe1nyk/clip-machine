# Clip Machine

Python 3.11 CLI that turns a long video into short vertical clips. Pipeline of six
stages (fetch, transcribe, candidates, score, render, report); each reads and
writes files in a run folder and can be restarted. Currently a walking skeleton:
stages are stubs.

## Read first
- `docs/brief-01-skeleton.md` (current brief), `docs/clip-machine-plan.md` sections 4 and 6.
- Where the plan conflicts with `docs/MVP.md`, `COSTS.md`, `DECISIONS.md` or the brief, those win.

## Commands (run from the repo root)
- `uv sync --locked`
- `uv run python -m clipmachine run <file> --client example-client`
- `uv run ruff check . && uv run ruff format --check .`
- `uv run mypy src`
- `uv run pytest -q`

## Rules
- Use `uv` only. Never hand-type dependency versions; `uv add` and commit `uv.lock`.
- Add no dependency that the current brief does not list. Ask first.
- Subprocesses take argument lists, never shell strings.
- Type-annotate everything in `src/` (mypy `disallow_untyped_defs`).
- Tests must not need ffmpeg, network or API keys.
- One brief at a time; small commits; out-of-scope ideas go to `docs/PROJECT_LOG.md`.

## Secrets
- The API key lives only in `.env` as `CLIPMACHINE_ANTHROPIC_API_KEY`. Never export `ANTHROPIC_API_KEY`.
- Deny rules in `.claude/settings.json` block Claude's file tools from `.env`, but they
  do NOT stop a script or command from opening `.env` itself.
- Never write code or commands that print, log or echo secrets or environment variables.

## Environment
- Native Windows (D-007), Manual permission mode, no command sandbox: every command is approved by Igor.
- Never push or add a remote unless asked.
