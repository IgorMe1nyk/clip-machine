# Brief R1 — Captions v2 (karaoke box) + hook overlay (approved when Igor hands it over)

> **Models (D-009):** Plan with **Opus 5.5, Medium** in Plan mode. Build with a **Sonnet 5.5, Medium** subagent. Each check runs separately.
> **Order:** after Brief 8.3 and the edit-point guard (HANDOFF section 4, as amended on 2026-10-12).
> **Privacy:** no transcript text, hooks, titles, frames or video names in any commit, test or log. Report them in chat only. Test fixtures are synthetic.
> **Cost:** $0. Local ffmpeg/libass only; no new API fields, no new dependency.

## Goal
Make clips more watchable with no extra cost or manual work:
1. **Captions v2, "B karaoke":** a short group of words on a dark rounded box. Words already spoken turn **yellow**; words not yet spoken are **white**. Words have **clearly visible spacing** between them; Igor asked that captions not feel cramped.
2. **Hook overlay:** the clip's `hook_line` as big text for the first 2 s.
3. **Safe zones:** captions and hook stay out of the areas the TikTok, Reels and Shorts apps cover.

Igor picked this style on 2026-10-12 from mockups. Style A (bold pop) and per-word key-word colouring (style C) are **out of scope**. They're kept as possible future per-client styles.

## Read first
1. `CLAUDE.md`, `docs/DECISIONS.md` (D-018 timing, D-020/D-021 crop and stacked layout), the newest `docs/PROJECT_LOG.md` entries.
2. The current caption code (word timestamps → `.ass`), the render stage, `scored.json`'s `hook_line`, the per-clip json, and their tests.
3. libass/ASS docs on karaoke tags (`\k`, `\kf`), `BorderStyle=3` (opaque box), `\fsp`, `\h`, and `PlayResX/PlayResY`. **Verify the behaviour in the docs; don't rely on memory.**

Work in Plan mode first. Show the plan, wait for Igor's OK, then implement in small commits. If anything is ambiguous, stop and ask.

## Design

### 1. Caption style "box_karaoke" (new default; the old style stays selectable)
- **Grouping:** keep the existing word groups but cap each line at `max_words` (4) and `max_width_px` (760). One line on screen at a time; never more than 2.
- **Colours:** spoken words are `#FFD400` (yellow); upcoming words are `#FFFFFF`. A word changes colour **at its own start time**.
  - Native ASS karaoke may do this (PrimaryColour = spoken, SecondaryColour = upcoming, `\k` per word). Confirm in the docs and propose it in the plan, or explain why another method is better.
- **Box:** dark, about 75% opaque, rounded if libass allows it (state it if not). Padding about 18 px around the text.
- **Spacing:** this is Igor's explicit requirement.
  - The gap between words must be clearly visible. Target about 0.45 × font size, about 2× a normal space.
  - Letter spacing stays slight (0–2 px).
  - If 2 lines are ever shown, the line gap is at least 0.35 × font size.
  - All three values live in config.
- **Font:** Montserrat (already in use), weight as close to 500–600 as the installed font allows. Size about 64 px at 1080 width (config).

### 2. Hook overlay
- **Text:** `hook_line` from `scored.json`. If it's empty or missing, show no overlay and add a note.
- **Timing:** shown from 0.0 to `hook.seconds` (2.0) with a short fade-out (about 150 ms).
- **Placement:**
  - Top of the safe zone (y ≥ `safe.top`), centred, white text on a dark band, max 2 lines, same font.
  - It must not cover a face. Check the existing face boxes for the first 2 s; if they overlap, move the hook down below the faces but still inside the safe zone.
  - In a stacked shot (8.3), place it over the top half's headroom if that fits. Otherwise put it on the seam, and the captions wait until the hook ends.
