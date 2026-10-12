# Brief 10 — Real sentence boundaries and clean cut edges

> **Models:** plan with **Opus 5.5, High** in Plan mode (measurement first). Build with a **Sonnet 5.5 Medium subagent**; Opus reviews each diff and re-runs the checks.
> **Privacy:** no transcript text, hooks, titles or video names in commits, tests or logs. Text may appear in chat only. Fixtures are synthetic.

## Evidence (T3 finished-clip ratings + diagnosis, 2026-10-11)
- Igor rated 7 finished T3 clips: **c09, c15 usable**; c07 needs camera fixes (Brief 8.2); **c05, c11, c12, c13 fail on cut points** (mid-word, mid-sentence, mid-thought).
- **Root cause (measured):** long stretches of the transcript have **no punctuation**. 59 of 537 "sentences" end at a Whisper segment edge after 40+ words; they hold **36% of all words**. These fallback edges sit mid-sentence with **0.00 s pause**. The four failing clips are exactly the four kept clips with a fallback-edge boundary.
- Secondary causes: the judge can't see which sentences end at a fallback edge, and the code doesn't check where an adjusted boundary lands (c11's own trim moved its end onto one). The judge once described a trim without supplying a range (c12). Some thoughts need about 70 s to finish (c05, c13), over our 60 s max.

## Goal
Every clip starts and ends at a **real sentence end in a real pause**, with no mid-word, mid-sentence or mid-thought cuts, and no audible click.

## Part 1 — Better punctuation at the source (measure first, then choose)
Compare on **T1 and T3** (re-transcribe locally on GPU, about 1–2 min each; numbers only in chat):
- (a) today: `small`, `condition_on_previous_text: false`
- (b) a **punctuated `initial_prompt`** (a short, neutral, well-punctuated sentence or two; Whisper tends to copy the prompt's style)
- (c) `condition_on_previous_text: true` (watch for repetition loops; report any)
- (d) the `turbo` model (D-011 noted it dropped punctuation in one stretch; measure again)
- combinations if useful

**Metric:** % of words inside runs longer than 40 words without a sentence end; number of fallback edges; transcription time; any hallucinated or repeated text (count only). Recommend one with numbers. It becomes a config change plus a decision record (D-017).

## Part 2 — Pause-aware boundaries (deterministic, free)
In `utils/snapping.py`:
- A **boundary candidate** is a sentence end (punctuation) **or** a pause ≥ `snap.min_pause_seconds` (default about 0.35 s; tune on T1/T3).
- In a run with no punctuation, split at the **largest pauses**, not at Whisper segment edges.
- **Never place a clip start or end where the gap between words is under `snap.min_cut_gap` (about 0.15 s).** This is a hard rule, applied to the snapped *and* the judge-adjusted boundaries. An adjusted boundary that breaks it is ignored and recorded (as `adjust_ignored`).
- Mark each numbered sentence shown to the judge with how it ends: `.` / `?` / `!` = real end; `pause` = pause boundary; `edge` = weak. The prompt says: prefer real ends, never end on a weak one.

## Part 3 — Clean edges in the render
- **End inside the pause:** move the cut to the middle of the silence after the last word (capped at about 0.5 s after the word), not 0.2 s after the word's timestamp. The same for starts (cut in the middle of the pause before the first word, capped).
- **Audio fade:** about 80 ms fade-in and fade-out, so a tight cut never clicks.

## Part 4 — Length limit (Igor decides when approving)
Some complete thoughts need about 70 s. Shorts accept up to 3 minutes (research guide). **Proposal:** `clip.max_seconds` 60 → **90**, keep the judge's "prefer the shortest complete version" rule, and keep the client's `clip_length` able to override.

## Tests (offline, synthetic)
- Boundary candidates from punctuation and pauses; long unpunctuated runs split at the largest pauses.
- The min-cut-gap rule rejects boundaries inside continuous speech, for snapped and adjusted boundaries.
- Sentence end-type labels reach the prompt.
- Pause-centred cut times; capped; fades present in the ffmpeg arguments.
- Old configs and old `scored.json` files load.

## Acceptance
```
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run pip-audit --skip-editable
```
**Real run** (ask Igor first; about $0.05–0.10 each): re-run T3 and T1 from `transcribe` (the transcript changes, so candidates can't be frozen). Report: % words in long unpunctuated runs before and after, fallback edges before and after, kept clips with start/end type (`.` / `pause` / `edge`) and the gap at each cut, and cost. **Target: no kept clip starts or ends on an `edge`, or on a gap under min_cut_gap.**
Igor re-rates the T3 clips (C1 especially).

## Out of scope
Camera motion and switching (Brief 8.2), the vocabulary fix for names (separate brief, though Part 1's prompt may later carry client vocabulary), the scoring rubric (Brief 9.1).

## Docs
D-017 (the punctuation approach, with numbers), D-018 (max clip length, if changed), and a PROJECT_LOG entry (numbers only). Don't push.
