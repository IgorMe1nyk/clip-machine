# Brief 6.1 — Caption timing offset

> **Models:** this is a small change, so plan AND build with **Sonnet 5.5, Medium** (an exception to D-009's default, stated here). Plan mode first, short plan.
> Builds on Brief 6. Status: APPROVED by Igor when he hands it to you.
> **Privacy:** no transcript text, hooks, titles or file names from the private test video in any commit, test or log. Fixtures are synthetic.

## Evidence (Brief 6 real run, Igor watched c04)
The yellow highlight lands on a word **before** the speaker says it. Whisper's word start times probably run early. C1/C7 aside, rubric C5 (captions in sync) fails.

## Goal
Add one config knob that shifts every caption event in time, so Igor can tune sync by eye. Keep it the simplest possible change. If tuning doesn't fix sync, the next step is the D-013 fallback (WhisperX), not more logic here.

## Change
1. **Config:** `captions.offset_seconds: float`, default `0.0`, validated to the range −1.0 to 1.0. Positive = captions appear **later**. Old configs still load (defaulted).
2. **Captions:** apply the offset to every event's start and end after the word times are shifted to clip time, then clamp to [0, duration]. Drop any event left with zero length. Keep the existing millisecond rounding.
3. **Restart:** the offset is a render setting, so it must be part of the per-clip "valid output" check (a changed offset means a re-render on resume). `--from-stage render` already forces a redo.
4. Record the offset used in `clips/<id>.json` with the other settings.

## Tests (offline)
- Offset 0 gives identical events to today.
- Positive and negative offsets shift starts and ends; clamping at 0 and at duration; zero-length events dropped.
- Config: default, valid values, out-of-range rejected, old config loads.
- A changed offset makes a previously valid clip invalid for resume.

## Out of scope
WhisperX or any re-alignment, per-word variable offsets, any other caption or render change.

## Acceptance
```
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run pip-audit --skip-editable
```
**Real run** (ask Igor first; local, no API cost): set `captions.offset_seconds: 0.15` in `runs/2026-10-10_source/config.resolved.yaml` (state the edit in the report), then
`uv run python -m clipmachine run runs/2026-10-10_source --from-stage render`.
Ledger unchanged. Then stop: Igor watches and may ask for another value; each retry is the same edit plus the same command.

## Report back (chat only)
Tests before/after, commits, the config edit made, render time, any deviation disclosed.

## Log
After Igor settles on a value, add a short "Brief 6.1" entry to `docs/PROJECT_LOG.md` (the value chosen and how it was judged; no transcript text) and commit it with this brief in one `docs:` commit. Don't change `default.yaml`'s default until a second video confirms the value. Don't push.
