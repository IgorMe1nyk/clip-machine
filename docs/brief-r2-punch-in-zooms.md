# Brief R2 — Punch-in zooms (approved when Igor hands it over)

> **Models (D-009):** Plan with **Opus 5.5, Medium** in Plan mode. Build with a **Sonnet 5.5, Medium** subagent.
> **Order:** after Brief 11 (campaign mode). Before 8.3.
> **Privacy:** synthetic test fixtures only; no transcript text, hooks or frames in commits.
> **Cost:** $0. Deterministic and local. **No AI picks the zoom points** (campaign Rule 3 bans AI edits).

## Goal
Make talking-head clips feel edited: **hard punch-ins** (an instant jump to a tighter crop) on some sentence starts, then back out. It's the classic podcast-clip style, and it costs nothing because it reuses the moving-crop (`sendcmd`) path.

## Read first
`CLAUDE.md`, D-016, D-019, D-020, the speaker crop and `sendcmd` code (Brief 8 Part B), operator smoothing (8.2), R1's caption and hook overlay code, Brief 11's campaign profile, and their tests.

Plan mode first; show the plan and wait for Igor's OK.

## Design
1. **When to punch in** (all deterministic, from data we already have):
   - **Candidates:** sentence starts (the existing sentence boundaries) **within the same camera shot**.
   - **Pick:**
     - the sentence containing the clip's **first spoken line** (the hook moment), and
     - sentences whose loudness (RMS of the audio, computed locally) is in the **top `loud_pct` (30%)** of the clip.
   - **Spacing:** at least **`min_gap_s` (4.0 s)** between zoom changes. Never within 0.5 s of a camera cut. Never in a stacked shot once 8.3 exists (leave a TODO hook for it).
   - **Pattern:** alternate in and out. A zoom-in lasts until the next selected sentence start or the **max hold, `max_hold_s` (6.0 s)**, whichever comes first, then cuts back out.
2. **How:**
   - Scale the existing crop window by **`scale` (1.12)** around the current face centre. Clamp it inside the source frame.
   - It's a hard cut, with no animation in v1. Operator smoothing continues inside each state.
3. **Overlays are untouched:** captions, hook and watermark draw after the crop, so they never zoom.
4. **Quality guard:**
   - Compute the upscale factor with the zoom applied. If it would exceed **`max_upscale` (2.0)**, skip that zoom and add a note.
   - Log the zoom count per clip.
5. **Config** (validated, backward compatible; old configs still load; `enabled: false` gives byte-identical output to today):
```yaml
video:
  zoom:
    enabled: true
    scale: 1.12
    loud_pct: 0.30
    min_gap_s: 4.0
    max_hold_s: 6.0
```
   A campaign profile (Brief 11) may override `video.zoom`.

## Tests (offline, synthetic)
- Selection: the first-line rule, the loudness percentile, min gap, the no-zoom-near-cuts rule, the max hold
- Geometry: scale around the face centre, clamped at frame edges, the upscale-guard skip
- The generated `sendcmd` commands match the expected crop windows and times (frame-exact at cuts, as in 8 Part B)
- Overlays are not scaled (filter order)
- `enabled: false` matches today's output
- All existing tests still pass

## Out of scope
Animated or ease zooms, shake, speed ramps, zooming in stacked shots, model-chosen emphasis words.

## Acceptance
Run each separately:
```
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run pip-audit --skip-editable
```
**Real run** (ask Igor first; $0 API): re-render the Brief 11 campaign clips with zoom on.

## Report back (chat only)
- Plan summary, test counts, and a diff summary
- Per clip: zoom count, times, skipped zooms (with the reason), render time vs before
- **Igor's check:** watch on a phone. Do the punch-ins feel natural, or too frequent? Tune `min_gap_s` and `scale` by config if needed.

## Log
- `docs/PROJECT_LOG.md` entry
- `docs/DECISIONS.md` **D-027**: punch-in zoom rules. Deterministic sentence + loudness rule; no AI-chosen emphasis because of campaign Rule 3. Claude proposed; Igor approved; date.
