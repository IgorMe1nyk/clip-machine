# HANDOFF — Clip Machine (read this first)

> **Written:** 2026-10-11 01:50 ET, end of chat 1, session 3 (ran past midnight).
> **Read next:** `ROLE-instructions.md` (how to work with Igor; skip its `<first_move>`, since Gate 0 is done; don't re-ask PC specs, budget or hard stop), then `docs/ROADMAP.md` (the plan: phases A–F, gates, steps).
> **Source of truth:** the repo (`C:\Users\15513\dev\clip-machine`, public as IgorMe1nyk/clip-machine). Repo docs are newer than any copy here; ask Igor to paste one if exact text matters.

## 1. Resume point
**Brief 8 Part A (speaker tracking) is built and committed, and is waiting at its CHECKPOINT.**
- HEAD `47027a4` (plus possibly one CLAUDE.md "pipefail" commit and a T1 `track` run done right before stopping; check with Igor).
- MediaPipe 1.1.0 is installed (locked), with `face_landmarker.task` downloaded and pinned by SHA-256. 865 tests pass. pip-audit is clean.
- **First job tomorrow:** Igor downloads a **two-person studio-style podcast at 1080p** (people at a table, real cameras; private per D-012) into `C:\Users\15513\Videos\clip-tests\`, named `t2.mp4`. Then:
  1. `uv run python -m clipmachine run "C:\Users\15513\Videos\clip-tests\t2.mp4" --client general` (about $0.05; Claude Code asks first)
  2. `track <run-folder> --preview`, after which Igor watches each `clips/<id>.track.preview.mp4`. **Checkpoint pass = the right person ≥90% of the time, no flicker, cuts at speaker changes.**
  3. If it passes: Brief 8 **Part B** (moving crop via ffmpeg `sendcmd`; times relative to the clip start; pixel test at a known time).
- **T1 `track` result (done):** both clips → `no_face` fallback (0 faces in about 300 samples each; 640×360 analysed; 4.5–6.4 s per clip). The safe fallback works on real video. The detector simply misses the tiny picture-in-picture webcam faces (BlazeFace short-range). No telemetry line in stderr (not a network measurement). CLAUDE.md pipefail rule committed (`f38be15`).
- **Watch at the checkpoint [judgment]:** BlazeFace short-range is built for faces close to the camera. In a **wide** two-person studio shot, faces may also be small. If T2 shows misses, try in this order: `speaker.analysis_width` 640 → 1280 (config only), then the plan's half-frame tiling in `utils/faces.py`.
- **Check first:** pushed, with CI green (CI also proves mediapipe installs on Ubuntu)? Commit the untracked docs (ROADMAP.md, brief-09, research guide, HANDOFF.md) in one `docs:` commit.

## 2. Order of work (from ROADMAP.md)
1. Brief 8 checkpoint → Part B → rate C6 on T2 (and later a solo T3).
2. **Brief 9: boundary repair** (`docs/brief-09-boundary-repair.md`, ready). The judge may extend clips (start earlier, end later), not just trim them. The caption offset default becomes 0.15. Test on **frozen** T1 candidates.
3. **Brief 9.1: scoring rubric** from `docs/research/clip-scoring-guide.md` section 7 (hook 0–3, standalone 0–2, payoff 0–2, shareability 0–2, +1; sound-off test). A separate re-score on the same frozen candidates, so each change is measured alone.
4. **Vocabulary brief:** per-client hint words for Whisper ("Claude" was transcribed as "Claw").
5. Polish (loudness, caption placement, a low-resolution warning, no watermark), then the MVP finish line and EVAL.md row 1.
6. The UI (design first, PRODUCT.md v2, D-015 stack), then ops and proof.

## 3. Evidence so far (real runs)
| Run | Result |
|---|---|
| T0 counseling interview (15 min, private) | 1 kept of 7. Video unsuitable. Proved render and captions. C6 failed (two people, center crop) |
| **T1** `runs/2026-10-11_t1` (35-min AI screen-share podcast, **360p**, picture-in-picture faces) | **First one-command end-to-end run:** about 2.5 min total, transcribe 121 s (17.6× realtime), **$0.044** (Haiku $0.0033 + Sonnet $0.041). 19 candidates, **2 kept (c09, c15)**: Igor says content is **usable**, captions in sync at 0.15, but "Claude" → "Claw". 13 of 17 rejects were needs_context |
| T1 near-miss listen (Igor) | c08 **fine** (false reject); c03 and c19 **end too early**; c06 and c17 **start mid-thought**. Diagnosis: **boundaries, not content**, hence Brief 9 |

**Precision is good** (2/2 kept clips are good). **Recall is low** (boundaries). The **caption offset 0.15** is confirmed on 2 videos.

## 4. Decisions
D-001 to D-013 as before. New or changed this session:
- **D-006 (update proposed, Igor hasn't explicitly confirmed):** private validation on third-party podcasts (under D-012); **public proof only from permissioned creators** (clipping campaigns, clients). Confirm and record.
- **D-014:** reframing uses MediaPipe Face Landmarker + lip-movement speaker choice + cut-based camera. **Telemetry (option A): accepted and documented**; it can't be disabled in 1.x; no hosts-file change. The extra packages (opencv-contrib, matplotlib, sounddevice) are accepted. Sampling at 10 fps. `video.crop: center | speaker`.
- **PRODUCT.md approved:** Igor is the only reviewer in v1.
- **The UI is pulled earlier** (Phase E, after quality). Igor wants live progress, review, approve, clients and export, with no terminal.

## 5. Business flags
- **Most podcasts are two people.** Speaker crop (Brief 8) is a must before clients.
- **Low-resolution sources get less reach** (Instagram's own ranking post lists low-resolution, watermarked, bordered and mostly-text reels as less visible). Always get **original files** from clients. Never add a watermark by default.
- **The niche is still unpicked.** It decides whether studio or screen-share layouts matter first. Igor plans to join **creator clipping campaigns** (permissioned, so they're the source of public proof).
- Svitlo opens **Oct 20**.

## 6. Standing rules (abbreviated)
- **Secrets:** the key lives only in `.env` as `CLIPMACHINE_ANTHROPIC_API_KEY`. Never export `ANTHROPIC_API_KEY`.
- **Money:** $0 cash; Max API credit only; Console spend limit $5. Any new paid dependency → ask Igor.
- **Privacy:** no transcript text, hooks, titles, reasons, frames or video names in commits, tests or logs. Test videos are named `t1`, `t2`… and kept in `C:\Users\15513\Videos\clip-tests\`. Never cloud-sync clips.
- **Repo hygiene:** always `uv run`. Scratch files only in `runs/_scratch/`. **Never `uv lock --upgrade` without a brief.** Never pipe checks through tail/head without pipefail.
- **Workflow:** one brief at a time **per area of the code**. Opus plans (High for risky briefs); a Sonnet 5.5 Medium subagent builds; Opus reviews the diff and re-runs the checks. Tell Igor the model for every step. Run Claude Code in parallel with Igor's human-only tasks (listening, rating).
- **Experiments:** change one thing at a time; freeze `candidates.json` when comparing scorer versions.
- **Reply format:** a senior colleague; "Learn:" notes; label fact, judgment or estimate; status shows Build, Business, time and API spend; end with the next step and its owner; **check the clock with the time tool, don't guess** (Claude got it wrong once).

## 7. Parked
Haiku candidate variance; c06 punctuation; abbreviations counted as sentence ends; the ledger isn't locked; Actions pinned by tag; prompt caching or Batch API; Haiku vs Sonnet for scoring; deny rules aren't a hard boundary; split-screen layout for picture-in-picture/screen-share (only if the niche needs it); per-speaker camera files; the model licence [gap] (confirm before clients); telemetry measurement.

## 8. Numbers
API spend about **$0.10** of the $100 credit (the credit expires Oct 23 and renews monthly). Tests: 865. Render: about 3–4 s per 30 s clip from 360p, about 8 s from 640×360 at preset medium.

## 9. Files delivered this session (all in `docs/`)
`brief-06-render.md`, `brief-06-1-caption-offset.md`, `brief-07-report.md`, `PRODUCT.md`, `brief-08-speaker-crop.md`, `brief-09-boundary-repair.md`, `ROADMAP.md`, `research/clip-scoring-guide.md` (v1.1), `HANDOFF.md` (this file).

## 10. Session history
| Session | Date and time (ET) | What happened |
|---|---|---|
| 1 | Oct 10, 02:11–04:00 | Gate 0, setup, Brief 1 |
| 2 | Oct 10, 17:47–20:00 | Briefs 2 and 3, D-011, D-012 |
| 2b | Oct 10, ~20:00–22:00 | API key and billing; Briefs 4 and 4.1 |
| 3 | Oct 10 22:39 – Oct 11 ~01:55 | Briefs 5.1, 6, 6.1, 7; first full run (T1); listening calibration; ROADMAP; research guide v1.1; Brief 8 plan and Part A; Brief 9 written |
