# Brief 9 — Boundary repair: let the judge extend clips, not just trim them

> **Models:** plan with **Opus 5.5, Medium** in Plan mode (short plan). Build with a **Sonnet 5.5 Medium subagent**; Opus reviews the diff and runs the checks.
> **Starts only after Brief 8 Part A is committed** (both touch `models.py` and `default.yaml`).
> **Privacy:** no transcript text, hooks, titles or reasons from test videos in any commit, test or log. Fixtures are synthetic.

## Evidence (35-min run `runs/2026-10-11_t1`, Igor listened 2026-10-11)
- 19 candidates; **2 kept**, both rated good on content by Igor (judge precision is high).
- 17 rejected, **13 as `needs_context`**. Igor checked the 5 score-6 rejects:
  - c08: **fine** → a false reject
  - c03: content fine, but the **end cuts mid-sentence**
  - c19: good start, but the **end stops before the thought finishes**
  - c06 and c17: the **start is mid-thought** (the setup is missing)
- **Root cause (judgment):** the boundaries are wrong, not the content. `keep_sentences` can only shrink a clip, so the judge can't repair a missing setup or an unfinished ending; it rejects instead.

## Goal
Let the score stage **move a clip's start earlier or its end later** (and still trim), so clips start on their setup and end on a finished thought. Recall should rise without lowering the bar.

## Changes
1. **Wider numbered context** in the score prompt, per candidate: up to `score.extend_before` sentences before (default 4) and `score.extend_after` after (default 3), using the same sentence rules as `utils/snapping.py`. Number them continuously around the clip: before-sentences get numbers ≤ 0 (… −1, 0), the clip is 1..n, after-sentences are n+1… All of it is still untrusted data.
2. **Answer schema:** `keep_sentences {first, last}` may now range from the lowest before-number to the highest after-number. The new start and end come from those sentences' word times plus the existing padding. The result must stay within `clip_length` min and max; otherwise the change is ignored and recorded (as today's `trim_ignored` logic, now named e.g. `adjust_ignored`, staying backward compatible in `scored.json`).
3. **Prompt rule (score_clip.md):** keep the existing meaning, and replace "trim first" with **"repair first, reject second"**: if the clip starts mid-thought, start earlier at the setup (often the host's question); if it ends before the thought is finished, end later at the conclusion; if the first or last sentence is weak, trim it. Score and judge the **repaired** clip. Reject only when no repair within the length limit fixes it.
4. **Candidate prompt (find_moments.md):** add one rule: "Start each moment at its setup (the question or the first sentence of the thought) and end it where the point is finished." No other change to candidate logic.
5. **`scored.json`:** record per clip `extended_start`, `extended_end` (sentence counts) next to the existing trim fields, plus summary counts `extensions_applied` and `adjust_ignored`. Old files still load.
6. **Caption offset default:** set `captions.offset_seconds: 0.15` in `default.yaml` and the model default (confirmed by eye on two videos).

## Tests (offline, synthetic)
- Numbering: before ≤ 0, clip 1..n, after n+1…; clipped at transcript start and end; never overlapping another sentence twice.
- Mapping: an extension backwards, forwards, both; trim plus extend; out-of-range numbers; a result outside `clip_length` is ignored and recorded.
- The prompt contains the repair rule and the numbered context.
- Old `scored.json` and old configs load; the offset default is 0.15.
- The existing score tests still pass.

## Acceptance
```
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run pip-audit --skip-editable
```
**Real run** (ask Igor first; expected cost about $0.05): **freeze the candidates** so the comparison is fair. Copy `runs/2026-10-11_t1/scored.json` to `runs/_scratch/t1-scored-before.json`, then run
`uv run python -m clipmachine run runs/2026-10-11_t1 --from-stage score`
(this keeps the existing `candidates.json`; do NOT rerun candidates).

## Report back (chat only)
- A before/after table for the same 19 candidates: id, score, verdict, reason, final mm:ss start–end, extension/trim applied.
- **Specifically c03, c06, c08, c17, c19** (Igor's verdicts) and c09, c15 (kept before).
- Kept count before vs after; cost.
- No target number of kept clips: don't lower `min_score` or loosen the bar to get more.

## Out of scope
Changes to how candidates are chosen beyond the one prompt line; the vocabulary fix for names (separate brief); rendering changes.

## Log
A "Brief 9" entry in `docs/PROJECT_LOG.md` (counts, cost, the before/after summary, no transcript text), committed with this brief. Don't push.
