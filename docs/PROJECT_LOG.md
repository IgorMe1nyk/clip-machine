# PROJECT_LOG.md

Dated entries, newest first. Numbers come from real runs only.

## 2026-10-10 — Brief 5: Score stage

**Facts**
- Commits: 6 feature/test commits plus this log entry. Tests: 582 passed, 1 skipped. ruff, mypy and
  pip-audit clean. No new dependency.
- Sonnet 5.5 rejects `thinking: disabled` with a 400; its lowest setting is `between_tools`
  (accepted at low, medium and high effort). Config: `score.thinking: between_tools`,
  `score.effort: medium`. Default effort for the model is `high`, so it is set explicitly.
- `call_structured` gained an optional `check` hook and a `repairs` count (0 or 1), so the id check
  uses the client's one repair call and the guard, ledger and cost log as before.
- Real run (15.4-min interview, `general` client, 8 candidates): 1 call, 1 chunk, no repair.
  4,298 input and 1,043 output tokens, 0 thinking tokens. Actual $0.019026 against a worst-case
  estimate of $0.089456 (the 8,000-token output ceiling). 8.9 s. About $0.074 per hour of video
  for stage 4. Rerun: "nothing to do", ledger unchanged (5 lines).
- Result: 1 kept, 7 rejected, 0 dropped by `min_score`, 0 dropped by `top_n`, 0 trimmed.
  Model reject reasons: needs_context 4, weak_hook 1, off_brand 1, below_threshold 1,
  no_payoff 0, too_long_or_rambling 0.
- Model vs Igor (c01, c03, c07, c08, from the Brief 4.1 spot check): **agreement 2 of 4**.
  - c03 and c07: both sides see a start mid-sentence (C1 fail). Agree on the defect; the model is
    harsher on "understandable".
  - c01: Igor rated it OK; the model rejected it under the client's "avoid intros" rule. Disagree.
  - c08: Igor rated it clean; the model rejected it as starting mid-sentence. Disagree, **pending
    Igor's re-listen**: its snap notes say the start is a segment edge with no sentence
    punctuation before it, and it starts exactly where c07 ends, which fits the model's claim.

**Observed**
- The model never used `keep_sentences`, so the trim path has only been exercised by tests, not by
  a real answer.
- 1 of 8 kept is strict but matches the brief ("it is normal to keep only a few"); neither
  `min_score` nor `keep_top` was the limit.
- The model's `below_threshold` reason is recorded as its own word; our own cuts are `dropped_by`
  (`min_score` / `top_n`) and are never written into `reject_reason`.

**Parked**
- Tell Sonnet which candidates start or end at a fallback segment edge (from `snap_notes`); may
  help with the c03/c07/c08 kind of C1 failure.
- Store word indices in `candidates.json` so the score stage need not locate candidates by time.

## 2026-10-10 — Brief 4.1: Candidate quality fixes

