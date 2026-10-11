# Clip Machine

Python 3.11 CLI that turns a long video into short vertical clips. Pipeline of six
stages (fetch, transcribe, candidates, score, render, report); each reads and
writes files in a run folder and can be restarted. Currently a walking skeleton:
`fetch` is real; the other stages are stubs.

## Read first
- `docs/brief-04-candidates.md` (current brief), `docs/clip-machine-plan.md` sections 4 and 6.
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
- Unit tests must not need ffmpeg; ffmpeg tests are marked `ffmpeg` and skipped when it's missing.
  No test needs network or API keys.
- Never create, change or delete anything outside the repository folder. Build paths from the
  repo root as absolute paths. If a `cd` or path check fails, stop and report; never fall back
  to creating folders.
- Stage outputs go only inside the current run folder.
- Scratch and backup files go only in `<repo>/runs/_scratch/` (git-ignored), never %TEMP% or any
  folder outside the repo.
- Always run Python through `uv run`, never bare `python`.
- One brief at a time; small commits; out-of-scope ideas go to `docs/PROJECT_LOG.md`.

## Secrets
- The API key lives only in `.env` as `CLIPMACHINE_ANTHROPIC_API_KEY`. Never export `ANTHROPIC_API_KEY`.
- Deny rules in `.claude/settings.json` block Claude's file tools from `.env`, but they
  do NOT stop a script or command from opening `.env` itself.
- Never write code or commands that print, log or echo secrets or environment variables.

## Environment
- Native Windows (D-007), no command sandbox. Mode is acceptEdits; shell commands outside the
  allow-list in `.claude/settings.json` still need Igor's approval.
- Use the Bash tool, not PowerShell, for shell commands: the Bash allow/ask/deny rules do not apply to PowerShell.
- Never push or add a remote unless asked.
