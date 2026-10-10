# Brief 1 — Walking skeleton (approved)

> Status: APPROVED by Igor 2026-10-10, including the step 2 addition (native Windows, Manual permission mode, D-007).
> Replaces section 15 of clip-machine-plan.md.

## Goal
An installable Python package with a working CLI, validated config, run folders, a restartable pipeline of six **stub** stages, tests, linting, type checks, CI, and Claude Code guardrails. No video, no transcription, no AI calls yet.

## Read first
1. `docs/clip-machine-plan.md` — sections 4 (pipeline), 6 (structure, example configs), 10 (quality checklist, context only). Also `docs/MVP.md`, `docs/COSTS.md` and `docs/DECISIONS.md`.
2. `CLAUDE.md` (you will create it in step 1; keep it under ~40 lines).
3. https://code.claude.com/docs/en/permissions.md and https://code.claude.com/docs/en/hooks-guide.md before writing `.claude/settings.json`.

Work in plan mode first. Show me the plan, wait for my OK, then implement in small commits.

## Decisions already made (do not change; ask if you think one is wrong)
- **Tooling:** `uv` for the virtual env, dependency resolution and the lock file (`uv.lock`, committed) (D-001).
- **Python:** 3.11 (`requires-python = ">=3.11,<3.12"`). Reason: video/ML libraries we add later ship wheels for 3.11 reliably.
- **Layout:** `src/clipmachine/` with `pyproject.toml` (hatchling build backend) and an editable install, so `python -m clipmachine` works.
- **CLI:** standard-library `argparse`. No extra CLI framework (D-002).
- **Runtime dependencies in this brief: `pydantic`, `pyyaml` only.** Do NOT add `anthropic`, `faster-whisper`, `yt-dlp` or `python-dotenv` yet; each arrives with the brief that uses it.
- **Dev dependencies:** `pytest`, `ruff`, `mypy`, `types-PyYAML`, and a dependency-audit tool (see step 9).
- Versions: let `uv` resolve current releases; the committed `uv.lock` is the pin. Do not hand-type version numbers from memory.

## Steps
1. **Repo hygiene.** `README.md`, `CLAUDE.md`, `.gitignore` (ignores `.env`, `.env.*` except `.env.example`, `runs/`, `*.mp4 *.mov *.mkv *.wav *.m4a *.mp3`, `.venv/`, caches), `.env.example` containing only `CLIPMACHINE_ANTHROPIC_API_KEY=` (deliberately NOT named `ANTHROPIC_API_KEY`, so it can never be picked up by Claude Code as a billing credential if someone exports it).
2. **Claude Code guardrails** in `.claude/settings.json` (committed). Context: this repo runs on **native Windows** (D-007), where Claude Code's command sandbox is not available, so permission prompts are the main safety layer.
   - `permissions.defaultMode`: `"default"` (Manual — Igor approves each command). `permissions.disableBypassPermissionsMode`: `"disable"`. Verify both key names against the permissions and settings docs.
   - `permissions.deny`: `Read(./.env)`, `Read(./.env.*)` — but allow `.env.example` via a `Read(!.env.example)` negation placed after them. Verify against the permissions docs.
   - `permissions.ask`: `Bash(git push *)`.
   - One `PostToolUse` hook that runs `ruff format` and `ruff check --fix` on edited `.py` files. Show me the hook JSON in your plan before writing it.
   - Note in CLAUDE.md that deny rules don't stop a script from opening `.env` itself; never write code or commands that print secrets.
3. **Folders** exactly as plan section 6: `config/`, `clients/example-client/`, `prompts/`, `src/clipmachine/{stages,utils}/`, `runs/` (with a `.gitkeep`), `tests/`, plus `docs/`. Igor will place the approved `docs/clip-machine-plan.md`, `docs/COSTS.md`, `docs/MVP.md`, `docs/DECISIONS.md` and this brief in `docs/` before you start; commit them unchanged in the first commit. Also create `docs/PROJECT_LOG.md` and `docs/EVAL.md` with just a title and, for EVAL.md, the table header from MVP.md section 6.
4. **Config** (`src/clipmachine/config.py`, `models.py`):
   - `config/default.yaml` and `clients/example-client/rules.yaml` as in the plan.
   - Pydantic models with `extra="forbid"` so typos fail loudly.
   - Validation: `min_seconds < max_seconds`, `keep_top <= candidates_target`, positive sizes, hex colour format.
   - Merge rule: client `clip_length.min/max` override `clip.min_seconds/max_seconds`. Everything else in `rules.yaml` is kept as a `ClientRules` object for the scoring stage later.
   - Client names must match `^[a-z0-9][a-z0-9-]{0,40}$` and an existing `clients/<name>/` folder.