**Evidence (Igor's spot check of 4 of 8 candidates):** 2 of 4 were understandable alone. One had its payoff cut short; two started mid-sentence.

**Root causes**
- Snapping treated every Whisper segment edge as a sentence start. Whisper breaks segments mid-sentence: 4 of 8 starts and 3 of 8 ends were segment edges.
- All 8 moments were 20-27 s, right at the minimum. The model cut answers short.

**Done**
- A sentence ends only after `. ? !`. A segment edge is a boundary only after more than 40 unpunctuated words (`candidates.fallback_max_words`).
- Prompt asks for a new thought at the start and a complete payoff, preferring 30-50 s.
- New config `preferred_min_seconds` / `preferred_max_seconds`, validated against the clip range and clamped into a client's `clip_length`.
- Snapping grows short moments toward the end only, never past the maximum. Under the hard minimum it may still grow backward as a last resort; that is noted in `snap_notes` and counted in `summary.grew_backward`.

**Before vs after (same transcript, 8 candidates each)**
- Segment-edge starts: 4 -> 1. Segment-edge ends: 3 -> 2.
- Length min / median / max: 20.3 / 22.1 / 27.3 s -> 32.3 / 37.2 / 47.6 s.
- Grew backward: 0 of 8.
- No moments were invalid, dropped or duplicates.

**Cost:** $0.0013 for the re-run (2 calls); the earlier Brief 4 run cost $0.0013 too.

**Igor's spot check of the new candidates (rubric terms)**
- **c08:** clean. C1, C2 and C4 pass.
- **c01:** OK, but starts slightly late (a padding issue, parked for Brief 6). It is an intro moment.
- **c03 and c07:** start mid-sentence (C1 fail) but are understandable.
- Only 4 of the 8 new candidates were checked (c01, c03, c07, c08); c03 and c07 are open C1 failures to look at in Brief 5/6.

**Caught along the way**
- Claude Code copied `candidates.json` (private transcript text) to `%TEMP%` as a backup, outside the repo. It disclosed this; Igor deleted the file. Fix: CLAUDE.md now says scratch and backup files go only in `runs/_scratch/` (git-ignored).
- Claude Code used bare `python` for some file edits. Fix: CLAUDE.md now says always run Python through `uv run`.
- A path typed with backslashes (`runs\...`) was not recognised as a run folder in Git Bash. Use forward slashes.

**Parked:** start padding (Brief 6, when real clips can be heard); abbreviation handling.

## 2026-10-10 — Brief 4: Claude client and candidates stage

**Facts**
- Commits: 9 (two docs commits first). Tests: 485 passed, 1 skipped (was 310). ruff, mypy and
  pip-audit clean.
- New runtime dependency: anthropic 1.13.0 (MIT); new packages docstring-parser, jiter, sniffio.
- Real run (15.4-min interview, `general` client, Haiku 5.5, thinking disabled, effort low): 2 calls,
  8,810 input and 830 output tokens, 0 thinking tokens, actual $0.001296 against a worst-case
  estimate of $0.0029692. About 3-4 s per call. 8 candidates returned, 0 invalid, 0 dropped, 0
  duplicates. Rerun: "nothing to do", ledger unchanged (2 lines).
- Cost per hour of video for stage 3: about $0.005 (2 calls for 923 s).
- Kill switch and budget refusal both checked on a real run folder with no network use.
- `count_tokens` accepted `output_config` and `thinking`; the estimate source was `count_tokens`.

**Observed**
- Whisper words are contiguous (each word ends where the next starts), so the 0.2 s padding is
  limited to 0.00 s at almost every boundary. The padding setting has no effect on this transcript.
- 4 of 8 candidate starts and 3 of 8 ends sit at a segment edge with no sentence punctuation. These
  are the boundaries most likely to fail rubric C1; Igor's spot check decides.
- With the brief's numbers a 15-minute video gives at most 10 candidates, not about 20.

**Parked**
- Padding into the silence between words needs a different rule (midpoint of the gap, or a fixed
  lead-in taken from the neighbouring word's start).
- Per-block response caching, so a stage that fails midway does not repeat paid calls.
- Abbreviations ("Dr.") count as sentence ends; a short last block is not merged.
- The SDK still reads `ANTHROPIC_CUSTOM_HEADERS` from the environment; `ANTHROPIC_BASE_URL` and the
  key are passed explicitly.
- The ledger is not locked: two runs at the same moment could both pass the daily check.
- CLAUDE.md's opening line still says only `fetch` is real.

## 2026-10-10 — Session 2 (17:47–20:00 ET): Briefs 2 and 3

**Done**
- **Brief 2 (fetch)** and **Brief 3 (transcribe)** shipped. CI is green.
- The default transcription setting is GPU `small` (D-011). A 15-minute interview transcribes in about 50 s.
- **Laptop check:**
  - Hardware: HP Omen, Ryzen 7 4800H, 16 GB RAM, GTX 1660 Ti 6 GB.
  - Free RAM went from 1.9 GB to 8.3 GB after a restart and turning off unneeded startup apps. Disk is fine (300 GB free).

**Decisions**
- D-011: default transcription setting.
- D-012: test inputs. The fetch and transcribe checks used third-party clips, privately and locally only. They are deleted after testing and are not used for evaluation material, demos or samples. The sample run for the finish line needs a video Igor owns or a CC-licensed one.

**Caught in reviews**
- Extraction now picks a fixed audio track (`-map 0:a:0`).
- The audio-duration tolerance scales with length.
- One run folder per benchmark setting.
- `nvidia-smi` added to the allow-list, so routine GPU checks don't need a click each time.
- Hugging Face cache and telemetry are kept inside the repo.

**What I did vs what AI did**
- **Igor:** decisions D-011 and D-012, laptop cleanup, test inputs, pushes and checks.
- **Claude (chat):** Briefs 2 and 3, plan and report reviews, the laptop check, the cost register update.
- **Claude Code:** Opus 5.5 wrote the plans and Sonnet 5.5 wrote the code and tests.

**Cost:** $0. No API calls yet.

**Parked**
- A rejected input still creates a run folder.
- Probe the copied file rather than the original.
- The Purfview GPU library route is untested.
- Segment timestamps in batched mode.
- Optional RAM upgrade (a purchase; Igor's call).

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
