# Brief 8.1 — Detect faces in wide shots, and reset the camera at shot cuts

> **Models:** plan with **Opus 5.5, High** in Plan mode (research and measurement needed). Build with a **Sonnet 5.5 Medium subagent**; Opus reviews the diff and re-runs the checks.
> Builds on Brief 8 Part A. **Part B waits until this brief passes its checkpoint.**
> **Privacy:** no frames, face crops, transcript text or video names in commits, tests or logs. Measurements on the private T3 run are reported in chat only, as numbers. Fixtures stay synthetic.

## Evidence (checkpoint on `runs/2026-10-11_t3`, 1080p two-person studio show, Igor watched all 6 previews)
| Clip | face_time | Igor's finding |
|---|---|---|
| c09 | 96% | Close-up the whole clip: face found, crop follows movement ✅ |
| c05, c06, c13, c17 | 14–45% | **Close-ups work; wide two-person shots: no faces detected** |
| c15 | 0% | **Wide shot the whole clip, speaker facing the camera, never detected** |

1. **Small faces are missed.** In wide shots faces are roughly 8–10% of frame height. Judgment: the landmarker's built-in detector (BlazeFace short-range) works on a downscaled whole frame, so these faces become too small. Faces facing the camera are missed too, so it's size, not angle.
2. **Stale position after camera cuts.** "No face → keep last position" carries a close-up's crop x into the next wide shot, landing between people or on half a face.
3. **Speaker switching is still untested**, because two faces are never detected at the same time.

## Goal
In wide shots, detect both faces reliably, so the speaker logic can work; and make the camera **reset at shot cuts** instead of carrying a position across them. No regression on close-ups.

## Part 1 — Small-face detection (research, measure, then choose)
Compare at least these options **on real T3 frames**, reporting numbers only:
- **(a) Tiling:** run the existing landmarker on overlapping crops of the frame (sized so a wide-shot face becomes large enough), then merge.
- **(b) A small-face detector plus the landmarker on face crops:** e.g. OpenCV's YuNet (`cv2.FaceDetectorYN`; opencv-contrib is already installed via mediapipe) finds faces at full resolution, then the Face Landmarker runs on each face's crop for mouth movement.
- **(c) A MediaPipe full-range face detector,** if one is officially available for Python Tasks (check the live docs; don't assume).

**Plan must answer:** for each option, the detection rate on wide-shot samples from c15 and c17, close-up rate (must stay ~100%), CPU time per sample, any new model file (official URL, size, licence, SHA pin) and whether a new **direct** dependency is needed. Recommend one, with numbers. **Ask before any download.**

## Part 2 — Shot-cut awareness
- **Detect cuts** from the frames we already decode (e.g. a frame-difference or histogram change between consecutive samples, or ffmpeg's scene score). Record cut times in `track.json`.
- **At a cut:** end the current shot and drop old tracks. **Never carry crop x across a cut.** Choose fresh: the active speaker if known; otherwise the largest/most central face; otherwise centre.
- The minimum shot length and hysteresis apply **within** a camera shot; a camera cut may move the crop immediately (the edit already cut).
- In a wide shot with two faces and speech, choose by mouth movement as designed. This is the first real test of speaker switching.

## Tests (offline, synthetic)
- Cut detection on synthetic frame sequences (hard cut, gradual change that is not a cut, flicker).
- No crop x carried across a cut; fresh choice rules (speaker → largest face → centre).
- Tiling or crop merging: faces split across tile edges are merged once; boxes mapped back to full-frame coordinates correctly.
- Existing camera and track tests still pass.

## Acceptance
```
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run pip-audit --skip-editable
```
**Real run** (ask Igor first; local, $0): `uv run python -m clipmachine track runs/2026-10-11_t3 --preview`.
Report per clip: face_time %, faces per sample (mean), cuts detected, switches, analysis seconds. **Targets:** c15 face_time ≥ 80%; c09 stays ≥ 90%; cuts detected in c05/c06/c13/c17.
**Then Igor re-watches** c15 and c17 first. **Checkpoint pass:** the right person ≥ 90% of the time, no crop left on empty space after a cut, no flicker.

## Out of scope
The moving-crop render (Part B), framing width (parked: arms cut at the edges; judge on finished clips), split-screen layouts, diarization.

## Docs
- `docs/DECISIONS.md`: **D-016**, the small-face approach chosen, with the measured numbers.
- `docs/COSTS.md`: rows for any new model file or direct dependency.
- `docs/PROJECT_LOG.md`: a Brief 8.1 entry (numbers only), and add to the parked list: "framing width: 9:16 crop cuts gesturing arms; Igor: not too bad; judge on finished clips (zoom-out + blurred fill vs tighter face framing)".
Don't push.