5. **Run folders.** `runs/<YYYY-MM-DD>_<slug>/`. Slug = input file stem, lowercased, non `[a-z0-9]` → `-`, collapsed, max 50 chars, never empty (fallback `episode`). Collision → suffix `-2`, `-3`… Must stay inside `runs/` (reject anything that resolves elsewhere). Save the resolved config as `config.resolved.yaml` in the run folder.
6. **Input validation (light).** Input must be an existing regular file with an extension in an allow-list (`.mp4 .mov .mkv .m4a .wav .mp3`). Full checks (size, duration, real container type) come in Brief 2.
7. **Pipeline** (`pipeline.py`):
   - Stages in order: `fetch, transcribe, candidates, score, render, report`. Each is a stub that logs `stage <name> not implemented yet` and returns.
   - `run.json`: `schema_version`, `input`, `client`, `created_at`, and per stage `status` (`pending|running|done|failed`), `started_at`, `finished_at`, `error`.
   - Write `run.json` atomically (temp file in the same folder, then `os.replace`) — matters on Windows.
   - Skip stages already `done`. A stage left `running` (crash) counts as `failed` on the next start.
   - `--from-stage X` resets X and every later stage to `pending`, then runs.
   - On failure: mark `failed` with the error message, stop, exit code 1, print a one-line human message.
8. **CLI** (`cli.py`, `__main__.py`):
   - `run <input-file> --client <name>` → new run.
   - `run <run-folder> [--from-stage <stage>]` → resume (detected by the folder containing `run.json`).
   - `report <run-folder>` → stub.
   - Startup check that `ffmpeg` and `ffprobe` are on PATH (`shutil.which`), done in a function the pipeline calls so tests can stub it. If missing, print short Windows install guidance and exit 2.
   - Never build shell strings; any future subprocess call uses argument lists.
9. **Quality tooling.** ruff (lint + format), mypy on `src/` (strict-ish: `disallow_untyped_defs`), pytest. Add a dependency audit (`pip-audit` or equivalent); **check it actually works inside a uv-managed env** and tell me which you used.
10. **Tests** (no ffmpeg needed): config loading and every validation rule; slug + collision; path-escape rejection; run.json atomic write; skip-when-done; `--from-stage`; crashed `running` → `failed`; a forced stub failure stops the pipeline with exit 1.
11. **CI:** GitHub Actions, `ubuntu-latest` only for now: `uv sync --locked`, ruff, mypy, pytest, audit.
12. **README:** what it is, setup on Windows from a clean machine (Python via uv, ffmpeg on PATH), how to run, how to run checks.

## Out of scope
Pushing to GitHub or adding a remote (Igor does that later; the CI file is written now and runs on first push); any ffmpeg call beyond the PATH check; Whisper; any Claude API call or the `anthropic` package; yt-dlp/URLs; the HTML report; face tracking; Docker; any web UI; any paid service. If you think something here is needed, stop and ask.

## Acceptance checks (run all, paste output)
```
uv sync --locked
uv run python -m clipmachine --help
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
<audit command you chose>
git check-ignore .env           # must print .env
```
**End-to-end check:** create an empty `tests/fixtures/sample.mp4`, then:
1. `uv run python -m clipmachine run tests/fixtures/sample.mp4 --client example-client` → new `runs/<date>_sample/` with `run.json` (all six `done`) and `config.resolved.yaml`.
2. Same command on the run folder → "nothing to do", no stage reruns.
3. `--from-stage render` → only `render` and `report` rerun (timestamps prove it).
4. `--client ../evil` → rejected with a clear message, nothing created.

## Report back
- Output of every acceptance command (not "done").
- Commit list with a one-line diff summary each.
- File tree of what you created.
- Any deviation from this brief and why; open questions.
- What CLAUDE.md now says (paste it).
