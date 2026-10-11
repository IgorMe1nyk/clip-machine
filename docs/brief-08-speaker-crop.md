# Brief 8 — Speaker-following crop (the camera follows whoever is talking)

> **Models:** plan with **Opus 5.5, High** in Plan mode (new dependency, vision math, Windows; mistakes are expensive here). Build with a **Sonnet 5.5 Medium subagent**, and Opus reviews the diff and runs the checks, as in Brief 7.
> Builds on Briefs 6 and 7. Status: APPROVED by Igor when he hands it to you.
> **Privacy:** no transcript text, frames, face crops or file names from test videos in any commit, test or log. Test fixtures are synthetic (generated shapes or images we own). Debug previews stay in the run folder (git-ignored).
> **New dependency:** `mediapipe` (Apache-2.0) plus its Face Landmarker model file. **Igor has approved adding it in principle;** confirm the exact version pin and the model source in the plan, and ask before any download.

## Goal
Replace the fixed centre crop with a **moving 9:16 crop that keeps the active speaker in frame**: it follows small movements smoothly and **cuts** to the other person when the speaker changes. If tracking fails, fall back to today's centre crop. No regression for single-speaker video.

## Read first
`CLAUDE.md`, `docs/MVP.md` (C6), `docs/DECISIONS.md` (D-013), `docs/COSTS.md`, `utils/crop.py`, `utils/ffmpeg.py`, `stages/render.py`, `models.py`, and the live MediaPipe Face Landmarker Python guide (VIDEO running mode, `num_faces`, landmark output, model download).

Work in Plan mode first. The plan must answer the questions in "Plan must answer".

## Part A — Analysis (stop at the checkpoint)
1. **Frames:** decode only the clip's time range at about 5 fps, downscaled (e.g. 640 px wide), via an ffmpeg pipe into numpy. **Avoid adding OpenCV** unless the plan shows it's needed.
2. **Faces:** Face Landmarker in VIDEO mode, `num_faces` up to 4. Per sample: each face's box (normalised), its size, and a **mouth-openness** value from the lip landmarks, normalised by face size.
3. **Identity:** link faces across samples into tracks by position (nearest centre within a threshold). Simple: no face recognition.
4. **Active speaker:** per short window (~0.5 s), the track with the highest mouth-movement variance, **counting only windows where the transcript has words** (silence = keep the current shot).
5. **Camera path (pure Python, fully tested):**
   - A shot = one tracked person. Minimum shot length about 1.5 s (no flicker). Switch on a confident change only (hysteresis).
   - Within a shot, follow the face centre with smoothing and a dead zone (no jitter on small movements). The crop is clamped to the frame.
   - No face → keep the last position; no face for the whole clip → centre crop.
   - **Faces too small** (e.g. picture-in-picture webcam boxes) → flag it and fall back to centre crop. Layouts are out of scope.
6. **Write `clips/<id>.track.json`:** the shots, per-sample crop x (in source pixels), the fallback reason if any, and summary numbers (faces seen, switches, % of time with a face).
7. **Debug preview** (opt-in flag or config): a low-res MP4 of the source with face boxes, the active speaker highlighted, and the chosen 9:16 window drawn as a rectangle. Saved in the run folder only.
8. **CHECKPOINT:** stop and report. Igor watches the debug preview on a two-person studio-style test video before Part B starts.

## Part B — Moving-crop render (only after Igor's OK)
1. Turn the camera path into a per-frame crop at the clip's real frame rate (interpolate within shots, hard cuts between shots).
2. Render: apply the moving crop, then scale to `video.width/height` and burn in the captions as today. Choose the approach in the plan (e.g. ffmpeg `crop` with time expressions or `sendcmd`, or decode → crop in Python → encode via pipes). Prefer what's simplest to test and stays correct on Windows.
3. **Config:** `video.crop: center | speaker` (default stays `center` until Igor approves after a real run). The resume-validity check includes the crop mode and the track file's hash.
4. Single-speaker or no-face clips must look the same as, or better than, centre crop.

## Plan must answer
1. The exact `mediapipe` version pin, wheel size, and whether it installs cleanly with our lockfile on Windows and Ubuntu CI.
2. The model file: name, official URL, size, licence, where it's stored (inside the repo's `models/`, like Whisper), and how it's verified.
3. **Telemetry:** MediaPipe Tasks send usage metrics to Google. Can it be disabled? How? If not, say so plainly; it goes in COSTS.md and the privacy section.
4. The render approach for Part B and why.
5. Speed estimate for a 60 s clip on Igor's laptop (CPU), Part A and Part B.
6. The file and function list, and the commit plan.

## Tests (offline, synthetic)
- Camera path: min shot length, hysteresis, dead zone, smoothing, clamping, no-face hold, whole-clip fallback, small-face fallback.
- Track linking: two synthetic faces crossing, one disappearing and returning.
- Active-speaker choice from synthetic mouth-openness series; silent windows keep the current shot.
- Config: `crop: center` behaves exactly as today; an old config loads.
- One opt-in marker test (e.g. `mediapipe`) that runs the landmarker on a generated or owned image, never a third-party face.

## Out of scope
Split-screen and picture-in-picture layouts, face recognition, diarization (pyannote), per-speaker camera files, zoom effects, the UI.

## Acceptance
```
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run pip-audit --skip-editable
```
**Real run** (ask Igor first; local, no API cost) on a two-person studio-style test video Igor provides:
Part A → the debug preview for each kept clip; Igor checks the speaker choice. Part B → re-render with `crop: speaker` set in that run's `config.resolved.yaml`; Igor rates C6.

## Docs
- `docs/COSTS.md`: a mediapipe row (licence, size, telemetry finding) and a model-file row.
- `docs/DECISIONS.md`: **D-014** (reframing approach: MediaPipe Face Landmarker + lip-movement speaker choice + cut-based camera, vs pyannote diarization vs ClipsAI; why; decided by Igor).
- `docs/PROJECT_LOG.md`: Brief 8 entries for Part A and Part B, with no transcript text.
Don't push.
