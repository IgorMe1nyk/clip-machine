# Brief 7 — Report stage (report.html review page)

> **Models:** plan AND build with **Sonnet 5.5, Medium**. The page design is already decided in `docs/PRODUCT.md`, so planning is light (an exception to D-009's default, stated here). Plan mode first, a short plan.
> Builds on Briefs 6 and 6.1. Status: APPROVED by Igor when he hands it to you.
> **Privacy:** `report.html` contains transcript text, hooks, titles and reasons. It lives only in the run folder (git-ignored) and is never committed. Tests use synthetic data only.
> **Principle:** simplest thing that works. **Standard library only** (`html.escape`, string building). No Jinja, no JavaScript, no new dependencies.

## Goal
Make stage 6 (`report`) real: one offline `report.html` per run, laid out exactly as in `docs/PRODUCT.md` section 5, so Igor can review every kept clip fast.

## Read first
`CLAUDE.md`, `docs/PRODUCT.md` (section 5 is the spec), `docs/MVP.md` (rubric C1–C9), `stages/score.py`, `stages/render.py`, `models.py` (ScoredFile, RenderedClip), `runs.py` (atomic writes), and how `costs.json`, `run.json` and `meta.json` are shaped.

## Design
1. **Inputs:** `scored.json`, `clips/<id>.json`, `costs.json`, `run.json`, `meta.json`, `transcript.json`. Missing `scored.json` → RunError naming the stage to run.
2. **Summary bar:** run folder name, source length, kept count of total candidates, total API cost (from `costs.json`), total processing time (from `run.json` stage timestamps), reject-reason counts.
3. **One card per kept clip, in rank order:** rank, id, score, start–end as mm:ss, duration, "trimmed" badge if trimmed, edge flags if true; a `<video controls preload="metadata">` about 360 px wide with a **relative** `src="clips/<id>.mp4"`; title, hook, the model's reason; the clip's transcript text (words between start and end) inside a collapsed `<details>`; a one-line rubric reminder with short labels for C1–C9.
4. **Rejected candidates:** one collapsed `<details>` list with id, times, score, reject_reason, reason. No video. Also list kept-but-dropped clips with their `dropped_by`.
5. **States** (PRODUCT.md): zero kept → the message "No clips passed the judge." and the rejected list open by default. A kept clip whose `clips/<id>.mp4` or `.json` is missing or invalid → its card shows "Render missing: <short reason>" with no player; other cards are normal.
6. **Safety:** every piece of text from the transcript, the model or file names goes through `html.escape` (quote=True). No external URLs, fonts, scripts or CDNs. Inline CSS only, readable at phone and desktop widths, light background.
7. **Write** atomically to `report.html` in the run folder. The stage is cheap, so `--from-stage report` simply rebuilds it. At the end, the CLI prints the report's path.

## Tests (offline, synthetic)
- Escaping: a hook, title, reason and transcript word containing `<script>`, `"`, `&` and `'` come out escaped; no raw `<script` in the output.
- No `http://` or `https://` anywhere in the output.
- Video `src` values are relative, with no drive letters or backslashes.
- Cards in rank order; counts and cost in the summary match the inputs.
- Zero-kept state; render-missing state for one clip while the others render normally.
- Missing `scored.json` → clear error.
- Stage end to end on a synthetic run.

## Out of scope
Buttons, JavaScript, ratings saved from the page, client-facing views, multiple episodes, opening a browser automatically.

## Acceptance
```
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run pip-audit --skip-editable
```
**Real run** (ask Igor first; local, no API cost):
`uv run python -m clipmachine run runs/2026-10-10_source --from-stage report`
Ledger unchanged; a rerun says "nothing to do". Igor opens `report.html` in his browser and checks that c04 plays, the text reads well, and the rejected list shows 6 entries.

## Report back (chat only)
Tests before/after, commits, the report's file size, any deviation disclosed.

## Log
Add a "Brief 7" entry to `docs/PROJECT_LOG.md` (no transcript text) and commit it with this brief and `docs/PRODUCT.md` in one `docs:` commit. Don't push.
