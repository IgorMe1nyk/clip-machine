# Brief 3 — Transcribe stage (approved)

> **Models (D-009):** Plan with **Opus 5.5, Medium** in Plan mode. Build with **Sonnet 5.5, Medium**. If GPU library loading on Windows turns into a loop of failures, stop and tell Igor; he may switch you to Opus, High.
> Builds on Brief 2 (fetch works; `audio.wav` is 16 kHz mono). Status: APPROVED by Igor when he hands it to you.

## Goal
Make stage 2 (`transcribe`) real: turn `audio.wav` into `transcript.json` with **word-level timestamps**, plus a human-readable `transcript.txt`.
- Use the NVIDIA GPU when it works; fall back to CPU cleanly.
- Measure speed on Igor's real video, so Igor can pick the default model from real numbers.

**Hardware (from Igor's diagnostic):**
- AMD Ryzen 7 4800H (8 cores, 16 threads)
- 16 GB RAM, often under pressure
- NVIDIA GeForce GTX 1660 Ti, 6 GB VRAM, driver 616.92
- Native Windows 11 (D-007)

## Read first
1. `CLAUDE.md`, `docs/DECISIONS.md`, and `docs/COSTS.md` (rows for faster-whisper, CTranslate2, Whisper weights, NVIDIA libraries).
2. The fetch stage (`stages/fetch.py`, `utils/ffmpeg.py`) and its tests. Follow the same patterns: atomic writes, cleanup on failure, one-line errors.
3. The live faster-whisper README: https://raw.githubusercontent.com/SYSTRAN/faster-whisper/master/README.md. Also check the **installed version's own source** for the exact model names and argument names; don't trust memory or the README alone.

Work in Plan mode first. Show me the plan, wait for my OK, then implement in small commits.

## Rules
- **New runtime dependency allowed:** `faster-whisper` (MIT). Add it with `uv add`. Igor approves the prompt.
- **GPU libraries:**
  - Allowed only as an **optional dependency group `gpu`**. Never a default dependency, never installed in CI.
  - Candidates: `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` (cuDNN 9).
  - Ask Igor before adding anything else.
- **Report licenses** of every new package pulled in (faster-whisper, ctranslate2, onnxruntime, av, tokenizers, huggingface-hub, nvidia-*), from their package metadata. Don't edit `COSTS.md`; Igor updates it.
- **No system changes:**
  - no global PATH edits, no admin installs, no CUDA Toolkit install, no driver changes
  - GPU DLLs are loaded at runtime with `os.add_dll_directory()`, either from the `gpu` group's packages or from a git-ignored `tools/cuda12/` folder
  - If the pip route fails on Windows (the README now says the pip method is for Linux), stop and ask Igor whether to use Purfview's `whisper-standalone-win` CUDA 12 library archive, unpacked into `tools/cuda12/`. Igor downloads it himself.
- **Models download only into `<repo>/models/`** (git-ignored, via the download root / cache dir).
  - Set `HF_HUB_DISABLE_TELEMETRY=1` in-process.
  - If any library writes outside the repo anyway (for example a Hugging Face cache or lock files in your user folder), report the exact paths.
- **The transcript is untrusted data.** Store it as data. It will be sent to Claude later, but never as instructions.
- All earlier rules still apply: nothing outside the repo; stage outputs only in the run folder; argument lists, never shell strings.

## Steps
1. **Config.** Extend the existing `transcribe` section, keeping `extra="forbid"`, validation, and backward compatibility with old `config.resolved.yaml` files.

   | Key | Default | Allowed values / notes |
   |---|---|---|
   | `model` | `small` | names the installed faster-whisper version supports; at least `tiny`, `base`, `small`, `medium`, `large-v3`, `turbo`, `distil-large-v3` if available |
   | `device` | `auto` | `auto`, `cpu`, `cuda` |
   | `compute_type` | `auto` | `auto` (cuda → `int8_float16`, cpu → `int8`), `float16`, `int8_float16`, `int8`, `float32` |
   | `language` | `null` | `null` = auto-detect, otherwise an ISO code like `en` |
   | `beam_size` | `5` | — |
   | `vad_filter` | `true` | — |
   | `condition_on_previous_text` | `false` | reduces repetition loops on long audio |
   | `batch_size` | `0` | `0` = normal transcribe; `>0` uses `BatchedInferencePipeline` |
   | `cpu_threads` | `0` | `0` = library default |

2. **Device resolution.**
   - `auto`: if CUDA is usable (device count > 0 *and* the model actually loads), use it. Otherwise fall back to CPU and log one line saying why.
   - `cuda`: fail clearly if it isn't usable.
   - Record the device and compute type actually used.
3. **Transcription** (`stages/transcribe.py` + a small `utils/whisper.py`):
   - `word_timestamps=True`
   - log progress about every 60 s of audio processed (percent + elapsed time)
   - free the model after use
4. **transcript.json** (pydantic, atomic write). Fields:
   - `schema_version`
   - `model`, `device`, `compute_type`, `settings` (the transcribe config used)
   - `language`, `language_probability`
   - `audio_duration_seconds`, `transcribe_seconds`, `realtime_factor` (audio seconds ÷ processing seconds)
   - `segments`: `id`, `start`, `end`, `text`, `avg_logprob`, `no_speech_prob`, and `words` (each with `start`, `end`, `word`, `probability`)
   - `warnings`: a list of strings
5. **Validation.**
   - Every time satisfies 0 ≤ start ≤ end ≤ audio duration + 1.0 s.
   - Words are in non-decreasing order, allowing a 0.05 s overlap.
   - Small problems: clamp them and add a warning.
   - Absurd output (for example most words out of range): fail the stage.
6. **transcript.txt:** one line per segment, `[mm:ss] text`, for Igor to spot-check against the video.
7. **Restartability.** Temp files are cleaned up on failure, and `--from-stage transcribe` replaces both outputs cleanly.
8. **Tests.**
   - **Unit (no model, no GPU; mock `WhisperModel` and CUDA detection):**
     - config validation
     - device/compute resolution, including auto → CPU fallback when the model fails to load on CUDA
     - transcript building from fake segments
     - validation and clamping
     - `transcript.txt` format
     - failure cleanup
   - **Integration tests marked `whisper`:** skipped unless `CLIPMACHINE_WHISPER_TESTS=1` is set. CPU only, `tiny` model, run on a short lavfi audio file, checking that a schema-valid transcript is written.
   - **CI:** unit tests only (the `whisper` tests stay skipped). The `gpu` group isn't installed there.
9. **README:** GPU setup on Windows (the route that actually worked), the CPU fallback, the model download location and sizes, and "close Ollama and heavy apps before long runs."

## Benchmark (part of acceptance, on Igor's real 10–15 min video)
Run fetch once, then `--from-stage transcribe` with each setting, changing settings through a temporary client or config copy, not by editing defaults:

| # | device | model | compute_type | batch_size |
|---|---|---|---|---|
| A | cpu | small | int8 | 0 |
| B | cuda | small | int8_float16 | 0 |
| C | cuda | turbo | int8_float16 | 0 |
| D | cuda | turbo | int8_float16 | 8 (only if C worked and VRAM allows) |

If `int8_float16` fails on this GPU, try `float16`, then `int8`, and report.

For each run, report:
- `transcribe_seconds` and `realtime_factor`
- peak VRAM, sampled with `nvidia-smi` during the run
- detected language and number of warnings
- 3 excerpts with timestamps (start, middle, end), the same 3 times for every setting, so Igor can compare them against the video

Also confirm the GPU was really used: `nvidia-smi` should show the Python process and its VRAM during the run.

Igor picks the default from the results (it becomes decision D-011). Then set it in `config/default.yaml` in a final small commit.

## Out of scope
Speaker diarization, translation, caption files, candidates, any Claude API call, CUDA Toolkit or driver installs, WSL, and audio-only inputs.

## Acceptance checks (paste output)
```
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q                                   # passed / skipped counts
uv run pip-audit --skip-editable
```
**End-to-end:**
1. A new run on Igor's video, all the way through transcribe: show the run folder listing, the head of `transcript.txt` and the key fields of `transcript.json`.
2. Rerun → "nothing to do".
3. `--from-stage transcribe` → outputs replaced, no temp files left.
4. Force a CPU fallback by making CUDA unavailable for one run (an environment toggle in tests is fine) → the run logs the one-line fallback reason and still succeeds.
5. The benchmark table above.

## Report back
- Output of every acceptance command.
- Commit list.
- Which GPU library route worked, and how you proved the GPU was used.
- The benchmark table and the 3 excerpts per setting.
- Licenses of the new packages.
- Any files written outside the repo.
- Deviations, open questions, anything unverified.
