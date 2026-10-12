# Clip Machine

Turns a long video (podcast, interview, YouTube episode) into short vertical clips
for Shorts, Reels and TikTok. It transcribes the video, finds candidate moments,
scores them with Claude, cuts and captions the best ones, and produces a page for
human review.

**Status: walking skeleton.** The package, CLI, validated config, run folders and a
restartable six-stage pipeline exist. The `fetch` stage (validates the input, copies it into
the run folder, extracts audio), the `transcribe` stage (local Whisper, word timestamps) and
the `candidates` stage (Claude Haiku proposes moments, snapped to sentence boundaries) are
real; the other three stages are still stubs. See `docs/MVP.md` for the finish line and
`docs/brief-01-skeleton.md` to `docs/brief-04-candidates.md` for what each step covers.

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

Clips follow the speaker by default (`video.crop: speaker`). That needs two face model
files, which you fetch once (nothing else ever downloads them):

```powershell
uv run python -m clipmachine fetch-face-model
```

Without them `run` stops before the first stage with a one-line error. Set `video.crop: center`
in `config/default.yaml` for a fixed centre crop, which needs no model files. A run folder keeps
the crop mode it was started with (`config.resolved.yaml`).

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

Accepted inputs: `.mp4 .mov .mkv .webm` (case-insensitive). Audio-only files
(`.m4a .wav .mp3`) are rejected: clips need video.

### What fetch produces

The `fetch` stage checks that the input really is video (ffprobe decides, not the file
extension), then writes three files into the run folder:

- `source.<ext>`: a copy of the input.
- `audio.wav`: 16 kHz mono 16-bit audio, for transcription.
- `meta.json`: original file name, SHA-256, size, ffprobe details (container, duration,
  video and audio streams, rotation, whether the frame rate is variable), ffmpeg version
  and timing.

Limits live in `config/default.yaml` under `fetch:` (defaults: at most 8 GB, at most 240
minutes, at least 60 seconds). Fetch also needs free disk space of twice the file size plus
1 GB. A rejected input stops the stage with a one-line reason in `run.json` and leaves no
outputs behind.

**The original file is never changed or moved.** It is only opened for reading; everything
fetch writes goes into the run folder.

Exit codes: `0` success, `1` a stage failed (the run folder keeps the error in `run.json`),
`2` bad usage, bad config, bad input, or ffmpeg/ffprobe missing.

### What transcribe produces

