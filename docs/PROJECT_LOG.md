# PROJECT_LOG.md

Dated entries, newest first. Numbers come from real runs only.

## 2026-10-10 — Brief 3: Transcribe stage (benchmark done, default not chosen yet)

**Facts**
- New runtime dependency: faster-whisper 1.2.1 (MIT) with ctranslate2 4.8.2, onnxruntime, av,
  tokenizers, huggingface-hub (plus hf-xet, numpy, tqdm and a few small helpers). New optional
  group `gpu`: nvidia-cublas-cu12 12.9.2.10, nvidia-cudnn-cu12 9.27.0.42 and (transitively)
  nvidia-cuda-nvrtc-cu12 12.9.86. The newest cuDNN already has a Windows wheel, so no upper bound.
- GPU route that worked first time: the pip wheels in the `gpu` group, with
  `os.add_dll_directory`, a ctypes preload and a process-local PATH prefix. No Purfview archive.
- Benchmark on a 923 s two-speaker interview, GTX 1660 Ti: see the report; no default chosen yet.

**Parked**
- Batched mode (`batch_size > 0`) returns about 26 s segments because the library turns timestamps
  off per chunk; with timestamps on it gave 236 segments but ran about 40 % slower. A config option
  for this is not built.
- `turbo` lost punctuation and capitalisation in some stretches; `small` did not.
- Per-process VRAM shows N/A under Windows WDDM; GPU use is shown by the PID row, the total memory
  rise and utilisation instead.
- Word strings keep faster-whisper's leading space (" Hey"); caption code must strip it.
- Hugging Face prints an "unauthenticated requests" warning on first download; harmless.

## 2026-10-10 — Brief 2: Fetch stage

**Facts**
- Commits: 9 (8 feature/docs/test commits + this log entry), `24a0978` to this one.
- Tests: 232 passed, 0 skipped (was 104). 8 of them are `ffmpeg`-marked integration tests.
- ruff, mypy and pip-audit clean. No new dependencies.
- Real video (`3Dprinting_time laps.mp4`, 5,219,973 bytes, 66.3 s, 640x360 h264/aac): first fetch
  `fetch_seconds` 0.79, `--from-stage fetch` 0.253. Audio is 16000 Hz mono `pcm_s16le`. SHA-256 of
  the copy matches the original; original size and mtime unchanged.
- Rejections checked with real files: text renamed `.mp4` (exit 1, run folder keeps only
  `run.json` and `config.resolved.yaml`), `.mp3` (exit 2, no run folder), missing path (exit 2).
- `-protocol_whitelist file` and the `file:` prefix work on Windows paths with spaces. On ffmpeg
  9.0.1 no hostile playlist tried (HLS, DASH, concat) connected even without the flag, so the flag's
  own effect is unproven here.

**Parked**
- Rejected input still creates a run folder and the CLI suggests "rerun" for it; consider
  rejecting before the folder exists, or a clearer message.
- A copy-then-probe order (probe the run-folder copy, not the original) would close the small
  window where the original changes between probe and copy.

## 2026-10-10 — Session 1: Gate 0 + Brief 1 (02:11–04:00 ET)

**Done**
- **Gate 0:** plan review (top 5 risks), `COSTS.md` from live pricing pages, `MVP.md` (finish line + clip rubric), decisions D-001 to D-010.
  - Key finding: Max 5x includes $100/month of API credits that stop (not overcharge) when empty, so the cash budget is $0.
- **Setup (native Windows 11):**
  - Tools: Git 2.55.0, uv 0.11.21, Python 3.11.15, FFmpeg 9.0.1 essentials, Claude Code 2.1.296, VS Code + Claude Code extension.
  - Git commits use the GitHub noreply email.
- **Brief 1, walking skeleton:** 12 commits.
  - Checks: 104 tests pass; ruff, mypy and pip-audit are clean.
  - End-to-end checks pass: new run, rerun with "nothing to do", `--from-stage render`, and `--client ../evil` rejected.

**Caught along the way**
- Path escape: a run-folder slug like `../../evil` could resolve outside `runs/`. Fixed with a single-path-component check and a test.
- 3.12-only generic syntax: the ruff hook flagged it on a real edit. Fixed.
- Stray folder: a failed `cd` led Claude Code to create a folder outside the repo (`dev\utils`). It disclosed this and removed it, and now uses absolute paths. Brief 2 adds a "never write outside the repo" rule.
- Approval fatigue: about 100 routine prompts. Replaced with an allow-list (D-010).

**Cost:** $0. No Claude API calls yet; Claude Code ran on the Max plan.

**What I did vs what AI did**
- **Igor:** every decision (D-001 to D-010), installs and accounts, checkpoint reviews (guardrail test, commit-email check), and pushed back on approval fatigue.
- **Claude (chat):** research with sources, the cost register, the MVP and rubric, Brief 1, plan and report reviews, and this log.
- **Claude Code:** Opus 5.5 wrote the plan; Sonnet 5.5 wrote the code and tests.

**Parked**
- GitHub Actions are pinned by tag, not commit SHA (revisit before taking clients).
- Test video choice (D-006).
- Phase 0 niche and target list.

**Next:** push to GitHub (first CI run), then Brief 2 (Fetch: local file, ffprobe validation, audio extraction).
