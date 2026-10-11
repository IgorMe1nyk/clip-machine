# Brief 6 — Render stage (cut + 9:16 crop + burned-in captions)

> **Models (D-009):** Plan with **Opus 5.5, Medium** in Plan mode. Build with **Sonnet 5.5, Medium**. If ffmpeg filter escaping on Windows keeps failing after two tries, stop and tell Igor; he may switch to Opus, High.
> Builds on Brief 5.1. Status: APPROVED by Igor when he hands it to you.
> **Privacy:** no transcript text, hooks, titles or file names from the private test video in any commit, test or log. Test fixtures are synthetic.
> **Principle:** simplest thing that works. No new dependencies. No options we don't need yet.

## Goal
Make stage 5 (`render`) real. For every clip with `kept: true` in `scored.json`, make one phone-ready vertical video with burned-in word-group captions:
- `clips/<id>.mp4`: 1080×1920, H.264 + AAC, captions burned in
- `clips/<id>.ass`: the subtitle file used (kept for debugging)
- `clips/<id>.json`: id, rank, score, final start/end, duration, crop box, output ffprobe summary

## Read first
1. `CLAUDE.md`, `docs/MVP.md` (rubric C1, C5–C8), `docs/DECISIONS.md`, `docs/COSTS.md` (Montserrat row).
2. `utils/ffmpeg.py` (existing wrappers: argument lists, `file:` prefix, `-protocol_whitelist file`), `stages/score.py`, `models.py` (ScoredClip, Word), and `transcript.json`'s shape.

Work in Plan mode first. Show the plan, wait for Igor's OK, then implement in small commits.

## Design
1. **Config** (validated, old configs still load): `render: { width: 1080, height: 1920, crf: 20, preset: medium, audio_bitrate: 160k }`, and keep the existing `captions: { words_per_line: 3, font: Montserrat, highlight_color: "#FFD400" }`, adding `font_file`, `font_size` (default ~80) and `margin_v` (default ~350, bottom-centred, clear of phone UI).
2. **Crop math, in Python, not in filter expressions** (pure function, fully tested):
   - Read the source width, height and rotation from ffprobe. If rotation is ±90/270, swap width and height (ffmpeg auto-rotates on decode).
   - Take the largest centred 9:16 rectangle: if the source is wider than 9:16, crop width = height×9/16; otherwise crop height = width×16/9. Round both to even integers.
   - Pass plain integers to `crop=w:h:x:y`, then `scale=1080:1920`, then `setsar=1`, then `subtitles`.
   - A source that is already 9:16 gives a full-frame crop. That's fine.
   - Record VFR/CFR (r_frame_rate vs avg_frame_rate) and rotation in `clips/<id>.json`.
3. **Captions (`utils/captions.py`, pure functions, fully tested)**:
   - Words are those from `transcript.json` overlapping [start, end]. Times are shifted so the clip starts at 0 and clamped to [0, duration].
   - **Strip leading and trailing spaces** on every word.
   - Groups of `words_per_line` words. A group also ends after a word ending in `. ? !`.
   - Highlight: one Dialogue event per word, showing its whole group with that word coloured `highlight_color` (convert #RRGGBB to ASS `&HBBGGRR&`). An event runs from the word's start to the next word's start (the last word runs to its end). Small gaps under 0.5 s are filled, so captions don't flicker.
   - **Escape untrusted text:** transcript words are data. Remove or escape `{`, `}` and `\` so a word can never become an ASS override tag; replace newlines with spaces.
   - Style: white text, black outline (~4), no box, bold, `PlayResX/Y` = 1080×1920, alignment 2 (bottom centre).
4. **Font:** bundle a **static** Montserrat Bold TTF plus its `OFL.txt` in `assets/fonts/`. OFL allows redistribution with the licence file; the repo is public, so the licence file is required. Propose in the plan where you'll get the file (the official Montserrat source); **ask Igor before downloading**. Pass `fontsdir` to the subtitles filter. Preflight: if the font file is missing, fail with a clear error, never fall back silently.
5. **One ffmpeg pass per clip:** `-ss <start>` before `-i` (accurate when re-encoding), `-t <duration>`, the video filter chain above, `-c:v libx264 -preset <preset> -crf <crf> -pix_fmt yuv420p`, `-c:a aac -b:a <bitrate>`, `-movflags +faststart`. Always re-encode, never stream-copy. Argument list only, never a shell string.
6. **Windows filter paths:** inside the `-vf` string, never put an absolute Windows path (the drive colon breaks filter parsing). Run ffmpeg with a working directory set so the `.ass` file and `fontsdir` are short relative paths with forward slashes. Run-folder slugs are already sanitized. Add a test asserting the built filter string contains no drive letter or backslash.
7. **Validate every output** with ffprobe: 1080×1920, h264 + aac, duration within ±0.15 s of expected. Fail the stage if any clip fails.
8. **Restartability:** write to `<id>.tmp.mp4`, validate, then rename. A rerun of a finished stage does nothing. If a previous run failed mid-way, rerun renders only the clips without a valid output. No temp files left behind on failure.
9. **Decision record:** add **D-013** to `docs/DECISIONS.md`: "Word timestamps for the MVP. Options: keep faster-whisper word timestamps · add WhisperX forced alignment · stable-ts. Choice: keep faster-whisper. Why: timestamps not yet shown to be a problem; WhisperX needs PyTorch + CUDA toolkit; stable-ts was archived 2026-05-30. Fallback: WhisperX alignment as an optional step if rubric C5 (caption sync) fails. Decided by Igor, 2026-10-10."

## Tests (offline unless marked)
- Crop math: landscape 16:9, 4:3, already 9:16, portrait narrower than 9:16, square, rotation 90, odd sizes → even integers.
- Captions: grouping (count and sentence ends), leading-space stripping, time shift and clamping, highlight colour conversion, gap filling, escaping of `{ } \` and newlines, words partly outside the clip.
- Command builder: argument list, `-ss` before `-i`, no drive letters or backslashes in the filter string, re-encode flags present.
- Stage: only `kept` clips rendered; restart renders only missing ones; failure leaves no temp files; missing font → clear error. ffmpeg faked.
- One opt-in `ffmpeg`-marker test: a synthetic 4 s `testsrc` + `sine` input and a 2-word fake transcript render, and ffprobe shows 1080×1920 h264 + aac.

## Out of scope
Hook text overlay, face tracking, loudness normalisation, per-client caption styles, emoji, rendering rejected clips, WhisperX (D-013), the report page (Brief 7).

## Acceptance
```
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run pytest -q -m ffmpeg
uv run pip-audit --skip-editable
```
**Real run** (ask Igor first; no API cost, since render is local):
`uv run python -m clipmachine run runs/2026-10-10_source --from-stage render`
- Ledger line count unchanged (render makes no API calls).
- Rerun → "nothing to do".

## Report back (chat only)
- Per clip: id, final start–end, source size and rotation, crop box, VFR/CFR, output size, duration, file size, render time.
- Test counts before and after; the commit list.
- Any deviation from this brief, disclosed.
- For Igor to check by watching (C1 clean start and end, C5 captions correct and in sync, C6 face in frame, C7 audio).

## Log
Add a "Brief 6" entry to `docs/PROJECT_LOG.md` (counts, render time, no transcript text), and commit it with this brief and the D-013 record in one `docs:` commit. Don't push.
