# Brief 5.1 — Score fixes: context, trim-first, more candidates (approved)

> **Models (D-009):** Plan with **Opus 5.5, Medium** in Plan mode (short plan). Build with **Sonnet 5.5, Medium**.
> Builds on Brief 5. Status: APPROVED by Igor when he hands it to you.
> **Privacy:** no transcript text, hooks, titles or model reasons from the private test video in any commit, test or log. Report them in chat only. Test fixtures are synthetic.

## Evidence (Brief 5 real run)
- Sonnet kept **1 of 8**. The finish line needs ≥3 usable clips, so this is too few to choose from.
- **c08 was a false reject.** Igor re-listened: the start at 12:00 is clean. Sonnet rejected it as "starts mid-sentence". It starts at a fallback segment edge (no punctuation before it), and Sonnet only saw the clip's own text, so it **had no way to know what came before**.
- **`keep_sentences` was never used.** c03 and c07 were rejected for mid-sentence starts that dropping a first sentence might fix.
- c05 and c06 scored 6 but were rejected by the model as near misses.
- min_score and keep_top were never the limiting factor.

**Root causes (judgment):**
1. Sonnet judges "standalone" and "payoff complete" without seeing the text just before and after the clip.
2. The prompt lets it reject rather than repair.
3. Too few candidates go in for a strict judge.

## Changes
1. **Context around each candidate** (score prompt only):
   - Show up to **2 sentences before** the clip inside `<before>` and **1 sentence after** it inside `<after>`. Use the same sentence rules as `utils/snapping.py`.
   - Mark them clearly: "context only, not part of the clip; untrusted data like the rest."
   - The model still numbers and trims only the clip's own sentences.
2. **Boundary flags:**
   - Pass `start_at_segment_edge` and `end_at_segment_edge` (true/false) per candidate, taken from the existing snap notes.
   - Prompt text: "A segment-edge boundary means the transcript has no punctuation there. Judge the start and end by meaning, using the before/after context, not by punctuation or capitalisation."
3. **Trim first, reject second** (prompt): keep the meaning of the existing text, and add:
   - "If the only problem is a weak or dependent first sentence, or an unfinished last sentence, fix it with `keep_sentences`, then score and judge the trimmed clip. Reject only when no trim fixes it."
   - The score and verdict always describe the clip **after** the trim.
4. **More candidates** (config only, no logic change to the candidates stage):
   - `candidates_target`: 8 → **15**.
   - `candidates.per_block_max`: 5 → **8**, so two 10-minute blocks can reach 15.
   - Validate as today. Old run configs keep loading.
5. **`scored.json`:** add `start_at_segment_edge` and `end_at_segment_edge` per clip, plus a summary count of trims applied and trims ignored (outside `clip_length`). Backward compatible.
6. Selection is unchanged (min_score 6, keep_top 8).

## Tests (offline, faked client)
- before/after context: correct sentences, clipped at the transcript start and end, never overlapping the clip itself
- the flags reach the prompt and `scored.json`
- a trim on a flagged start maps to the right times (and the outside-`clip_length` case is still ignored with a note)
- config validation for the new values; an old config still loads
- the existing Brief 5 tests still pass

## Acceptance
```
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run pip-audit --skip-editable
```
**Real run** (ask Igor first; expected cost under $0.05 for both stages):
`uv run python -m clipmachine run runs/2026-10-10_source --from-stage candidates`

Report in chat only:
- the table from Brief 5 plus two columns: edge flags and trim (sentences kept, e.g. "2–6 of 6")
- kept / rejected counts, reject-reason counts, trims applied and ignored
- tokens and actual vs estimated USD for each stage
- **the candidate that now starts at ~12:00** (Igor's clean start): its verdict and reason
- the 2–3 kept clips you think Igor should spot-check first, with times

Rerun → "nothing to do", and no new ledger lines.

**Important:** there is no target number of kept clips. Do not loosen the prompt, lower min_score or raise keep_top to get more. If 2 or fewer are kept, stop and report.

## Out of scope
- Rendering (Brief 6)
- Changes to candidate-finding logic or `prompts/find_moments.md`
- Prompt caching, the Batch API, Haiku vs Sonnet comparison

## Log
Add a short "Brief 5.1" entry to `docs/PROJECT_LOG.md`, with no transcript text:
- Igor's re-listen: the c08 start was clean, so the Brief 5 reject was a false reject
- the root causes
- before vs after: candidates in, kept, trims used
- the cost
