# Brief 8.3 — Stacked layout for wide two-person shots (approved when Igor hands it over)

> **Models (D-009):** Plan with **Opus 5.5, High** in Plan mode (render changes are risky). Build with a **Sonnet 5.5, Medium** subagent. Each check runs separately.
> Builds on Briefs 8, 8 Part B, 8.1 and 8.2 (YuNet faces D-016, cut detection, `sendcmd` moving crop, operator smoothing, `video.crop: speaker` D-020).
> **Privacy:** no transcript text, hooks, titles, frames or video names in any commit, test or log. Report them in chat only. Test fixtures are synthetic.

## Goal
In a **wide shot with two people**, stop choosing who to show. Show **both**, stacked top and bottom, each half filling 1080×960, with no borders. Close-ups keep today's speaker crop.

**Why:** lip reading fails on people turned sideways, and the `mix` signal failed the held-out test with false cuts (D-019). Stacking removes most switching decisions and gives reaction shots for free. [judgment]

## Read first
1. `CLAUDE.md`, `docs/DECISIONS.md` (D-016, D-019, D-020), the newest `docs/PROJECT_LOG.md` entries (8.1, 8 Part B, 8.2), `HANDOFF.md` section 3 item 1.
2. The current crop and render code: speaker crop, cut detection, the `sendcmd` crop path, the caption `.ass` writer, and their tests. **Reuse the face detections, cut list and smoothing as they are.**

Work in Plan mode first. Show the plan, wait for Igor's OK, then implement in small commits. If anything below is ambiguous, stop and ask; don't decide product behaviour yourself.

## Design (approved defaults, Igor 2026-10-12)

### 1. When to stack: one decision per shot
- A **shot** is the span between two detected camera cuts (the existing cut list).
- A shot is **stacked** when:
  - it lasts **≥ `min_seconds` (2.0)**, and
  - exactly 2 faces are detected in at least **`two_face_coverage` (0.7)** of its sampled frames.
- Otherwise the shot uses the existing speaker crop. This includes 1 face, 3+ faces, and shots under 2 s.
- **The layout never changes inside a shot.** Changes happen only at real cuts, frame-exact, as a hard cut.

### 2. Framing each half
- **Order is fixed by screen position:** the face whose centre is further left in the source goes on **top**, for the whole shot. No active-speaker logic here.
- Each half is a 9:8 crop region of the source, scaled to 1080×960.
- Crop size is chosen so each face fills about **`face_height_frac` (0.35)** of its half's height. Both halves use the same target, so the faces look the same size.
- Each half follows its own face with the existing operator smoothing, clamped inside the source frame.
- Crop regions may overlap if people sit close together; that's allowed. Log it as a note.

### 3. Captions
- Captions in a stacked shot are centred **on the seam** (y = 960).
- Each caption line takes its position from the layout at its start time. Speaker-crop shots keep today's position.
- Report how often a caption line spans a layout change. That's a known judgment call, so Igor can see it.

### 4. Upscale check (measure, don't assume)
- For every stacked shot, log the **upscale factor**: output half height ÷ source crop height.
- A shot whose factor is above `max_upscale` (2.0) still renders. Add a note to the clip's json and to the report.

### 5. Config (validated, backward compatible; old run configs still load)
```yaml
video:
  stack:
    enabled: true
    min_seconds: 2.0
    two_face_coverage: 0.7
    face_height_frac: 0.35
    max_upscale: 2.0
```
- `enabled: false` must give byte-identical behaviour to today's speaker crop.

### 6. Rendering constraints
- One encode generation; no re-encoding of already-encoded clips.
- Audio untouched, including the 80 ms fades (D-018).
- Frame-exact at cuts, same as 8 Part B. ffmpeg is called with argument lists only.
- Choose the filter-graph approach in the plan and explain it. For example: split → two `sendcmd` crops → scale → `vstack`, switched per shot. Don't build anything yet.
- Each clip's json gets a per-shot list: start, end, layout (`stack` or `speaker`), upscale factor, notes.

## Tests (offline, synthetic)
- Shot layout decision: coverage just above and just below 0.7; a shot exactly at 2.0 s and one just under; 1, 2 and 3 faces
- Left/right → top/bottom assignment, including faces that cross mid-shot (order stays fixed by the shot's first decision; note it)
- Crop geometry: 9:8 ratio, same face fraction in both halves, clamped at frame edges, overlap allowed and noted
- Caption y per layout, and the "line spans a layout change" count
- Upscale factor maths and the `max_upscale` note
- Config validation; an old config loads; `enabled: false` matches today's output on a synthetic clip
- No layout change inside a shot (a property test over random shot lists)
- All existing tests still pass

## Out of scope
- 3+ people layouts, active-speaker highlighting in stacked mode, animated transitions
- Edit-point guard, vocabulary/hotwords, scoring rubric (separate briefs)
- Changing the speaker signal (`mouth` stays, D-019)
- Loudness, safe zones, the low-resolution warning in the UI (polish brief)

## Acceptance
Run each separately:
```
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run pip-audit --skip-editable
```
**Real run** (ask Igor first; $0 API, render only):
`uv run python -m clipmachine run runs/2026-10-12_t3 --from-stage render`

**Held-out check:**
- Compare stacked shots with Igor's hand labels in the held-out window (the D-019 window).
- If those labels don't mark wide two-person spans, stop and ask Igor to label that window first. Don't label it yourself.
- Target: **0 layout changes inside a shot**, and no stacked shot where the hand labels show one person.

## Report back (chat only)
- Plan summary and the filter-graph choice
- Test output (counts) and a diff summary per commit
- Per clip, a table: shots, stacked shots, layout changes inside a shot (must be 0), max upscale, overlap notes, caption lines spanning a layout change
- Render time vs before (~230 s for 6 clips), and per-run disk use
- Held-out result vs hand labels
- **The 2 clips with the most stacked time,** with mm:ss spans, for Igor to rate for sharpness and framing. Then Igor re-rates all clips under the MVP rubric → EVAL.md row 3.

## Log
- `docs/PROJECT_LOG.md`: a "Brief 8.3" entry with counts, render time and held-out result. No transcript text.
- `docs/DECISIONS.md`:
  - **D-021**: stacked layout for wide two-shots. Options: smarter speaker signal · always speaker crop · stack. Choice: stack, per shot, with the defaults above. Claude proposed; Igor approved; 2026-10-12.
  - **D-006 update**: third-party videos for private validation only; public proof only from permissioned creator content. Igor confirmed 2026-10-12.
