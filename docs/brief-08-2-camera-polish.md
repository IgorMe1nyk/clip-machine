# Brief 8.2 — Camera polish: fast, correct switching and smooth motion

> **Models:** plan with **Opus 5.5, High** in Plan mode (measure first). Build with a **Sonnet 5.5 Medium subagent**; Opus reviews each diff and re-runs the checks.
> Builds on Brief 8 Part A, 8.1 and Part B. **Privacy:** no frames, face crops, transcript text or video names in commits, tests or logs; numbers only in chat. Fixtures synthetic.

## Evidence (Igor's ratings, `runs/2026-10-11_t3-2`, EVAL.md row 1: 6 usable, 2 needs edit, 0 reject)
Both "needs edit" clips fail only on the camera (C6):
1. **Switch lag in back-and-forth talk** (c09, c04): the camera cuts to the new speaker late; sometimes the other person has already stopped talking.
2. **Missed short replies** (c04, c13): a quick "no" or response from the other person doesn't get the camera.
3. **Laughs/chuckles steal the camera** (earlier c17): mouth movement without speech wins.
4. **Robotic motion** (c07, c15 earlier): stop–jump–stop instead of glides.
5. **Turned-away speakers** (8.1 limit): landmarks unreadable for 30–50% of faces in some wide shots, so the speaker can't win.

## Goal
The camera shows **whoever is actually speaking, promptly**, and **moves like a human operator** (mostly still, smooth glides). Then make `video.crop: speaker` the **default**, so one command produces finished clips.

## Part 1 — Who is speaking (measure first, then choose)
Build a per-window speaker signal from what we already have, and compare on T3 (numbers only):
- **(a) Today:** mouth-openness variance plus hysteresis.
- **(b) Audio-visual sync:** correlate each face's mouth-openness series with the **audio loudness envelope** of `audio.wav` over short windows (~0.3–0.5 s). The speaker's lips move *with* the sound; a chuckle or a smile doesn't. This should fix laughs and help short replies.
- **(c) Pixel motion around the mouth** (measured in 8.1 as a candidate): the fallback when landmarks are unreadable (turned-away faces).
- **(d)** combinations, e.g. (b) where landmarks exist, (c) where they don't.

**Evaluation without hand labels:** for the T3-2 kept clips, report per clip: the number of speaker turns detected, **switch latency** (time from the first speech window of the new speaker to the camera cut, median and max), switches during a single speaker's talk (false switches), and the share of speech windows where no face could be scored. Recommend one option with numbers.

## Part 2 — Switching rules (offline advantage)
We process the whole clip in advance, so the cut can sit **at the start of the new speaker's turn**, not after the evidence builds up.
- Place each switch at the **turn start** (backdated to the first window of the new speaker's speech), not at the confirmation time.
- Re-tune `window_seconds`, `confirm_windows`, `switch_ratio` and `min_shot_seconds` on T3 with the chosen signal.
- **Short replies, a decision for Igor in the plan:** cut to an interjection only if it lasts at least `speaker.min_turn_seconds` (proposal: ~0.6 s, so a clear "No." gets a cut but "mm-hm" doesn't). Measure how many cuts each value would add on T3.
- A **laugh without speech** (mouth moving, no correlated audio) must never take the camera.

## Part 3 — Operator-style motion
- **Mostly still:** within a shot, hold the crop while the face stays inside a comfortable central zone (larger dead zone).
- **Glide, don't jump:** when the face nears the edge, ease to the new position (smooth start and stop, ~0.4–0.8 s), with a speed cap.
- **Offline smoothing:** smooth the face path with a centred (zero-lag) filter before planning moves, so the camera starts gliding as the person starts moving, not after.
- Hard cuts stay hard: speaker switches and camera cuts are instant; only moves *within* a shot glide.
- Report crop changes per clip before and after (c07-style clips had 100+).

## Part 4 — Default to speaker crop
After Igor passes the checkpoint: `video.crop` default `speaker` (center fallback unchanged), so `uv run python -m clipmachine run <video> --client <name>` gives finished clips with no manual step. Old run folders keep their frozen config.

## Tests (offline, synthetic)
- AV sync: synthetic mouth series correlated vs uncorrelated with a synthetic loudness envelope; a "laugh" (movement, no audio match) never wins.
- Turn-start placement; min_turn_seconds; no false switch during one speaker's turn.
- Motion: dead zone holds; glide shape (eased, speed-capped, no overshoot); zero-lag smoothing; hard cuts untouched.
- Pixel-motion fallback used only when landmarks are missing.
- Existing camera, track and render tests still pass; centre mode byte-for-byte unchanged.

## Acceptance
```
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run pip-audit --skip-editable
```
**Real run** (ask Igor first; local, $0): re-track and re-render `runs/2026-10-11_t3-2` (`track --preview`, then `--from-stage render`). Report the Part 1 metrics before and after, plus crop changes per clip.
**Igor re-watches c09 and c04 first** (the two needs-edit clips), then the rest. **Pass:** the camera on the speaker promptly in back-and-forth talk, no laugh steals, glides instead of jumps. Then Part 4.

## Out of scope
Framing width (parked), diarization (pyannote), split-screen layouts, per-speaker camera files.

## Docs
D-019 (speaker signal chosen, with numbers), D-020 (default crop = speaker, after the pass), PROJECT_LOG entries (numbers only), EVAL.md row 2 after Igor's re-rating. Don't push.
