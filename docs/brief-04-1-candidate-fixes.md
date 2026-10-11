# Brief 4.1 — Candidate quality fixes (from Igor's first spot check) (approved)

> **Models (D-009):** Plan with **Opus 5.5, Medium** in Plan mode (short plan). Build with **Sonnet 5.5, Medium**.
> Builds on Brief 4. Status: APPROVED by Igor when he hands it to you.
> **Privacy:** this brief, the commits and the log must contain **no transcript text or hooks** from the private test video. Report hooks only in chat.

## Evidence (Igor's spot check, rubric C1/C2/C4)
Igor checked 4 of the 8 candidates from `runs/2026-10-10_source`:
- **c05:** clean and understandable.
- **c02:** clean start and understandable, but it **ended before the answer was finished** (payoff cut: C4).
- **c03 and c07:** **started mid-sentence** and weren't understandable alone (C1 and C2 fail).

**Root causes:**
1. The snapping rule treats every Whisper segment edge as a sentence start. Whisper often breaks segments mid-sentence (4 of 8 starts and 3 of 8 ends were segment edges).
2. All 8 moments were 20–27 s, right at the minimum. The model cuts answers short.

## Changes
1. **Sentence boundaries** (`utils/snapping.py`):
   - A sentence ends only after a word ending in `. ? !` (with the existing quote and bracket handling).
   - A segment edge counts as a boundary **only as a fallback**: when more than `candidates.fallback_max_words` words (default 40) have passed since the last sentence end, or at the very first word of the transcript.
   - Keep the `segment_edge_starts` / `segment_edge_ends` counts. They should drop sharply.
2. **Prompt** (`prompts/find_moments.md`): keep the meaning of the existing text, and add:
   - "Start where a new thought starts. Never start on a continuation (and, so, but, because, which…) that depends on what came before."
   - "End only after the payoff is complete. If the moment is a question, include the full answer. If it's a story, include its ending. Prefer {preferred_min}-{preferred_max} seconds. Never cut a payoff just to stay short."
3. **Config:** `candidates.preferred_min_seconds: 30` and `candidates.preferred_max_seconds: 50`, validated: min_seconds ≤ preferred_min < preferred_max ≤ max_seconds. These are passed into the prompt. Old run configs keep loading.
4. **Snapping length rule:** when the model's range is shorter than `preferred_min_seconds`, it may grow by whole sentences toward the **end** first (the payoff side), as long as it stays ≤ `max_seconds`. Never grow backward past a sentence start into a different thought.
5. **Tests:** update and add snapping tests with synthetic words:
   - a mid-sentence segment edge is no longer a boundary
   - the fallback after 40 unpunctuated words
   - the very first word of the transcript
   - grow-toward-end
   - the config validation for the preferred range

## Acceptance
```
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
```
**Real re-run** (Igor confirms first; about $0.002):
- `uv run python -m clipmachine run runs\2026-10-10_source --from-stage candidates`
- Report in chat only:
  - the candidates table: id, mm:ss start–end, length, type, hook (max 80 characters)
  - segment-edge starts and ends, **before vs after**
  - lengths: min, median and max
  - the cost
- Then Igor spot-checks 4 new candidates. Pick the 2 with the most risky starts and the 2 longest, and give their times.

## Log
Append a short "Brief 4.1" entry to `docs/PROJECT_LOG.md`, with no transcript text. Include:
- Igor's first spot-check results in words (2 of 4 understandable; 1 payoff cut short; 2 mid-sentence starts)
- the root causes
- the before and after counts
- the cost

## Out of scope
- Scoring (Brief 5)
- Rendering and padding (handled when we can hear real clips in Brief 6)
- Abbreviation handling