The `transcribe` stage runs [faster-whisper](https://github.com/SYSTRAN/faster-whisper) on
`audio.wav` and writes:

- `transcript.json`: the model, the device and compute type actually used, the settings,
  detected language, audio length, processing time, real-time factor, every segment with its
  words (start, end, text, probability), and a list of warnings. Times are checked and small
  problems are clamped with a warning; absurd output fails the stage.
- `transcript.txt`: one `[mm:ss] text` line per segment, for spot-checking against the video.

The transcript is data from an untrusted source (it is whatever was said in the video). It is
never logged and must never be passed to a model as instructions.

Settings live under `transcribe:` in `config/default.yaml` (`model`, `device`, `compute_type`,
`language`, `beam_size`, `vad_filter`, `condition_on_previous_text`, `batch_size`,
`cpu_threads`). Close Ollama and other heavy apps before long runs: they compete for RAM and
VRAM.

### Models

Models download on first use into `models/` in the repository (git-ignored). Nothing goes to
your user-level Hugging Face cache. Approximate sizes on disk: `tiny` 75 MB, `small` 464 MB,
`turbo` 1.6 GB. `large-v3` is about 3 GB.

### GPU on Windows (optional, NVIDIA)

Without a GPU everything still works on the CPU: `device: auto` falls back to it and logs one
line saying why. For an NVIDIA GPU, install the CUDA 12 libraries, which are an optional
dependency group (about 1.3 GB to download, 2 GB unpacked in `.venv`):

```powershell
uv sync --locked --group gpu
```

This route worked on a GTX 1660 Ti (driver 616.92): the `nvidia-cublas-cu12`,
`nvidia-cudnn-cu12` and `nvidia-cuda-nvrtc-cu12` wheels. No CUDA Toolkit, driver change, admin
rights or system PATH edit is needed: at run time the app makes the DLL folders findable for
its own process only. A plain `uv sync` removes the group again; add `--group gpu` each time.
If you cannot use the wheels, unpack an NVIDIA CUDA 12 library archive into `tools/cuda12/`
(git-ignored); the same loader picks it up.

To check the GPU is really used, watch `nvidia-smi` while a run is going: the python process
appears in the compute apps list and memory used rises. The run also records `device` in
`transcript.json`. Set `CLIPMACHINE_DISABLE_CUDA=1` to force the CPU for one run.

## Configuration

- `config/default.yaml`: clip length, how many clips to keep, model names, prices, budget,
  candidates settings, caption style.
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

Unit tests need no ffmpeg, GPU, model, network or API key. Tests marked `ffmpeg` run real
ffmpeg and ffprobe on tiny generated files and are skipped when those are not on `PATH`.
Tests marked `whisper` run the real `tiny` model on the CPU and are skipped unless
`CLIPMACHINE_WHISPER_TESTS=1` is set (the first such run downloads about 75 MB into `models/`). The same
commands run in CI (`.github/workflows/ci.yml`), which installs ffmpeg so they run there too.

## Claude API (candidates stage)

Only the `candidates` stage calls the API, and only transcript text is sent (never video or
audio). Everything else runs locally.

**1. Create `.env`** in the repository root (copy `.env.example`) with one line:

```
CLIPMACHINE_ANTHROPIC_API_KEY=<your key>
```

`.env` is git-ignored. The key is read only from this file, by a small built-in parser, and
passed to the SDK explicitly. **Never export `ANTHROPIC_API_KEY`**: the app ignores it, and
Claude Code would start using it ahead of your subscription.

**2. Spend limits.** Set a Console spend limit as a second lock (Settings > Billing > Spend
limits), buy no credits and keep auto-reload off (see `docs/COSTS.md` section 3).

**Kill switch.** `CLIPMACHINE_NO_API=1` makes every API call refuse before any network use or
key lookup. The candidates stage then fails with "API disabled".

```powershell
$env:CLIPMACHINE_NO_API = "1"      # for this terminal only
Remove-Item Env:CLIPMACHINE_NO_API # turn it off again
```

**Budget guard.** Before each call the worst-case cost is estimated: input tokens (the free
token-counting endpoint plus 10 %, or characters / 3 if counting fails) and `max_tokens` of
output, priced from `pricing:` in `config/default.yaml`. A call that would take the run past
`budget.max_usd_per_run` (default $1) or today's total (UTC) past `budget.max_usd_per_day`
(default $5) is refused before it is sent. Prices live in `pricing:` with the date they were
copied from the live pricing page; a model without a price is refused, never guessed.

**Where costs are logged.**

| File | Holds |
|---|---|
| `runs/<run>/costs.json` | every call and every refusal of that run: purpose, model, input / output / thinking / cache tokens, estimated and actual USD, duration, request ID, totals |
| `runs/_ledger/costs.jsonl` | one line per paid call from all runs (git-ignored); the source of the daily total |

Neither file holds prompts, transcript text or the key. Errors that mean money or limits (your
spend limit, no credits, a bad key, Anthropic's monthly cap) are never retried and stop the run
with a one-line message that says what to check. Rate limits with a `retry-after` header and 5xx
errors are retried up to three attempts in all.

**Candidates output.** `candidates.json` holds each candidate's snapped `start` / `end`, the
model's original `model_start` / `model_end`, `hook`, `reason`, `type`, the text rebuilt from the
words, and `snap_notes`, plus a summary of the counts and cost. Tune `candidates:` in
`config/default.yaml`; the prompt is `prompts/find_moments.md`.

## Secrets

The Claude API key lives only in `.env` as `CLIPMACHINE_ANTHROPIC_API_KEY`. `.env` is
git-ignored. No code prints, logs or writes the key or the environment.

## Layout

```
config/            default settings
clients/<name>/    per-client rules
prompts/           prompt files, one per stage that calls the model
src/clipmachine/   cli, config, models, pipeline, run folders, stages/, utils/
runs/              one folder per processed episode (git-ignored)
models/            downloaded Whisper models (git-ignored)
tools/             optional CUDA 12 library folder (git-ignored)
tests/
docs/              plan, MVP, costs, decisions, briefs
```
