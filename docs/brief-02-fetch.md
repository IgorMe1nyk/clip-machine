# Brief 2 — Fetch stage (approved)

> **Models (D-009):** Plan with **Opus 5.5, Medium** in Plan mode. Build with **Sonnet 5.5, Medium**.
> Builds on Brief 1 (12 commits, 104 tests, CI green). Status: APPROVED by Igor 2026-10-10.

## Goal
Make stage 1 (`fetch`) real. It takes the run's local input file, checks it is real media within limits, copies it into the run folder, extracts 16 kHz mono audio for transcription, and writes `meta.json`. All other stages stay stubs.

## Read first
1. `CLAUDE.md`, `docs/MVP.md` (sections 2–3), `docs/DECISIONS.md`.
2. The existing code: `src/clipmachine/pipeline.py`, `context.py`, `runs.py`, `preflight.py`, `models.py`, `config.py`, `stages/fetch.py`, and their tests. Follow the patterns already there.
3. `docs/clip-machine-plan.md` section 4, "Stage 1 — Fetch" (local-file part only).

Work in Plan mode first. Show me the plan, wait for my OK, then implement in small commits.

## Rules for this brief (also add the first two to CLAUDE.md)
- **Never create, change or delete anything outside the repository folder.** Build paths from the repo root as absolute paths. If a `cd` or path check fails, stop and report; never fall back to creating folders.
- **Stage outputs go only inside the current run folder.**
- **No new dependencies.** Everything here is standard library + ffmpeg/ffprobe via `subprocess`.
- **Treat the input file as untrusted** (it will be a client's file later):
  - Every subprocess call uses an argument list (never a shell string), `stdin=DEVNULL`, a timeout, and captured output.
  - Pass inputs to ffmpeg/ffprobe as absolute resolved paths with the `file:` protocol prefix, and add `-protocol_whitelist file` on inputs. This stops a crafted file (e.g. a playlist inside a container) from making ffmpeg open URLs or other files. Verify both work on Windows paths with a real run, and report.
  - Never trust the extension alone; the real container comes from ffprobe.

## Steps
1. **Config.** Add a `fetch` section to `config/default.yaml` and the pydantic models (`extra="forbid"`, validated):
   - `max_size_gb: 8`
   - `max_duration_minutes: 240`
   - `min_duration_seconds: 60`

   Existing configs and `config.resolved.yaml` keep working; add tests.
2. **Input rules (tighten Brief 1's light check).**
   - Extension allow-list becomes `.mp4 .mov .mkv .webm` (case-insensitive).
   - Audio-only extensions (`.m4a .wav .mp3`) are rejected with the message "audio-only files aren't supported yet (video needed for clips)". Update Brief 1's tests accordingly.
   - Size checked before anything else runs: reject if over `max_size_gb`.
3. **Probe** (`utils/ffmpeg.py`, new). `probe(path) -> MediaInfo`:
   - Command: `ffprobe -v error -print_format json -show_format -show_streams`, 60 s timeout.
   - Parse into a pydantic `MediaInfo`:
     - container `format_name`, `duration_seconds`, `size_bytes`
     - first video stream: codec, width, height, `avg_frame_rate`, `r_frame_rate`, rotation (from side data or tags; 0 if none)
     - first audio stream: codec, sample rate, channels
   - Reject with one clear line each when:
     - ffprobe fails or the output isn't valid JSON ("not a readable media file")
     - the `format_name` has no token in {`mov`, `mp4`, `matroska`, `webm`}
     - there is no video stream, or no audio stream
     - the duration is missing, shorter than `min_duration_seconds`, or longer than `max_duration_minutes`
   - Record whether the frame rate is variable (`avg_frame_rate` ≠ `r_frame_rate`). Note it only; don't fix it (Render will need it).
4. **Disk check.** Before copying, require free space on the run folder's drive ≥ 2 × file size + 1 GB (`shutil.disk_usage`). Otherwise reject with the numbers in the message.
5. **Copy the source.**
   - Copy the input to `source<original ext, lowercased>` in the run folder. Write it to a temp name in the same folder, then `os.replace`.
   - Compute SHA-256 while copying (one pass; don't read the file twice).
   - **Never modify or move the original file.**
6. **Extract audio** to `audio.wav`: 16 kHz, mono, `pcm_s16le`.
   - Command shape: `ffmpeg -nostdin -hide_banner -v error -protocol_whitelist file -i file:<abs source> -vn -ac 1 -ar 16000 -c:a pcm_s16le -f wav -y <temp>`, then `os.replace` to `audio.wav`.
   - Timeout: 120 s + 0.5 × source duration.
   - Verify the result with `probe` (sample rate 16000, 1 channel, duration within 0.5 s of the source).
7. **meta.json.** Write it with the existing atomic writer. Contents:
   - `schema_version`
   - `original_filename` (name only, no folder path)
   - `source_file` (file name inside the run folder)
   - `sha256`, `size_bytes`
   - the `MediaInfo` fields
   - `audio_file`
   - `ffmpeg_version` (first line of `ffmpeg -version`)
   - `fetched_at` (UTC)
   - `fetch_seconds` (wall time of the stage)
8. **Restartability.**
   - On any failure, delete this stage's temp files, leave no half-written outputs, and fail the stage with the one-line reason (the pipeline already records it in `run.json`).
   - `--from-stage fetch` must cleanly replace existing outputs.
9. **Tests.**
   - **Unit tests (no ffmpeg needed):**
     - parsing of real ffprobe JSON, saved as fixtures from actual runs on this machine
     - every rejection rule
     - the command argument lists (exact lists, including `file:` and `-protocol_whitelist`)
     - the disk-space rule (stub `disk_usage`)
     - copy + hash
     - config validation
   - **Integration tests marked `@pytest.mark.ffmpeg`**, skipped if ffmpeg isn't on PATH. They generate tiny files in `tmp_path` with ffmpeg's built-in `lavfi` test sources (3–65 s, small resolution), using encoders that exist in both the gyan.dev essentials build and Ubuntu's ffmpeg. Cases:
     - a valid video with audio → full fetch works
     - a video with no audio → rejected
     - an audio-only `.m4a` → rejected
     - a text file renamed `.mp4` → rejected
     - a too-short clip → rejected
   - **CI:** add a step that installs ffmpeg on the Ubuntu runner (`apt-get`), so the integration tests run there too.
   - **CLAUDE.md:** replace "Tests must not need ffmpeg" with "Unit tests must not need ffmpeg; ffmpeg tests are marked `ffmpeg` and skipped when it's missing."
10. **README:** a short note on what fetch produces, the input limits, and that the original file is never changed.

## Out of scope
- yt-dlp or URLs.
- The YouTube heatmap.
- Transcoding or normalizing video (VFR fixes, rotation fixes, HDR).
- Audio-only inputs.
- Transcription.
- Any API call.
- Deleting old runs (retention comes later).

If you think something here is needed, stop and ask.

## Acceptance checks (run all, paste output)
```
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q                 # report passed / skipped counts
uv run pip-audit --skip-editable
```

**End-to-end with Igor's real test video** (Igor gives the path):
1. `uv run python -m clipmachine run "<path>" --client example-client`, then show:
   - the run folder listing with sizes
   - `meta.json`
   - `ffprobe` output for `audio.wav` (16000 Hz, mono, `pcm_s16le`)
   - `fetch_seconds`
   - proof that the original file's size and modified time are unchanged
2. Same command again → "nothing to do".
3. `--from-stage fetch` → outputs replaced, and no temp files left behind.
4. Rejections, each with its one-line message and no run outputs left behind:
   - a text file renamed `.mp4`
   - an `.mp3`
   - a path that doesn't exist

## Report back
- Output of every acceptance command (not "done").
- Commit list with a one-line summary each.
- Whether the `file:` prefix and `-protocol_whitelist` worked on Windows paths, and how you checked.
- Deviations, open questions, anything unverified.