- **Untrusted input:** `hook_line` and transcript words are model or transcript output. **Escape ASS override characters** (`{`, `}`, `\`) and newlines before writing them. Test it.

### 3. Safe zones (config; strictest published numbers, checked 2026-10-12)
On a 1080×1920 frame:
- Top 290, bottom 672 (Reels and Shorts reserve the bottom 672 px), left 120, right 192.
- TikTok's button column, about 300 px wide, covers the right side from about y 840 down. A centred 760 px caption (x 160–920) overlaps it slightly. **This is a known trade-off, not a bug.** Igor will judge it on real frames.

Caption placement:
- Normal shots: the caption baseline sits **above y = 1920 − 672 = 1248**, about 1150–1230.
- Stacked shots: on the seam (y = 960), as in Brief 8.3.

Add a **local-only debug flag** (`--debug-safe-zones`, or a config switch) that draws the margins as translucent lines on the rendered clip. That way Igor can check without uploading private frames anywhere. It must be off by default and never on in delivered clips.

### 4. Config (validated, backward compatible; old run configs still load)
```yaml
captions:
  style: box_karaoke          # or: legacy
  font: Montserrat
  font_size: 64
  max_words: 4
  max_width_px: 760
  word_gap_em: 0.45
  letter_spacing_px: 1
  line_gap_em: 0.35
  spoken_color: "#FFD400"
  upcoming_color: "#FFFFFF"
  box_opacity: 0.75
  box_padding_px: 18
hook:
  enabled: true
  seconds: 2.0
  fade_ms: 150
safe:
  top: 290
  bottom: 672
  left: 120
  right: 192
```
`style: legacy` + `hook.enabled: false` must reproduce today's output (a byte-identical `.ass` on a synthetic clip).

## Tests (offline, synthetic)
- Karaoke timing: each word's colour switch matches its start time (within one centisecond, the ASS time unit)
- Grouping respects `max_words` and `max_width_px` (use a font-metrics estimate; state the method)
- Spacing values appear in the generated `.ass` as configured
- Escaping: words and hooks containing `{`, `}`, `\`, newlines and long strings
- Hook: shown 0–2 s; skipped when empty; moves down when a synthetic face box overlaps; stacked-shot placement
- Safe zones: caption and hook boxes stay inside the margins for normal and stacked layouts
- Legacy style reproduces the old `.ass`
- All existing tests still pass

## Out of scope
- Key-word colouring, emoji, style A, animated pop/scale effects
- Punch-in zooms (R2), B-roll (R3, parked)
- Changes to scoring, prompts or the hook-writing rules
- Loudness and the low-resolution warning (polish brief)

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

Then render **one clip with the safe-zone debug overlay** to a scratch path in `runs/_scratch/`, so Igor can check placement. Never put it in the clips folder.

## Report back (chat only)
- Plan summary: the karaoke method, how the box is drawn, how spacing is implemented
- Test counts and a diff summary per commit
- For each clip: caption line count, max words per line, hook shown (yes/no, moved for faces yes/no), anything placed outside the safe zone (must be none)
- Render time vs before
- **Igor's check:** play 3 clips on a phone and rate them on readability, spacing ("not cramped"), sync, and hook placement. Then re-rate all clips under the MVP rubric → the next EVAL.md row.

## Log
- `docs/PROJECT_LOG.md`: an "R1" entry with counts, render time and Igor's ratings. No transcript text.
- `docs/DECISIONS.md`:
  - **D-022**: caption style. Options: A bold pop · B box · B karaoke · B karaoke + key words · C keyword colour. Choice: **B karaoke** (spoken words yellow, wide word spacing). Why: Igor's pick from mockups, distinct from the old look. Key words are parked to avoid an extra model field and random-looking picks. Decided by Igor, 2026-10-12.
  - **D-023**: safe-zone margins. Strictest published numbers, config-driven; TikTok button-column overlap accepted for now. Claude proposed; Igor approved, 2026-10-12.
- `docs/ROADMAP.md`: add the retention pack (R1 captions and hook, R2 punch-in zooms, R3 B-roll parked/off by default) after the edit-point guard; the vocabulary brief moves before R2.
