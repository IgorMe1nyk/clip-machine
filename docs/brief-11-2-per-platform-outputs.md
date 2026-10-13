# Brief 11.2 — Per-platform outputs and a posting plan (approved when Igor hands it over)

> **Models:** Plan with Opus 5.5 (or Fable) in Plan mode; build with a Sonnet 5.5 subagent. Each check runs separately. Auto mode; push asks.
> **Branch:** `brief-11-2-platforms` from main.
> **Privacy (public repo):** no campaign or creator names, captions, hooks or closers in commits, tests or logs. Synthetic fixtures only. Campaign data stays in gitignored folders.
> **Cost:** $0, no API calls.

## Why
Scheduling the first 3 posts was painful. Every moment rendered as "tiktok": one flat folder, file names that all say `_tiktok`, captions with the TikTok prefix even for YouTube (Igor had to edit `@...` by hand), and `campaign-posted` refused the real platforms. The campaign also requires each moment to be posted **once, on one platform**, with platform-specific captions.

## Goal
One render gives Igor three ready-to-upload folders (TikTok, Instagram, YouTube). Each moment belongs to exactly one platform, with that platform's caption, plus a suggested posting plan. Uploading becomes: open the folder → upload the next file → paste its text.

## Design
1. **Platform assignment** (`moments.yaml`):
   - `platform` may be `tiktok | instagram | youtube | auto`. Missing means `auto`.
   - **auto** spreads moments across the platforms in `campaign.yaml`'s new `platforms: [tiktok, instagram, youtube]` list. Round-robin **in moments.yaml order** (the list is already ranked A → B → C), so every platform gets a fair mix of strong and weak moments.
   - A moment that was **already posted** keeps the platform it was posted on, and auto skips it.
   - The assignment is **stable**: written to `campaigns/<slug>/assignments.json` on first use and reused, so re-renders never move a moment to another platform. Delete the file to reassign.
2. **Output layout** (per run): `runs/<run>/campaign/<slug>/tiktok/`, `instagram/`, `youtube/`. Inside each:
   - `<id>.mp4`
   - `<id>.txt`, the paste-ready post text for that platform:
     - **TikTok and Instagram:** the caption with that platform's prefix from `campaign.yaml`, hashtags, then the closer as the last line.
     - **YouTube:** line 1 `TITLE: <short title>`, the hook in sentence case, max 100 characters; then a blank line; then the description with the **YouTube prefix** (e.g. `@show`), the caption, and the closer.
   - The JSON, review sidecar and report stay where they are, or move to a `_meta/` subfolder (choose and state it). The upload folders must contain only the mp4 and txt files.
3. **Posting plan:** `runs/<run>/campaign/<slug>/posting_plan.csv`, opened in Excel. Columns: `date, time_et, platform, id, file, text_file, posted`.
   - Unposted clips only.
   - One per platform per day, at **12:30 PM / 3:30 PM / 6:30 PM ET** (config: `campaign.posting_times`), starting the next day.
   - Order follows the assignment order (strongest first).
   - `posted` is filled in from used.json.
4. **campaign-posted:**
   - `--platform` becomes optional. Default: the moment's assigned platform.
   - New `--fix-platform <tiktok|instagram|youtube>` re-labels an existing posted entry, to correct earlier records. Tonight's real ones: A03 → instagram, B05 → youtube.
   - A posted moment is never re-rendered (as today).
5. **Closers:** a closer is assigned when the `.txt` is written, rotating per platform so the same closer never appears twice in a row **within a platform's posting order**.
6. **Backward compatible:** an explicit `platform:` in moments.yaml works as today. The flat-folder layout goes away (migration: the next render writes the new layout; old flat files are left untouched and listed in the report as old).

## Tests (offline, synthetic)
- auto round-robin follows moments.yaml order, skips posted moments, and is stable across runs (assignments.json)
- An explicit platform overrides auto
- Folder layout: only mp4 + txt in each platform folder
- TikTok/Instagram text uses its prefix; YouTube text has a TITLE line ≤ 100 chars, the YouTube prefix, and the closer last
- posting_plan.csv: one per platform per day, configured times, unposted only, strongest first
- campaign-posted: default platform, `--fix-platform` re-labels, an unknown id errors clearly
- Closer rotation per platform never repeats back to back
- All existing tests pass

## Acceptance
Run each separately:
```
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run pip-audit --skip-editable
```
**Real run** (ask Igor first; $0):
1. Fix tonight's records: `campaign-posted --campaign <slug> --id A03 --fix-platform instagram`, and the same for B05 → youtube.
2. Set `platforms: [tiktok, instagram, youtube]` in Igor's campaign.yaml and `platform: auto` (or remove the field) in his moments.yaml.
3. Render all unposted moments.

**Report back:** per platform, the clip count and ids; the first week of posting_plan.csv; and confirmation that the 3 posted moments were untouched.

## Log
`docs/PROJECT_LOG.md` entry; `docs/DECISIONS.md` **D-031**: per-platform outputs, auto assignment, posting plan. Igor's request after the first manual scheduling; Claude proposed; Igor approved; 2026-10-13.
