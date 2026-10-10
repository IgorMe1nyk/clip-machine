# Clip Machine

Turns a long video (podcast, interview, YouTube episode) into short vertical clips
for Shorts, Reels and TikTok. It transcribes the video, finds candidate moments,
scores them with Claude, cuts and captions the best ones, and produces a page for
human review.

**Status: walking skeleton.** The package, CLI, validated config, run folders and a
restartable six-stage pipeline exist, but every stage is a stub. There is no video
processing, transcription or AI call yet. See `docs/MVP.md` for the finish line and
`docs/brief-01-skeleton.md` for what this stage covers.

## How it works

```
fetch -> transcribe -> candidates -> score -> render -> report
```

Each stage reads files from the run folder and writes its own, so a crashed or
unsatisfying stage can be rerun without redoing the earlier ones. Progress is tracked
in `runs/<date>_<name>/run.json`.

## Setup on Windows (clean machine)

You need [uv](https://docs.astral.sh/uv/), ffmpeg on `PATH`, and git. You do not need to
install Python yourself: uv downloads Python 3.11 on first use.

```powershell
winget install --id astral-sh.uv -e
winget install Gyan.FFmpeg.Essentials
```

Open a **new** terminal afterwards so `PATH` is refreshed, then:

```powershell
ffmpeg -version
ffprobe -version
git clone <repo-url> clip-machine
cd clip-machine
uv sync --locked
```

`uv sync --locked` creates `.venv`, installs Python 3.11 if needed, and installs exactly
the versions pinned in `uv.lock`.

## Run

Run commands from the repository root (that is where `config/`, `clients/` and `runs/` are found).

```powershell
# New run: input file plus a client folder from clients/
uv run python -m clipmachine run path\to\episode.mp4 --client example-client

# Resume a run folder: finished stages are skipped
uv run python -m clipmachine run runs\2026-10-12_episode

# Rerun from a stage onward (that stage and every later one)
uv run python -m clipmachine run runs\2026-10-12_episode --from-stage render

# Report (stub for now)
uv run python -m clipmachine report runs\2026-10-12_episode
```

Accepted inputs: `.mp4 .mov .mkv .m4a .wav .mp3`.

Exit codes: `0` success, `1` a stage failed (the run folder keeps the error in `run.json`),
`2` bad usage, bad config, bad input, or ffmpeg/ffprobe missing.

## Configuration

- `config/default.yaml`: clip length, how many clips to keep, model names, caption style.
- `clients/<name>/rules.yaml`: per-client niche and tone. Its `clip_length` overrides the
  default clip length. The merged result is saved in each run as `config.resolved.yaml`.

Unknown keys, wrong types and inconsistent values (for example `min_seconds >= max_seconds`)
are rejected at startup with a message naming the field.

## Checks

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run pip-audit --skip-editable
```

Tests need no ffmpeg, network or API key. The same commands run in CI
(`.github/workflows/ci.yml`).

## Secrets

The Claude API key will live only in `.env` as `CLIPMACHINE_ANTHROPIC_API_KEY` (copy
`.env.example`). `.env` is git-ignored. Nothing reads it yet.

## Layout

```
config/            default settings
clients/<name>/    per-client rules
prompts/           prompt files (arrive with the stages that use them)
src/clipmachine/   cli, config, models, pipeline, run folders, stages/, utils/
runs/              one folder per processed episode (git-ignored)
tests/
docs/              plan, MVP, costs, decisions, briefs
```
