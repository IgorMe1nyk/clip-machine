# PROJECT_LOG.md

Dated entries, newest first. Numbers come from real runs only.

## 2026-10-14 — Word lists: "self harm" is flagged, no longer masked (Igor's request)

A deviation from Brief 11.1 (Tier 1 lists self-harm as masked), asked for by Igor on 2026-10-14.
Whisper writes "self-harm" as the caption words "self" and "-harm", so the mask showed
"s*** -h***", which reads like a swear word in a clip that has none. "self harm" and "self-harm"
moved to flag.txt: a warning in the report for hooks and post captions, never masked. Side
effect: "cutting" next to it is now flagged only, not masked. Test:
`test_self_harm_is_flagged_not_masked`.

## 2026-10-14 — Brief 12.2, parts 10 and 11: the allocator fix, the data-loss fix, first real runs

**Part 10** (`d979f3d`): posts already made on a day use up that platform's slots and the
campaign's `max_per_day` for that day; the free slots left take the day's latest times.

**Part 11** (`81c0c33`): a campaign that does not load during a combined-plan rebuild no longer
loses its rows. Before, the rebuild rewrote `posting_plan.csv` from the campaigns that loaded
only, so one broken file dropped another campaign's rows, typed result columns included, for
good. Now its rows are kept exactly as they are, its unposted rows hold their slots (no other
campaign is given them), its view is left alone and the warning says so. A broken campaign in
the `priority` list no longer fails the whole rebuild; a moments.yaml that is not UTF-8 is a
warning too; plan times saved as "17:30" or "5:30PM" are read. Three tests (the new ones fail
on the old code); a fresh-context review of the diff found no row loss, and two of its points
were fixed before the commit.

**Checks at 81c0c33:** ruff, format, mypy, pip-audit (no known vulnerabilities), pytest (2257
passed, 3 skipped), each run separately, all exit 0.

**Real runs** (one campaign with `selection: ai`):

| Run | Source | API cost | Result |
|---|---|---|---|
| `produce`, episode 1 | 102 min | $0.0917, 14 calls | 6 clips (instagram 6) of 21 candidates; transcribe 393 s on the GPU; render 372 s |
| `produce`, episode 2 | 229 min | $0.0981, 25 calls | 0 clips: 22 of 23 rejected by the scorer, 1 dropped (adjustment ignored) |
| `produce`, episode 3 | 250 min | $0 | refused at fetch: over `fetch.max_duration_minutes` (240) |
| `campaign-init` on a real brief | 5 KB text | $0.0198, 1 call | about 14 s; `selection: human`; 6 `# confirm:` notes |

- After each `produce` the combined plan was compared against a copy: posted rows and result
  columns unchanged, only the new rows added. Deleting a test campaign and rebuilding left the
  combined plan and both views byte-identical.
- The first `produce` on a new episode re-renders nothing from other episodes: only that
  episode's moments are rendered, so the fingerprint re-render of older clips happens on their
  own episode's next run.
- An episode whose scout proposes nothing leaves no scout.json record, so `produce` on it again
  scouts and pays again.
- The `campaign-init` draft against the hand-written campaign.yaml: platforms, prefixes, clip
  length, caption style and hook limit identical; differs in `selection` (human against ai),
  `hook.mode` (full against intro), `scout.per_episode` (8 against 15), topic keys, and names
  that include handles.

## 2026-10-14 — Brief 12.2: one-command campaigns and one posting plan across campaigns (built; real run pending Igor's OK)

**Built** on branch `brief-12-2-one-command` (D-034, D-035), eight commits, no real campaign
data touched:

- `campaign-init <slug> --brief <file>`: the brief (.txt/.md/.pdf; pypdf text extraction only,
  10 MB and 50 pages at most, encrypted or malformed files refused cleanly) is cleaned, cut at
  60k characters, fenced between `<brief>` tags and treated as data; one Sonnet call with an
  API-enforced draft schema and one repair; the draft is validated as a CampaignConfig and
  written under a DRAFT header with the model's reasons and `# confirm:` notes as comments.
  `selection` falls back to `human`. `--watermark` needs a real PNG under 5 MB; `--moments`
  drafts moments.yaml from a timestamped list with no model call. Costs land in
  `runs/_campaigns/<slug>/_init/costs.json`.
- `produce <episode> --campaign <slug>`: scout once per episode where allowed, render this
  episode's unposted moments, keep unchanged clips (fingerprint in used.json; the first `produce`
  after this change re-renders every unposted clip once, since older entries have none),
  rebuild report and plan, print the new clips' slots and the cost. A sourceless moment rendered
  from another episode is refused, not guessed.
- `campaigns/schedule.yaml` and `runs/_campaigns/posting_plan.csv`: the combined plan (strict
  priority, per-campaign `max_per_day`, `max_per_platform_per_day`, no slot whose time has
  passed today). First build: per-campaign plans backed up to `posting_plan.pre-12-2.bak` and
  their result columns carried over; per-campaign plans are read-only views from then on.
  `plan` rebuilds; `campaign`, `produce`, `campaign-posted` and `--fix-platform` rebuild too.
- Refactor: `run_campaign` is now `open_campaign` → `render_moments` → `finalize`; the
  `campaign --only` source-column bug is fixed; `watermark.position` defaults to `auto`.
- Docs: `docs/RUNBOOK.md`, the example README (5 steps, combined plan), the example
  `schedule.yaml`, COSTS row for pypdf.

**Checks:** ruff, format, mypy, pytest (2251 passed, 3 skipped), pip-audit, each run separately
and green at every commit. A fresh-context security and reliability review of the full diff
followed (see the session report).

**Pending (Igor):** write the real `campaigns/schedule.yaml` (first campaign `max_per_day: 3`,
then the second; 4 times a day), rehearse the migration on a copy under `runs/_scratch/`, then
`plan`; `campaign-init` on the second campaign's saved brief (compare with the hand-written
YAML); `produce` on one new episode.


## 2026-10-14 — Scout robustness, the recovered clips, and the 1440p switch of the ai campaign

**Scout robustness** (branch `scout-robustness`, merged into main): a hook still over 7 words
after the one repair keeps the clip with an empty hook and a note ("no hook: write one" in
moments.yaml, scout.json, the report; the render refuses the moment until a hook is written);
`scout --reuse-scored` rebuilds the proposals from the episode's scored.json or the new
`scored.last-good.json` copy with no API call. Tests for both; the suite green.

**Recovered clips at $0:** the last good scoring (its scored.json had been deleted by the later
attempts) was replayed from the saved raw answers through the real stages, then
`scout --reuse-scored` appended S06 (score 9, dating → instagram), S07 and S08 (7, youtube):
3 proposed, 5 skipped as already listed, 0 API calls.

**1440p switch (Igor's decision):** the 1440p AV1 + Opus download of the same episode decodes
at about 5.7x realtime on the CPU (10.5 s for 60 s; the 1080p H.264 file: 1.6 s). Same episode:
audio correlation 0.999 at six points, the 1440p file running 77.5 ms earlier (video cuts 41.7
ms earlier, one frame). A new run folder took the 1080p transcript with every time shifted by
-0.0775 s (transcribe marked done); moments.yaml repointed with the same shift (backup
`moments.yaml.bak-1080`). Upscale factor 1080x1920 from the crop: **1.78x before (zoomed
2.05x), 1.33x after (zoomed 1.53x)**. Renders from 1440p (fit layout on):

| clip | length | fit | zooms | render |
|---|---|---|---|---|
| S01 | 35.3 s | 5.0 s | 1 | 64.7 s |
| S04 | 26.9 s | 13.6 s | 1 | 43.6 s |
| S05 | 38.2 s | 13.5 s | 0 | 65.5 s |
| S06 | 56.6 s | 0 | 3 | 69.4 s |
| S07 | 47.8 s | 0 | 1 | 62.1 s |
| S08 | 46.3 s | 15.7 s | 2 | 75.8 s |

About 1.6x the 1080p render time (AV1 decode). **S02 was re-rendered by mistake:** it was
posted but not yet marked (used.json said unposted), so the tool treated it as pending; the
local 1080p file is replaced by a 1440p render (the posted upload is unaffected). It is marked
posted now. S03 untouched. The old 4.3 GB 1080p file and the old run folder's source copy went
to the Recycle Bin; the old run folder keeps its transcript and scout files.

## 2026-10-14 — Brief 12.1: graphics-aware layout (built; real re-render done; Igor's phone check pending)

**Why:** the second campaign's episodes carry on-screen text (chapter title, logo, citation
footnotes, definition panels). The speaker crop cut a footnote in three of the first five scouted
clips and lost a definition panel in one. Igor's rule: graphics must never look cut off (D-033).

**Done** (branch `brief-12-1-graphics` from `main`, 2 fix commits + 7 brief commits; not merged,
not pushed)
- **Two fixes first:** a kept clip whose adjustment only broke the length limits keeps the
  candidate's own cut (`trims_recovered`; the first scout run had dropped its best-scored clip
  this way), and repair notes carry the validator's message, not only "value_error".
- **Text detector:** OpenCV's DB detector with the model zoo's PP-OCRv3 English model
  (`models/textdet/`, 2,423,490 bytes, Apache-2.0, pinned commit and SHA-256), fetched once by
  `fetch-text-model`; preflight names the command. 105-150 ms per 640-wide frame on the CPU.
- **Rules (`utils/graphics.py`):** 2 frames a second at 960 wide; boxes at the same place in 80%
  of a clip's samples are static overlays (title, logo) and ignored; a shot has graphics when the
  rest cover 2% of the frame in 30% of its samples and one reaches outside the crop window in
  force; fit shots become spans on the exact cut times. `clips/<id>.graphics.json`, reused like
  the track; kept through the resume sweep.
- **Fit layout (`utils/layout.py`, `ffmpeg.fit_graph`):** the whole frame scaled to 1080 wide on
  a blurred (boxblur 20), darkened (0.35) copy of itself; frame and caption box centred as a
  group in the safe area (frame at y 448-1056 for 1080p sources); captions of a fit shot placed
  under the frame; hook at its usual top; no zoom starts in a fit span; one ffmpeg graph with
  `split`, the speaker crop as `crop@spk` (the sendcmd lines name it), an `overlay` switched by
  `enable=between(t,...)` on the spans, subtitles last; the watermark graph wraps it.
- **Records:** `fit_spans`, `fit_seconds`, `static_overlays`, `graphics_sha256` in the clip
  JSONs, the graphics keys in `RenderedSettings` (a toggle or a tuning re-renders on resume),
  console `fit N s in M spans, K static overlays`, a report line per card. `enabled: false`:
  no call, no file, the old ffmpeg arguments byte for byte. A campaign may override the keys.
- Tests: 50 new (rules, cache, geometry, graph, render stage, campaign, report, preflight,
  fetch) plus one ffmpeg-marked real render with a magenta panel; the whole suite green.

**Real re-render** (the four unposted scouted clips of the first ai campaign, $0):

| clip | length | fit | speaker | static overlays | analysis | render |
|---|---|---|---|---|---|---|
| S01 | 35.3 s | 4.0 s (0-4.0) | 31.3 s | 3 (top-left, bottom-right x2) | 11.2 s | 40.7 s |
| S02 | 53.3 s | 0 | 53.3 s | 3 | 13.4 s | 31.5 s |
| S04 | 26.9 s | 14.5 s (0.1-14.6) | 12.4 s | 3 | 7.6 s | 28.1 s |
| S05 | 38.2 s | 13.5 s (10.1-23.6) | 24.7 s | 3 | 10.2 s | 37.8 s |

The static overlays are the chapter title (top-left) and the two lines of the show logo
(bottom-right) in every clip. S04's "85% rule" card and S05's "Hormesis" card are shown whole;
S01's fit shot is the citation footnote. Analysis adds about 10 s per clip on the CPU (frames
decoded once more); the fit graph adds about 5-10 s to the render.

**Scout rerun for the length fix:** refused by the $1 per-run cap at first (the folder had
booked $0.87), then, with the cap at 2.00 for one run (Igor's OK), it died in scoring again:
four hooks over 7 words in one chunk, one still over after the repair ($0.21 more, nothing
written; the run folder now holds $1.08). Measured at $0 on the last good scored.json instead:
the fix recovers 3 of the 4 dropped clips (scores 9, 7, 7); the fourth failed on a weak
boundary and stays dropped. See the note below.

**Not done / next**
- Igor's phone check: S04 and S05 with graphics, S01's footnote shot; is the switch natural?
- The scout's hook rule fails whole chunks: a hook over 7 words after the repair should drop
  that clip, not the run (a Brief 12 follow-up; three of four scout runs on this episode died
  this way). Candidates are re-fetched on every scout rerun ($0.02).
- `stages/report.py` (the normal run report) does not show the fit seconds yet.

## 2026-10-13 — Brief 12: AI scout (built; real run pending Igor's OK)

**Why:** Igor's second campaign allows the AI to pick the moments (40+ episodes, no moment list,
quality and accuracy rules only) and wants neither a watermark nor a closer line. The normal
pipeline finds and scores moments; campaign mode renders rule-compliant clips; Brief 12 connects
them behind a hard gate (D-032). The human-picked path of Brief 11 is unchanged.

**Done** (branch `brief-12-ai-scout` from `main`, 6 commits; nothing pushed or merged)
- **Optional watermark and closers:** no overlay and one ffmpeg input; the text ends with the
  hashtags or the caption; the two checks read "not required" (`n/a`). Existing campaigns unchanged.
- **One campaign folder:** clips, `_meta/`, the report and `posting_plan.csv` now live in
  `runs/_campaigns/<slug>/` whatever the source video (Igor's decision). An entry may name its
  own `source:`; each video keeps its run folder; the plan has a `source` column.
- **Stages:** `RunContext.out_dir` and `scout_block` (a `{scout_block}` placeholder in both
  prompts, empty on normal runs), `ClientRules.extra`, optional `topic`/`caption` on the scorer's
  answer and in scored.json, an `extra_check` composed with the id check.
- **`scout <episode> --campaign <slug> [--dry-run]`:** refused unless `selection: ai`; candidates
  and scoring with the campaign's rules and clip length; the check asks for a valid topic, a hook
  of at most 7 words (and `hook.max_chars`) and a caption, repaired once; top `per_episode` kept
  clips, skipping stretches already listed, rendered, posted or rejected for that source; host-side
  face heuristic (`likely`/`unsure`/`not_checked`, never drops); entries `S<NN>` appended to
  moments.yaml as text (Igor's lines and comments byte for byte; `moments.yaml.bak`); scout.json;
  console: counts, cost, the next `campaign --only` command. No text ever on the console.
- **`campaign-reject --id`:** recorded in used.json; the render skips it, the scout never proposes
  the stretch again; a posted moment cannot be rejected.
- **Report:** scouted cards show score, topic → platform, host check and reason.
- Config: `campaign.scout_host_seconds` 5.0, `campaign.scout_host_share` 0.5. D-032. Example
  campaign and README updated; CLAUDE.md names the campaign output folder.
- Tests: about 70 new (optional watermark/closers, multi-source, stage hooks, scout end to end
  with the fake client and fake faces, reject, report cards); the whole suite green; ruff,
  format, mypy and pip-audit clean.

**Not done**
- The real run (Igor's OK; a few cents of API credits): Igor creates `campaigns/<slug2>/`
  (`selection: ai`, rules, topics, prefixes, no watermark, no closers) and downloads one episode;
  `scout <episode> --campaign <slug2>`, then `campaign <episode> --campaign <slug2> --only <ids>`.

**One-time move (done, 2026-10-13):** the first campaign's outputs moved from its run folder to
`runs/_campaigns/<slug>/` (48 used.json output paths repointed, no file missing; the old flat
files came along and are listed as old in the report).

**Cost:** $0 so far. No API call (the tests fake the client).

## 2026-10-13 — Brief 11.2: per-platform outputs and a posting plan (built; real run pending Igor's OK)

**Why:** every moment rendered as "tiktok" into one flat folder: `_tiktok` file names, the TikTok
prefix in YouTube captions (edited by hand), `campaign-posted` refusing the real platforms, and
the first 9 posts recorded under "tiktok". The campaign wants each moment posted once, on one
platform, with that platform's text. $0, no API call, no new dependency (D-031).

**Done** (branch `brief-11-2-platforms` from `main`; nothing pushed or merged)
- **Layout:** `runs/<run>/campaign/<slug>/tiktok/`, `instagram/`, `youtube/` hold `<id>.mp4` and
  `<id>.txt` only; jsons, `.ass`, review files, crop commands and tracks moved to `_meta/`
  (`<id>_<platform>.*`, so `auto_spelling.json` and `auto_position.json` read as before). Old
  flat files are left untouched and listed in the report.
- **Assignment** (`campaign/assign.py`): `platform: auto` (or no field) spreads moments over
  `campaign.platforms` (default all three) by the least-loaded rule in moments.yaml order, locked
  in `campaigns/<slug>/assignments.json`; posted platform > explicit > lock > rule. Ids are unique
  now. moments.yaml entries are `MomentSpec`; `Moment` (platform decided) is what the renderer sees.
- **Post text** (`campaign/post.py`): TikTok/Instagram unchanged in shape; YouTube gets
  `TITLE: <hook in sentence case>` (names, spelling targets, allowed words and the app config's
  `campaign.title_keep_case` kept as written; at most 100 characters), a blank line, then the
  YouTube prefix, caption, hashtags, closer. The title is masked like the hook.
- **Closers** rotate per platform (`used.json` `closers`; each platform starts at its own place in
  the list); the Brief 11 counters stay in old files, unused.
- **Posting plan** (`campaign/plan.py`): `posting_plan.csv` with `date, time_et, platform, id,
  file, text_file, posted` plus Igor's `views_48h, likes, comments, shares, pct_us_ca_uk,
  avg_watch_s, pct_completed, notes`. Posted clips keep their row (posted date, platform from
  `used.json`); unposted rendered clips get `posting_times` (12:30 / 3:30 / 6:30 PM ET) one slot
  each per platform per day from tomorrow, strongest first. The old file is read first (Excel
  BOM and CRLF fine); filled values and extra columns are never overwritten; only an unposted
  clip's slot moves.
- **campaign-posted:** `--platform` optional (the assigned platform); `--fix-platform` relabels
  the posted entry; clear errors for an unknown id or an ambiguous platform.
- Tests: 2103 passed, 3 skipped (about 60 new across assign, post, plan, used, run, report,
  loader). ruff, format, mypy and pip-audit clean.

**Real run** (Igor's OK; $0, API calls 0; 1252.5 s for 26 clips, 40–52 s each with the tracks reused)
- Records first: only 3 of the 9 scheduled posts were marked posted in used.json (the handoff said
  9). The other 6 were marked (`campaign-posted --platform tiktok`, their recorded label), then all
  wrong labels relabelled with `--fix-platform`: 3 instagram, 3 youtube, 3 stay tiktok. Backups of
  used.json, moments.yaml and campaign.yaml in `runs/_scratch/backup-11-2/`.
- `platforms: [tiktok, instagram, youtube]` and `platform: auto` on all 35 moments. Assignment:
  tiktok 12, instagram 12, youtube 11 (the 9 posted keep their platforms). 26 rendered, 0 failed,
  9 refused (posted). Per folder: tiktok 9, instagram 9, youtube 8 clips; 61 files of the 9 posted
  moments unchanged (hash and mtime). posting_plan.csv: 9 posted rows, 26 slots Oct 14–16 plus
  Oct 17 (3 per platform per day).
- Three new clips overlap a posted moment by more than half (`not_previously_used` warning):
  C03 and A04, C09 and A03, C11 and A05. Igor decides whether to post them (the campaign wants
  each moment once).

**Not done**
- HANDOFF.md refresh after Igor's folder check; merge after his OK.

**Cost:** $0. No API call.

## 2026-10-13 — Brief R2.1: smooth zooms (built; 3-clip real run done; Igor's phone check pending)

**Why:** on the phone the R2 hard punch-in looked like a glitch (Igor, 2026-10-13). An eased zoom
in and out reads as a camera move. Same branch, same sendcmd path, still deterministic and $0.

**Done** (3 commits on the branch `brief-r2-zooms`, plus this one; nothing pushed or merged)
- **`video.zoom.style: smooth | punch`** (default smooth; punch is R2 unchanged, byte for byte,
  with a golden test) and **`ramp_s`** (0.4; two ramps must fit in `min_gap_s`). New defaults
  `scale` 1.15 and `max_upscale` 2.1: a 1080p source zooms to 528x940, an upscale of 2.05.
  `zoom_style` and `zoom_ramp_s` join `RenderedSettings`, so the R2 punch clips re-render.
- **Smooth:** cosine ease-in-out from the base window to the zoomed one over `ramp_s` from the
  zoom-in time, hold, ease back out ending at the hold end. One crop command per output frame
  during a ramp (12 lines per 0.4 s at 29.97 fps), even pixels, clamped. The width follows from
  the height with the base window's own aspect, so the first frame of a ramp is the base window
  exactly and the aspect stays within 0.5% of the base's (which is the closest even 9:16 the
  source allows). A clip whose first zoom starts at 0 starts on the base window and eases in.
- **Cuts:** a hold that ends on a camera cut (or the clip end) switches instantly, no ramp-out;
  a ramp that would cross a boundary is shortened (note `zoom_ramp_shortened`); with the R2
  spacing rules a 0.4 s ramp never meets one. The clip json lists `zoom_ramps`.
- Captions, hook and watermark still draw after the crop; the hook's face avoidance reads the
  per-frame boxes.
- Tests: 2043 passed, 3 skipped (51 new); ffmpeg-marked: a ramp shrinks the window frame by
  frame (the top band's share of the output rows grows strictly over the ramp frames) at a
  constant output size. ruff, format, mypy and pip-audit clean.

**Real run** (A03, A07, B05; $0, API calls 0; 77.3 s; tracks reused)
- A03: zooms 0.00–6.00 and 24.12–28.44 s, ramps 0.00–0.40, 5.60–6.00, 24.12–24.52,
  28.04–28.44 s; 21.1 s.
- A07: zooms 0.08–5.88 and 15.34–21.34 s, ramps 0.08–0.48, 15.34–15.74, 20.94–21.34 s; the
  first hold ends on a cut at 5.878 s, so it switches there without a ramp; 22.0 s.
- B05: zooms 0.10–5.36 and 25.22–31.22 s, ramps 0.10–0.50, 4.96–5.36, 25.22–25.62,
  30.82–31.22 s; 19.2 s.
- Render time with the track reused: 19–22 s, as before the zooms.

**Not done**
- Igor's phone check of the three clips; then the other 32 moments (each analysed once more for
  track schema 5, ~25 s extra). `ramp_s`, `scale` and `min_gap_s` are the knobs.

**Cost:** $0. No API call.

## 2026-10-13 — Brief R2: punch-in zooms (built; 5-clip real run done; Igor's phone check pending)

**Why:** talking-head clips look flat. A hard punch-in (an instant jump to a crop 1.12x tighter on
some sentence starts, then back out) is the podcast-clip look, and it costs nothing: the zoom is
extra fields on the lines of the moving-crop `sendcmd` file from Brief 8 Part B. The zoom points
are picked by a deterministic rule from data the run already has, never by a model (campaign
Rule 3; D-027).

**Done** (9 commits on the branch `brief-r2-zooms` from `main`, plus this one; nothing pushed or merged)
- **`video.zoom`** in `config/default.yaml` (`enabled`, `scale` 1.12, `loud_pct` 0.30, `min_gap_s`
  4.0, `max_hold_s` 6.0, `max_upscale` 2.0; validated, old configs load with zoom off). A campaign
  profile may override any key under `video.zoom`. `enabled: false` writes the command file and
  the clip exactly as before (the old code path, byte for byte). The six values go into
  `RenderedSettings`, so a toggle or a tuning re-renders on resume and an old output is not stale
  while zoom is off.
- **Rule** (`utils/zoom.py`): the sentence holding the clip's first spoken line, plus the loudest
  `loud_pct` of its sentences (mean RMS in dB from the run's WAV, `utils/audio.py`). A pick zooms in
  when it is `min_gap_s` after the previous zoom-out, at least 0.5 s from every camera cut, and the
  shot has at least `min_gap_s` left. The hold ends at the next pick that is `min_gap_s` later, at
  `max_hold_s`, or at the shot boundary (frame-exact at a cut, as the x path already was); an end
  just before a cut moves onto it. Shots without a face are excluded; a `TODO(Brief 8.3)` hook
  excludes stacked shots.
- **Geometry:** the window shrinks by `scale` around the smoothed crop's centre (so the 8.2
  operator smoothing carries on inside a zoom) and is centred vertically on the shot's face
  (`face_cy`, now recorded per shot in the track file: `TRACK_SCHEMA_VERSION` 5, so every track
  is analysed once more). Even pixels, clamped to the frame. Upscale guard: a window that would be
  enlarged more than `max_upscale` gives no zooms and the note `zoom_skipped_upscale` (a 1080p
  source sits at 1.99 with scale 1.12; 720p sources never zoom). Other notes: `zoom_no_audio`,
  `zoom_no_track`. The clip json has `zoom_count` and `zoom_spans`; the console line the count.
- **Overlays untouched:** captions, hook and watermark are drawn after the crop and scale. The
  hook's face avoidance now reads the faces through the zoomed windows.
- **Measured before building:** with the local ffmpeg 9.0.1 a sendcmd line
  `crop w …, crop h …, crop x …, crop y …` reconfigures the crop filter in place; through
  `crop -> scale -> setsar` the output size stays constant and a probe pixel flips on the exact
  frame. Size first when shrinking, position first when growing, so every in-between state fits
  the frame. An ffmpeg-marked test keeps that check.
- Tests: 1992 passed, 3 skipped (103 new); ruff, format, mypy and pip-audit clean.

**Real run** (5 of the 35 Brief 11 campaign moments, Igor's pick; $0, API calls 0; 252.9 s)
- A03: 2 zooms (0.00–6.00, 24.12–28.44 s); A07: 2 (0.08–5.88, 15.34–21.34 s); B05: 2
  (0.10–5.36, 25.22–31.22 s). A01 and A02: 0 zooms and no note: the source is cut every 1–5 s
  (11 and 7 cuts) and its editor cuts on sentence starts, so every pick was within 0.5 s of a cut
  or had under 4 s of shot left. Notes unchanged from the Brief 11.1 render.
- Render time: 42–53 s per clip, of which the one-off schema-5 track analysis is ~20–25 s; A03
  rendered again with its track reused took 21.6 s against 24.9 s before, so the zoom itself
  adds nothing measurable.

**Not done**
- Igor's check on a phone: natural or too frequent; tune `scale` and `min_gap_s` by config. Then
  the other 30 moments (each re-analysed once, ~25 s extra). Out of scope, for later: animated
  or eased zooms, shake, speed ramps, zooms in stacked shots (8.3), model-chosen emphasis words.

**Cost:** $0. No API call.

## 2026-10-12 — Brief 11.1: campaign auto-setup (built; real run done; Igor's phone check pending)

**Why:** after Brief 11's real run two per-campaign settings needed hand-tuning: the watermark
position (top centre covered the head and, with screen blend, was faint on a light wall) and the
name spellings (Whisper wrote one name three ways; one was listed). Igor also wants clips safe to
post: profanity masked, sensitive words flagged. Goal: a new campaign takes about 2 minutes to set
up. $0, deterministic, local, no model (D-030).

**Done** (12 commits on the branch `brief-11-1-auto-setup` from `main`)
- **`watermark.position: auto`.** A grid of centre positions (x 0.5 ± 0.05 steps, y every 0.02)
  is filtered by the geometry checks an explicit position must pass, by the caption envelope (the
  widest, tallest caption box, so no clip's captions can reach the spot) and, per sampled clip,
  by the render's own hook and caption checks. Survivors are scored over the first
  `campaign.auto_watermark_sample` (5) moments being rendered: 0.6 × the share of face samples
  the box overlaps (from the track; 0 with a centre crop) + 0.4 × the median luminance of the
  source pixels behind it on 3 frames per clip, read through the existing ffmpeg frame pipe into
  memory (no frame touches disk). Lower wins; ties go to the spot nearest the captions, then the
  centre. Locked in `campaigns/<slug>/auto_position.json` (score, runner-up, sampled ids, the
  geometry it was chosen for) and reused while the watermark file, scale, blend, frame, safe
  margins, corner zone and hook position stay the same; a stale lock is a warning and a new
  choice; a lock that no longer fits is a clear error. Console and report show the choice.
  TikTok's button column (`safe.button_left: 780`, `safe.button_top: 840`, D-023; the preview
  draws the same values) is a no-go zone: a candidate touching it is dropped (Igor, before the
  real run).
- **`names:`** with automatic spelling rules. Once per run the whole transcript is scanned for
  spans of 1–3 words (never across a sentence end) that look or sound like a name: lower case,
  digit look-alikes mapped (3 1 0 → e i o), difflib ratio and a basic Soundex. Ratio ≥ 0.8 makes
  a rule (a name shorter than 5 letters also needs the Soundex match); 0.7–0.8 with the same
  Soundex is a **suggestion only**, listed but not applied (Igor's decision). An explicit
  `spelling` entry with the same `from` wins, so an identity rule vetoes. A name plus its
  neighbour is not a second variant. `auto_spelling.json` records every variant with status,
  count and up to 3 places it was heard; the report lists rules, suggestions and the veto hint.
- **Word lists (all modes):** `config/wordlists/mask.txt` (51 words, 5 context words, 27 slurs),
  `flag.txt` (30), `never.txt` (16), from `docs/wordlists.md`. `captions.mask_profanity` (default
  on) masks caption words and the hook as first letter + `***` at the one point the normal
  render, campaign mode and the safe-zone preview share; whole word, case kept, common
  inflections, a doubled consonant, never-list wins for single words, phrases match exactly.
  Context words (coke, crack, cutting, lean, molly) are masked only within 2 words of another
  masked word, else flagged. The audio is never changed. Old clip jsons stay valid (the new
  settings field defaults to the new value, so nothing re-renders).
- **Campaign:** `hook.sensitive: warn | fail` (flag words, slurs and lone context words in the
  hook or the post caption; a hook hit fails the moment with `fail`; the hook is never
  rewritten); the post caption's first line is masked, hashtags and the closer are not;
  per-campaign `allowed:` and `extra_masked:`; the campaign's names and spelling targets are
  never masked. Masked and sensitive words go to a local `<clip>.review.txt` (a slur as a dash);
  jsons and console carry counts only; the report shows the masked count and names the
  sensitive words.
- **Watermark format:** a WebP, JPEG, GIF, BMP, AVIF or SVG named `.png` fails with the real
  format named and the advice to convert.
- Refactor: the watermark is measured once (`WatermarkAsset`) and placed apart, so a position can
  be chosen later. `face_rects_timed` keeps the sample time of each face box.
- Tests: 1886 passed, 3 skipped (Whisper, MediaPipe, a symlink test); the ffmpeg tests ran here,
  including an auto-position campaign on a generated source. ruff, format, mypy and pip-audit
  clean. Built by Fable in auto mode, one commit per step, every check run separately.

**Real run** (5 of 35 moments with `--only`, the same 1:37 h source and run folder as Brief 11;
speaker crop with the real face models; $0, API calls 0)
- 5 rendered, 0 failed, 0 refused. Whole command 220 s; per clip 19–24 s of render (tracking of
  the sampled clips happens before, while the position is chosen, and is reused by the render).
  Lengths 39.1–43.3 s from `end: auto`.
- Position: x 0.50, y 0.61, face overlap 0.3% of samples, background luminance 23 of 255, score
  0.038; runner-up x 0.55 at the same height. 588 grid positions, 156 placeable, 124 with room
  for the hook and captions in every sampled clip. The spot is just above the captions, on dark
  clothing: where Igor had moved it by hand after Brief 11. Three clips still carry the binary
  `watermark_over_face` note (a face reaches the box in a few samples).
- Names: 3 rules applied, 1 suggestion, 1 variant already in the spelling list; none of the
  variants is spoken in the five rendered clips (0 fixes). Found in the real transcript: a
  two-word variant and the three-word span containing it were both counted from the same place;
  fixed (a longer span is not a variant when a shorter span inside it already applies). One
  applied rule mapped a three-letter ordinary word to a name (ratio 0.86, same sound); Igor's
  rule since: a span under 4 letters is a suggestion at most. Detection run again without a
  render (no clip text changed): 1 rule applied, 2 suggestions, 1 already in spelling.
- Masking: 8 words masked across 4 clips (profanity, inflections included), 0 sensitive words.
  All eight checks pass on every clip.

**Not done**
- Igor's phone check of the chosen spot and the masked captions; his veto or OK on the applied
  name rules (the report lists them).
- Out of scope, for later: changing the hook mode automatically; Whisper hot words for names;
  a dictionary to tell a real word from a misspelled name (today the report and the identity
  veto are the guard).

**Cost:** $0. No API call.

## 2026-10-12 — Brief 11: campaign mode (built; real run done; Igor's phone check pending)

**Why:** clipping campaigns (D-024) forbid tools that pick the moments, and require a supplied
watermark on screen for the whole clip, names spelled correctly in the captions, and a post
caption that ends in an approved closer. Igor picks the moments; the software renders them
(D-026). Campaign mode makes no Claude API call.

**Done** (11 commits on the branch `brief-11-campaign-mode`, plus this one; nothing pushed or merged)
- **`clipmachine campaign <source-video | run-folder> --campaign <slug> [--only ids] [--force-reuse]`**
  and **`clipmachine campaign-posted --campaign <slug> --id <id> --platform <p>`**.
- **Campaign folder** `campaigns/<slug>/` (git-ignored; only the synthetic `_example` is tracked):
  `campaign.yaml`, `moments.yaml`, `used.json`, `assets/`. Both YAML files are validated; an
  error names the entry and stops the command before anything is rendered.
- **Times:** each end of a moment moves to the nearest clean sentence edge within 1.5 s
  (`campaign.max_snap_seconds`), on its own; otherwise the given time is kept with a warning.
  `end: auto` picks the clean edge nearest to start + 40 s (`campaign.auto_end_target_s`) inside
  the campaign's clip length. A moment outside the length range fails alone; the others render.
- **Captions:** the R1 karaoke captions with the campaign's spelling fixes (whole word, any case,
  a multi-word entry merged into one caption word). The transcript files are never touched.
- **Hook:** `hook.font_size` is now a general setting (default 80) for normal runs too. In
  campaign mode the hook stays for the whole clip, starts below the watermark, and then avoids
  faces as in R1. A hook that would be cut short fails the moment.
  After the real run: `hook.seconds` is 3 s (was 2) for every run, and `hook.position: top |
  avoid_faces` (default top: the top of the safe area, even over a face, no face note). A
  campaign may set its own `hook.seconds` and `hook.position` (left out, the config applies).
- **Watermark:** a PNG drawn on top of video, captions and hook for the whole clip, unmodified
  apart from uniform scale. `watermark.blend: normal | screen` (screen hides an opaque black
  background). Every geometry check uses the visible content box, measured once at load, so the
  canvas may reach outside the frame. Frame, corner zones (15%), safe area, captions and hook are
  hard checks; a face under the watermark is a note.
- **Audio:** the source's own audio with the existing fades; the ffmpeg arguments are checked for
  exactly one audio stream before every render.
- **Outputs** in `runs/<run>/campaign/<slug>/`: `<id>_<platform>.mp4`, `.post.txt` (prefix and
  caption, hashtags if any, the closer last), `.json` (requested and final times, notes, counts,
  the eight checks) and `campaign_report.html` (a card and a checklist per moment).
- **Used log:** every render is recorded; closers rotate round-robin and never repeat back to
  back; a posted moment is refused unless `--force-reuse`; a new moment that overlaps a posted
  one by more than half its length renders with a warning.
- **Run reuse:** a source file reuses the run made from the same file (path, size, modified
  time). Fetch now records the modified time in `meta.json`; an older run is matched on path and
  size with a printed warning. The reused folder is always printed.
- **Zero API:** the command runs with the kill switch on, the pipeline stops after transcribe,
  and the cost files are compared before and after.
- Built by a Sonnet 5.5 (Medium) subagent one step at a time; each diff reviewed by the planner,
  who re-ran every check separately before each commit. **Review caught two things:** the audio
  check matched filter names inside file paths (a campaign folder whose name contains "sine"
  would have failed every render), and the run lookup compared paths case-sensitively on Windows.
- Tests: 1785 passed, 3 skipped (Whisper, MediaPipe, a symlink test that needs privileges). The
  ffmpeg tests ran here: both blends and a whole campaign render from a generated source. ruff,
  format, mypy and pip-audit clean.

**Measured before building** (a generated source, this machine's ffmpeg)
- A screen blend of a 1600×534 black-background PNG onto a 1080×1920 frame leaves the area under
  the black margin within 1 level of 255 on average of a render without it; the mark is drawn at
  full brightness in the first and the last second; frame count and the single audio stream match
  a plain render.

**Real run** (3 of 35 moments with `--only`, a 1:37 h source whose run folder already held the
transcript; speaker crop with the real face models; $0, API calls 0, checked by the ledger and
cost files)
- 3 rendered, 0 failed, 0 refused. Whole command 158 s; per clip 62, 50 and 46 s (tracking plus
  encode). Lengths 41.2, 43.3 and 39.1 s from `end: auto` (target 40 s); starts moved 0, 0
  (no clean edge within 1.5 s, kept with a note) and +0.15 s.
- All eight checks pass on every clip. Every clip carries the note "watermark over a face"
  (the mark sits at the top centre, where the head is in a speaker crop) and the hook moved
  below the watermark and away from faces; one hook shrank to fit.
- 0 spelling fixes: none of the listed names is spoken in these three stretches. Across the
  whole transcript Whisper writes the streamer's name in three different ways, two of which were
  not in the spelling list yet.
- The supplied watermark was a WebP with a `.png` name and no alpha, on an opaque black
  background; the loader rejected it (bad PNG signature). Converted locally to a real RGB PNG
  with OpenCV (pixels identical), `blend: screen`, `scale: 0.6`. The mark's brightest pixel is
  118 of 255, so with screen blend it is faint on light backgrounds; Igor's phone check decides.

**Not done**
- Igor's check of the three clips at phone size (watermark legible, hook from frame 1, captions).
- Out of scope, for later: skip moments whose clip is already rendered and unchanged (today every
  run renders all selected, unposted moments again; `--only` limits it); Whisper hot words for
  names; the AI scout command.

**Cost:** $0. No API call.

## 2026-10-12 — Brief R1: captions v2 (karaoke box), hook overlay, safe zones (built; real run done; style approved; MVP re-rating pending)

**Why:** make clips more watchable at $0: captions that read at a glance and do not feel cramped,
the hook line on screen at the start, and nothing under the TikTok, Reels and Shorts buttons.

**Order:** the brief places R1 after Brief 8.3 and the edit-point guard. Igor chose to run it
first (2026-10-12): normal shots only, with a `TODO(Brief 8.3)` where stacked shots plug in.

**Measured before building** (a spike on a synthetic colour source, this ffmpeg's libass)
- libass turns the ASS font size into letter size through the font's OS/2 win ascent and descent:
  Montserrat Bold at 64 has a 41 px em and capitals about 28 px tall (the old 80: about 35 px).
- A drawn rounded box sized from the font file's advance widths sits on the text as intended
  (estimated text width 594 px, measured ink 590 px).
- `\k` switches each word from the secondary to the primary colour at its start; `\fsp` on the
  space character alone widens the gap between words.

**Done** (8 commits plus the brief and this one, nothing pushed)
- **Captions (D-022, `captions.style: box_karaoke`, the new default):** one line of at most 4
  words on a dark rounded box, 75% opaque; spoken words yellow, upcoming words white, native ASS
  karaoke with one event per line; the gap between words is 0.45 × font size. A line is also
  split when it is too wide for the box or at a pause of 0.5 s. `style: legacy` keeps the old
  captions, and a run folder made before R1 re-renders as it did (a golden test compares the
  `.ass` bytes).
- **Hook:** `hook_line` as white text on the same dark band for the first 2 s with a 150 ms
  fade-out, at most 2 lines, at the top of the safe area. It moves below a face that is under it
  in those 2 s. An empty hook shows nothing and leaves a note in the clip's json.
- **Safe zones (D-023):** top 290, bottom 672, left 120, right 192, in config. Captions and hook
  are centred on the safe area (x = 504). The clip's json counts anything outside the margins.
- **Track files are schema 4:** the face boxes of every sample of the whole clip are saved (Igor:
  Brief 11's full-length hook and watermark need them too, so one schema change instead of two).
  Existing run folders analyse each clip again once.
- **`clipmachine safe-zones <run> --clip <id>`:** renders one clip with the margins and TikTok's
  button column drawn, into `runs/_scratch/safe-zones/` only; the run folder is only read.
- Hook text and transcript words are untrusted: `{`, `}`, `\` and newlines are removed before
  they reach the `.ass`. Neither appears in a clip's json or in the log.
- Built by a Sonnet 5.5 (Medium) subagent in two steps; each diff reviewed by the planner, who
  re-ran every check separately. **Review caught one thing:** a word with no length (Whisper
  writes some) was dropped from karaoke captions (fixed with tests).
- Tests: 1413 passed, 2 skipped. ruff, format, mypy and pip-audit clean.

**Real run** (`runs/2026-10-12_t3`, render and report only, `captions.font_size: 96` in that run's
config at Igor's request; `default.yaml` stays at 64 until he has seen it on a phone)
- 6 clips rendered in 221 s wall time; per-clip render times add up to 217 s (226 s before R1).
  130 s of that is the one-time track analysis for schema 4.
- Caption lines per clip: 35 to 80; at most 4 words on a line. At size 96 many lines hold one
  word (14 to 26 lines per clip).
- Nothing outside the safe margins in any clip (0 of 6). Caption boxes span x 124–884 and
  y 1141–1236; the widest is 760 px.
- **Hook: shown in 6 of 6, moved below a face in 6 of 6.** In these close-up crops the face
  starts at y 260–440, so a band at the top of the safe area (y 290) always touches it; the band
  lands at y 749–1051, between the face and the captions. The same happens at sizes 80 and 64.
- Hook text at size 96: shrunk in 4 of 6 and cut short with an ellipsis in 2 of 6 (hooks of 8 and
  11 words). At 80 and 64 none would be cut.
- Safe-zone preview of one clip rendered in 13 s into `runs/_scratch/safe-zones/`; the run
  folder's files were identical before and after (hashes).
- No real frame was looked at by Claude: placement was checked from the numbers in the `.ass`
  files and the clip json. The pictures are Igor's to judge.

**Igor's check** (c05, c14 and c01 on his phone, size 96): style approved, font size good, hook
placement fine; the captions sat a bit too high.
- **Changed after it (D-022 update, D-023 update):** `captions.font_size` 64 → 96 and
  `safe.bottom` 672 → 560 in `config/default.yaml`. The 560 margin still has to be checked
  against the real TikTok UI with a draft upload.
- **Second render** of `runs/2026-10-12_t3` with both values: 6 clips in 98 s wall time (tracks
  reused, no analysis). Caption boxes now sit at y 1253–1348 in every clip (were 1141–1236);
  hook bands unchanged at y 749–1051; still nothing outside the margins (0 of 6).
- Tests after the change: 1413 passed, 2 skipped; ruff, format, mypy and pip-audit clean.

**Not done**
- Stacked-shot caption and hook placement, and their tests: moved to Brief 8.3.
- Font weight 500–600: only Montserrat Bold (700) is installed.
- Igor's re-rating of all six clips under the MVP rubric (the next EVAL.md row).

**Cost:** $0. No API calls, no new dependency.

## 2026-10-12 — Brief 8.2: camera polish (built; real run done; Igor's re-rating pending)

**Why:** both needs-edit clips of `runs/2026-10-11_t3-2` (c09, c04) fail only on the camera: late
switches, missed short replies, laugh steals, robotic motion.

**Measured before choosing** (the eight kept clips at 30 samples per second, in memory; then
Igor's turn times for the two-face shots of c04 and c09, whole seconds; numbers only)
- **Cause:** in 7 of the 9 two-face shots one face has a readable mouth in 0–9% of frames (the
  other 87–100%). The old signal can never show that person: 1 switch in all eight clips.
- **The problem is missed turns, not slow switches:** when a signal sees a turn, the cut lands
  0–0.2 s from the labelled time.
- Switches in two-face shots, all eight clips, under the old rule: mouth variance 1; mouth
  against loudness 2; pixel motion 10; pixel motion against loudness 27; the mix (D-019) 8.
- Lip sync is real but weak: mean correlation 0.145 with the sound 67 ms after the picture in
  close-ups, about 0.03 at control lags. Pixel motion against loudness: 0.106 at 33 ms.
- An audio-only voice reference (spectrum clustering, pitch) did not separate the two voices.

**Done** (4 commits plus this one, nothing pushed)
- **Inputs:** pixel motion round the mouth per face (frame difference of the lower face less the
  upper face), the loudness envelope of `audio.wav` (`utils/audio.py`), and
  `speaker.detect_every` (face boxes every Nth sample, never reused across a cut).
- **Speaker signal (D-019, `speaker.signal: mix`):** per window, mouth openness against loudness
  where every main face of the shot has a readable mouth, pixel motion otherwise. Shortest turn
  that gets a cut: 1.0 s in sync windows, 1.5 s in pixel-motion windows. `signal: mouth` keeps the
  old rule. Track files are schema 3 and record how many speech windows each signal decided.
- **Motion:** inside a shot the face path is averaged with a centred window (0.5 s), the crop
  holds while the face stays within 15% of the crop width (was 8%), and then makes one eased glide
  of at least 0.6 s, capped at 0.8 crop widths per second, starting before the person has moved.
  Switches and cuts stay hard. `speaker.smoothing` is accepted but unused.
- Built by a Sonnet 5.5 (Medium) subagent in three steps; each diff reviewed by the planner, who
  re-ran every check separately. **Review caught two things:** face boxes reused across a camera
  cut (fixed with a test), and a glide that could exceed the speed cap after its destination was
  recomputed (fixed).
- Tests: 1242 passed, 2 skipped (was 1151). ruff, format, mypy and pip-audit clean.
- Pinned test values that changed: the exponential-smoothing tests (300 and 600) are replaced by
  glide tests with hand-worked values; the dead-zone test uses a ±20 px wobble for the 15% zone;
  tests of the mouth rule through tracking and render pin `signal: mouth`; schema pins 2 → 3.

**Against Igor's turn times, with the built code** (c04 and c09, 30 samples per second, boxes
every third sample; share of speech time with the camera on the labelled speaker)

| | c04 (5 real switches) | c09 (3 real switches) | Both |
|---|---|---|---|
| Never switching | 68% | 89% | |
| Old rule | 68%, 0 switches | 45%, 1 switch | 58% |
| Mix, settings as built | 67%, 4 switches (2 false), 1 turn missed | 92%, 0 switches, 1 missed | 77% |
| Mix, coarse grid of 18 settings | | | 74–81% |

- Grid: window 0.25 / 0.5 / 1.0 s, confirm 1 / 2, ratio 1.0 / 1.5 / 2.0. It is flat, so the
  settings stay at window 0.5 s, confirm 2, ratio 1.5.
- Analysis time at 30 samples per second with boxes every third sample: 33.7 s, 43.1 s and 65.6 s
  for clips of 29.7 s, 44.7 s and 58.5 s (about 2 to 2.5 times before).

**Gaps and risks**
- **The mix does not beat never switching on these labels.** It is better than the old rule,
  and its right switches are prompt, but about half its switches in c04 are wrong. The 76% / 85%
  reported before the build came from a simpler rule that did not hold up.
- At wide-shot face sizes no visual signal is dependable: pixel motion is right in about 60% of
  c04's windows, and the readable person's mouth openness barely moves in about half the windows
  labelled as their speech. Four other motion scores were no better (59–65% against 71%).
- A person's face lost for more than 0.3 s comes back as a new track.
- The evidence is two clips of one video with labels good to about half a second.
- `speaker.sample_fps` and `detect_every` still default to 10 and 1; the mix was measured at 30
  and 3. The defaults change with Part 4.

**Parked**
- A laugh can still win in pixel-motion windows (Igor: accept for now).
- Diarization (who is talking, from the audio) as the dependable answer for wide shots.
- A reply by a person outside the source frame cannot be shown.

**Real run** (`runs/2026-10-11_t3-2`, local, $0; `speaker.sample_fps` 30, `detect_every` 3 and
`dead_zone` 0.15 set in the run folder's config, the files from before in `runs/_scratch/b82/before/`;
`track --preview` 366 s, then `--from-stage render` 108 s with every track reused)

| Clip | Length | Crop changes (was) | Moving episodes (was) | Time moving (was) | Switches (was) | Windows sync / motion / unscored | Analysis (was) |
|---|---|---|---|---|---|---|---|
| c05 | 20.3 s | 2 (4) | 0 (1) | 0% (0%) | 1 (0) | 4 / 31 / 4 | 27.3 s (12.9 s) |
| c06 | 35.1 s | 35 (69) | 2 (7) | 3% (6%) | 0 (0) | 9 / 49 / 10 | 46.5 s (19.4 s) |
| c09 | 44.7 s | 19 (20) | 1 (5) | 1% (1%) | 0 (1) | 55 / 14 / 16 | 51.8 s (23.4 s) |
| c04 | 29.7 s | 5 (8) | 0 (2) | 0% (1%) | 4 (0) | 11 / 30 / 9 | 37.6 s (16.5 s) |
| c12 | 58.5 s | 20 (47) | 1 (5) | 1% (2%) | 2 (0) | 17 / 95 / 2 | 71.8 s (32.9 s) |
| c13 | 28.5 s | 52 (100) | 3 (6) | 6% (11%) | 2 (0) | 1 / 55 / 0 | 36.2 s (17.1 s) |
| c01 | 40.0 s | 287 (325) | 15 (26) | 24% (26%) | 0 (0) | 31 / 30 / 18 | 43.1 s (21.2 s) |
| c02 | 43.6 s | 20 (97) | 1 (12) | 1% (7%) | 0 (0) | 37 / 39 / 9 | 47.8 s (21.7 s) |

- Crop changes in all: 440 (was 670). A moving episode is a run of crop changes with no pause of
  more than 0.25 s; they fall from 64 to 23. Each glide now changes the crop on every frame, so a
  glide counts for many changes. Fastest movement inside a shot: 485 pixels per second, the cap.
- c01 still moves for 24% of its time, in 15 glides.
- Analysis: 362 s for the eight clips (was 165 s), about 1.2 s per second of clip.
- All eight in speaker mode, no fallback.

**Held-out stretch** (c12, 15–35 s, labelled by Igor after the build and never used in tuning: one
person speaks throughout, so every switch is false)
- Share of time on the speaker: **87.5%** (17.5 of 20 s). Switches: **2, both false** (away at
  17.0 s, back at 19.5 s). The old rule made no switch here: 100%.

**Reading:** on the two tuning clips the mix equals never switching; on the held-out stretch it
is worse. The motion change does what it was built for. Whether the mix stays the default is
Igor's decision after watching c09 and c04; `speaker.signal: mouth` keeps the old speaker choice
with the new motion.

**Igor watched the mix render** (2026-10-12)
- c04 good (cuts sometimes slightly early). c13 good: he liked the brief cut to the other
  person's reaction on a chuckle. c09 missed every turn of the second person. c12 a false cut
  while only one person talked.
- **Decision (D-019):** `speaker.signal: mouth` as the interim default, keeping the new motion.
  Every switching problem is in wide two-person shots; those get their own layout in Brief 8.3
  (stacked layout) instead of a better guess at who is speaking. The mix stays in the code as
  `speaker.signal: mix`.

**Re-render with the interim default** (`signal: mouth`, 10 samples per second as before,
`dead_zone` 0.15; `--from-stage render`, 262 s with every track analysed again; the mix run's
records are in `runs/_scratch/b82/mix/`)

| Clip | Crop changes (original run) | Moving episodes (original) | Time moving (original) | Switches (original) |
|---|---|---|---|---|
| c05 | 1 (4) | 0 (1) | 0% (0%) | 0 (0) |
| c06 | 38 (69) | 2 (7) | 3% (6%) | 0 (0) |
| c09 | 3 (20) | 0 (5) | 0% (1%) | 1 (1) |
| c04 | 1 (8) | 0 (2) | 0% (1%) | 0 (0) |
| c12 | 20 (47) | 1 (5) | 1% (2%) | 0 (0) |
| c13 | 73 (100) | 4 (6) | 8% (11%) | 0 (0) |
| c01 | 305 (325) | 16 (26) | 25% (26%) | 0 (0) |
| c02 | 21 (97) | 1 (12) | 1% (7%) | 0 (0) |

- The shots are the same as in the run Igor rated for EVAL.md row 1 (same speaker choice, same
  times); only the movement inside a shot differs. Crop changes in all: 462 (was 670); moving
  episodes 24 (was 64). Analysis: 156 s for the eight clips (was 165 s).
- The preview files in the run folder are from the mix run and no longer match the tracks.

**Parked (added)**
- **Reaction shots on punchlines are wanted.** A short cut to the listener's laugh read as good
  editing in c13, so "a laugh never takes the camera" is not the goal; a later brief should make
  it a choice (short reaction cuts, then back).
- Brief 8.3: a stacked layout for wide two-person shots.

**Part 4: `video.crop` defaults to `speaker`** (D-020)
- `config/default.yaml`: `crop: speaker`. The other speaker defaults are the rated ones
  (`signal: mouth`, 10 samples per second, boxes on every sample) plus the new motion.
- A run that will render in speaker mode checks both face model files before any stage runs
  (`preflight.check_face_models`, called for a new run and for a resume whose render stage is not
  done). A missing or wrong file stops the run with the fetch command and the `center`
  alternative: no run folder is created for a new run, and a resumed folder is left untouched.
  Nothing is downloaded by `run`. The render stage's own check stays.
- A run folder keeps its own crop mode: one whose config says `center` renders the centre crop
  and needs no model files (tested).
- README: the one-time `fetch-face-model` step.
- Tests: 1250 passed, 2 skipped. ruff, format, mypy and pip-audit clean. The test project
  fixture pins `crop: center` in its copy of the default config, so tests that are not about
  speaker mode need no model files; one test reads the shipped default.
- Igor watched c01 and c13 of the re-render (the version that became the default): OK.

**Fresh one-command run with the new defaults** (`runs/2026-10-12_t3`, T3, client `general`, one
command, no config edit and no manual step)
- Finished clips in speaker mode from one command: exit 0, **7 min 9 s** wall time (sum of the
  stage times in run.json 424 s: fetch 3 s, transcribe 166 s, candidates 13 s, score 16 s, render
  226 s, report under 1 s).
- **Cost $0.0541** (6 calls: candidates $0.0032, score $0.0509; 39,207 input and 3,707 output
  tokens).
- 16 candidates (17 returned, 1 duplicate), **6 kept**, 8 rejected, 1 dropped as an overlap, 1 as
  `adjust_ignored`. Candidate ids are new in this run.

| Clip | Score | Length | Crop mode | Fallback | Cuts | Switches | Crop changes | Analysis |
|---|---|---|---|---|---|---|---|---|
| c05 | 8 | 23.5 s | speaker | none | 1 | 0 | 1 | 14.6 s |
| c09 | 8 | 56.0 s | speaker | none | 6 | 0 | 2 | 28.1 s |
| c11 | 8 | 50.0 s | speaker | none | 1 | 0 | 37 | 23.1 s |
| c14 | 8 | 32.3 s | speaker | none | 0 | 0 | 91 | 17.7 s |
| c01 | 7 | 40.0 s | speaker | none | 3 | 0 | 305 | 19.9 s |
| c12 | 7 | 58.5 s | speaker | none | 1 | 0 | 20 | 31.2 s |

- The run folder's frozen config holds `crop: speaker`; every track was made with `signal: mouth`,
  10 samples per second and the 15% dead zone.
- Fewer clips than the 8 of `runs/2026-10-11_t3-2`: the candidates stage returned a different set
  (16 against 17) and two keeps were dropped by the overlap and weak-boundary rules. Nobody has
  watched these six yet.

**Igor's ratings of `runs/2026-10-12_t3`** (EVAL.md row 2)
- **Usable: 5** (c05, c11, c14, c01, c12). **Needs edit: 0. Reject: 1** (c09, checks C1 and C2: it
  starts without its context, then runs into a different conversation, and ends mid-sentence).
  No camera complaints.

**Diagnosis of c09** (read-only; 1448.44–1504.45 s of the source, 56.0 s; numbers only)
- **Root cause: the clip crosses an edit point of the source.** T3 is a compilation (Igor). The
  track has a burst of 6 camera cuts between 17.07 s and 19.94 s of the clip (1465.51–1468.38 s
  of the source), no face for the 2.9 s between them, and a different framing afterwards (face at
  0.52 of the frame width, 0.68 before). The transcript changes subject in the same place.
- How it got there: the moment the find model chose was 16.5 s long (1452.15–1468.67 s) and
  ended at the edit point. It was under the 20 s minimum, so snapping grew it by 4 sentences at
  the end (2 to reach the minimum, 2 toward the preferred length), across the edit point, to
  1497.00 s. The judge then extended it by one sentence at each end.
- Start: kind `.`, gap 0.82 s. The judge's one extra sentence is inside the same run of thought;
  the two sentences that set it up come before and were not taken.
- End: kind `.`, gap 0.86 s, so it passed the clean-cut rules. But the full stop is inside a
  sentence: the speaker pauses and the same sentence goes on after it. The candidate's own end
  (1497.00 s, gap 1.48 s) was a complete sentence; the judge's extension moved the end onto the
  false full stop, giving "ends cleanly on the finding" as its reason, while the finding is in
  the next sentence.
- **Not caused by the style prompt (D-017):** the transcript from before D-017
  (`runs/2026-10-11_t3`) has the same full stop there, with a 0.98 s gap.
- Audio at the edit point: a 0.7 s stretch below -65 dB (1468.03–1468.73 s) inside the burst of
  cuts. On its own that proves little: the recording has 54 such stretches (its quietest 5% is at
  -65 dB), and c11, rated usable, has 6 inside it. The camera evidence is the strong one.

**Parked (added)**
- **Source edit points: clips must not cross them (compilations).** A burst of camera cuts with
  no face in between marks one in c09. Candidate growth and the judge's extensions should stop
  at such a point. Not built or measured beyond this one clip.
- A full stop that Whisper writes at a pause inside a sentence passes the clean-cut rules
  (kind `.`, gap 0.86 s). One case seen.

**Next:** Brief 8.3 (stacked layout for wide two-person shots).

## 2026-10-11 — MVP walking-skeleton finish line met on private validation (6 usable of 8); public samples pending permissioned content (D-012)

**Igor's ratings** (the eight clips in `runs/2026-10-11_t3-2`, speaker crop)
- **Usable: 6** (c05, c06, c12, c13, c01, c02). **Needs edit: 2** (c09, c04). **Reject: 0.**
- Both needs-edit clips fail **C6**: the camera switches late in fast back-and-forth and misses
  short replies.
- **No complaints on start or end cuts** (C1). The four cut-point failures of the earlier T3 run
  do not come back.

**Against the finish line (MVP.md section 1)**
- Quality: 6 usable, against the 3 required.
- Measured: row 1 of `docs/EVAL.md` (17 candidates, 8 kept, 6 / 2 / 0, $0.0560, 7 min 58 s as the
  sum of the stage times in run.json).
- Safe stop: everything is committed (nothing pushed); 1151 tests pass, 2 skipped.
- Differences from the wording of the finish line: the video is 41 minutes, not 10–15; the client
  is `general`; and the rated clips needed one manual step, since the one-command run finished
  with the centre crop and the speaker crop was set in the run folder and re-rendered.
- The video is third-party and private (D-012), so none of these clips can be shown. Public
  samples need permissioned content.

**Next:** Igor chooses the next brief (both needs-edit clips point at camera switching, the
parked Brief 8.2) and finds permissioned content for public samples.

## 2026-10-11 — Brief 10: real sentence boundaries and clean cut edges (built; real runs done on T3 and T1)

**Why:** Igor rated the seven finished T3 clips: c09 and c15 usable, c07 needs camera fixes, and
c05, c11, c12, c13 fail on cut points. Those four are exactly the kept clips with a boundary on a
fallback edge (a Whisper segment edge used as a sentence end after 40+ unpunctuated words), each
with a 0.00 s gap.

**Measured before choosing** (T3 and T1 re-transcribed locally, 18 runs, outputs only in
`runs/_scratch/b10/`; numbers only)

| Setting | T3 long runs | T3 fallback edges | T3 lost stretches | T1 long runs | T1 fallback edges | T1 lost stretches |
|---|---|---|---|---|---|---|
| a: today (`small`, conditioning off) | 45.3% | 59 | 0 | 26.9% | 25 | 0 |
| b: `initial_prompt` | 45.3% | 59 | 0 | 24.4% | 22 | 0 |
| bh: prompt in every window | 10.9% | 5 | 6 | 7.2% | 4 | 0 |
| c: conditioning on | 4.8% | 1 | 0 | 65.0% | 61 | 0 |
| **bhc: both** | **4.8%** | **0** | 0 | **8.8%** | **6** | 0 |
| d: `turbo` | 31.4% | 36 | 0 | 73.1% | 86 | 0 |
| dh: `turbo` + prompt in every window | 13.5% | 0 | 1 | 5.4% | 1 | 0 |
| dc: `turbo` + conditioning on | 6.3% | 3 | 0 | 5.1% | 4 | 0 |
| dhc: `turbo` + both | 7.0% | 6 | 0 | 4.9% | 3 | 0 |

- "Long runs" = share of words inside runs of more than 40 words with no sentence end. "Lost
  stretches" = gaps of 8 s or more with no words that the baseline does not have.
- Chosen: **bhc**, D-017. Run (a) reproduced the stored transcripts word for word.
- Repetition loops (a 3–12 word phrase three or more times in a row): never more than the
  baseline (T3: 3 today, 1–3 in the variants; T1: 0 everywhere). No 30 s window gained words.
- Time for bhc: 182 s on T3 (145 s today), 154 s on T1.
- Word gaps against `audio.wav`: a gap of 0.35 s or more has real silence nearby 79% of the time;
  under 0.15 s, 5–10%. About a quarter of punctuated sentence ends have a gap under 0.15 s (T3
  24.9%, T1 24.6%).
- Whisper's word times show pauses almost only at punctuated ends, so pause splitting adds little
  (8 pause boundaries on T3, 23 on T1 with the bhc transcript).
- With bhc and the new rules: 582 clean cut points on T3 (about 14 a minute) and 432 on T1 (about
  12 a minute); longest stretch with no clean cut point 31.0 s and 21.6 s.

**Done** (6 commits plus this one, nothing pushed)
- **Transcribe:** `transcribe.style_prompt` is sent to Whisper as `hotwords` (every window);
  `condition_on_previous_text: true`. The stage warns, with counts only, when a transcript has
  stretches of 8 s or more without words.
- **Sentence index (`utils/snapping.py`):** every sentence carries how it ends (`.` `?` `!`
  `pause` `edge`) and the gap after it. A run of more than 40 unpunctuated words is split at pauses
  of `snap.min_pause_seconds` (0.35), then at its largest gap (an `edge`). A punctuated end with a
  gap under `snap.min_cut_gap` (0.15) is an `edge`. Whisper segment edges are no longer used.
- **Cut times:** the cut sits in the middle of the pause, at most 0.25 s before the first word
  and 0.5 s after the last (was a fixed 0.2 s). `candidates.snap_padding_seconds` is accepted
  but unused.
- **Candidates:** snapping runs over the stretches between clean cut points, so a clip never
  starts or ends on an `edge`. candidates.json records the snap settings.
- **Score:** each numbered line shows `start=ok|no end=<kind>`; the prompt says where a clip may
  start and end and that a repair only described in the reason is not applied. An adjustment that
  would start or end on a weak boundary is ignored, so the clip is dropped as `adjust_ignored`
  (`adjusts_on_weak_boundary` counts them). scored.json and the report show each clip's start and
  end kind and gap. A candidates.json from before this brief is refused with a clear message.
- **Render:** 0.08 s audio fade-in and fade-out (`render.audio_fade_seconds`); older outputs
  re-render.
- **Length:** `clip.max_seconds` 60 → 90 and the `general` client's `clip_length.max` 60 → 90
  (D-018).
- Built by a Sonnet 5.5 (Medium) subagent; each diff reviewed by the planner, who re-ran every
  check separately. Tests: 1151 passed, 2 skipped (was 1051). ruff, format, mypy and pip-audit
  clean.
- **Pinned test values that changed, by reason** (each checked by hand, none regenerated):
  mid-gap cut times (about 55 values: with the fixtures' 0.2 s gaps every start is 0.1 s later,
  every end 0.1 s earlier and every length 0.2 s shorter; 0.15 s pads where the gap is 0.3 s);
  segment-edge fallback replaced by pause and largest-gap splits (5 tests); the end of the
  recording is never an edge (2); snapping over clean cut points (1); the judge's line format,
  the removed segment-edge attributes and the prompt wording (5 tests and one shared pattern);
  max 90 (9); the new no-words warning on a fixture with a 59.5 s gap (1); `padding` became
  `start_pad` / `end_pad` and the four "padding limited" note checks went with the notes.
  Three tests needed a new input to keep their intent: the overlap test (the old ranges
  now overlap by exactly 50%), the over-the-maximum extension (it now needs more than 90 s) and a
  config check that used 70 s as "over the maximum".

**Real runs** (fresh run folders `runs/2026-10-11_t3-2` and `runs/2026-10-11_t1-2`, client
`general`; the older folders are the "before")

| | T3 before | T3 after | T1 before | T1 after |
|---|---|---|---|---|
| Words in long unpunctuated runs | 45.3% | 4.8% | 26.9% | 8.8% |
| Fallback edges (old rule) | 59 | 0 | 25 | 6 |
| Clean cut points | 400 | 582 | 340 | 432 |
| Transcribe time | 138 s | 152 s | 121 s | 142 s |
| Candidates | 18 | 17 | 19 | 19 |
| Kept | 7 | 8 | 8 | 8 |
| Kept clips with a boundary on an edge or a gap under 0.15 s | 5 | 0 | 5 | 0 |
| Cost | $0.051 (score only) | $0.0560 | $0.0505 (score only) | $0.0585 |

- **Target met on both:** no kept clip starts or ends on an `edge` or on a gap under 0.15 s.
  Smallest gap at a kept cut: 0.16 s on both videos. No transcript warnings (no stretch of 8 s
  or more without words).
- **T3** (17 candidates, 8 kept, 6 rejected, 2 outside the top 8, 1 `adjust_ignored`; 8
  extensions and 4 trims applied; score call 15,155 input and 2,247 output tokens). Re-rendered
  with `video.crop: speaker` (set in the new folder's `config.resolved.yaml`, `--from-stage
  render`, local): all eight in speaker mode, no fallback.

| Clip | Score | Length | Start: kind, gap | End: kind, gap | Adjustment |
|---|---|---|---|---|---|
| c05 | 8 | 20.3 s | `.` 2.00 s | `.` 0.68 s | trimmed, end +1 |
| c06 | 8 | 35.1 s | `.` 0.38 s | `.` 2.15 s | trimmed, end +2 |
| c09 | 8 | 44.7 s | `.` 1.50 s | `.` 0.60 s | start +1, end +1 |
| c04 | 7 | 29.7 s | `.` 0.16 s | `?` 1.36 s | trimmed |
| c12 | 7 | 58.5 s | `.` 0.56 s | `.` 0.30 s | end +1 |
| c13 | 7 | 28.5 s | `.` 0.26 s | `.` 0.16 s | trimmed, end +1 |
| c01 | 6 | 40.0 s | `?` 0.90 s | `.` 0.22 s | start +1 |
| c02 | 6 | 43.6 s | `.` 0.18 s | `.` 0.20 s | start +1, end +1 |

- **T1** (19 candidates, 8 kept, 10 rejected, 1 `adjust_ignored`; 6 extensions and 2 trims
  applied; score call 16,250 input and 2,295 output tokens). Centre crop.

| Clip | Score | Length | Start: kind, gap | End: kind, gap | Adjustment |
|---|---|---|---|---|---|
| c05 | 8 | 61.4 s | `?` 0.74 s | `.` 0.78 s | end +1 |
| c06 | 8 | 48.2 s | `.` 0.24 s | `.` 0.24 s | end +1 |
| c07 | 8 | 47.6 s | `.` 0.58 s | `?` 0.30 s | trimmed |
| c12 | 8 | 54.6 s | `.` 0.22 s | `.` 0.50 s | start +1 |
| c14 | 8 | 40.9 s | `.` 0.28 s | `.` 0.72 s | start +1, end +1 |
| c04 | 7 | 42.6 s | `.` 0.16 s | `.` 0.30 s | start +1 |
| c10 | 7 | 43.5 s | `.` 0.38 s | `.` 0.26 s | none |
| c13 | 7 | 32.7 s | `?` 0.56 s | `.` 0.20 s | start +1, trimmed |

- **One clip lost on each video to the weak-boundary check:** the judge kept T3 c11 and T1 c17
  (both score 7) with an adjustment that ended where there is no pause; each was dropped as
  `adjust_ignored`. Neither run ended with fewer than 8 kept clips.
- One kept clip is over the old 60 s limit (T1 c05, 61.4 s).
- Candidate ids are new in these runs, so c05 here is not the earlier c05.

**Gaps and risks**
- **Nobody has listened to the new clips yet.** The numbers say the cuts sit on real sentence ends
  with a gap; whether they sound right is Igor's re-rating.
- The measurement is two videos, one prompt wording, one run each. Conditioning alone and the
  prompt alone each failed on one video; only the combination held on both.
- The 0.15 s rule removes about a quarter of today's sentence-end boundaries. T3's c15 starts on
  a 0.14 s gap and was rated usable, so the threshold may be slightly strict.
- A "pause" is Whisper's estimate from word times, not measured silence: in the 0.15–0.35 s band
  only about 28% have real silence nearby by a strict measure. The fades hide clicks, not a
  clipped word.
- The judge can still lose a clip by choosing a weak boundary (it is told, and the count is
  recorded).

**Parked**
- Read pauses from `audio.wav` (loudness per 10 ms) instead of word gaps, and place the cut in
  the quietest part of the gap. Dry run on T3: fewer punctuated ends pass (46–54%) but many more
  unpunctuated pauses appear.
- `turbo` + conditioning (dc) as the fallback setting if bhc disappoints on a new video.
- The vocabulary brief can reuse `style_prompt` for client words.
- `edge_flags` and the two segment-edge notes in `utils/snapping.py` are now only read by tests
  and old files.

**Next:** done, see the entry above: Igor re-rated the eight T3 clips in `runs/2026-10-11_t3-2`.

## 2026-10-11 — Brief 8, Part B: moving-crop render (`video.crop: speaker`)

**Before it:** Igor watched all six Brief 8.1 previews on T3 and passed the checkpoint (faces
found in wide shots, the right person most of the time, cuts reset correctly).

**Done** (4 commits plus this one, nothing pushed)
- **Render approach:** ffmpeg `sendcmd` drives the existing `crop` filter in the same single pass
  (`sendcmd=f=clips/<id>.crop.cmd,crop=…,scale=…,setsar=1,subtitles=…`). Audio, captions, encoder
  settings and the ffprobe check are unchanged. Centre mode is byte-for-byte as before.
  `video.crop` still defaults to `center`.
- **Checked on ffmpeg 9.0.1 before building:** with `-ss` before the input, command times count
  from the clip start, and a command takes effect on the first frame at or after its time (no
  one-frame lag).
- **Frame-exact cuts:** the 10-per-second samples place a cut only to within a few frames, and a
  crop jump at the sample time would have been one frame late on 3 of T3's 5 cuts. For each cut
  the track now also stores the exact time (`cuts_exact`), found from the source's own frames and
  timestamps around it. Track files are schema 2; older ones are recomputed.
- **`utils/crop.py` `frame_path`:** the crop x at every frame: linear between samples inside a
  shot, a hard jump at every shot boundary (a camera cut at its exact time, a speaker switch at
  its own time). `ffmpeg.crop_commands` writes the changes as sendcmd lines.
- **Render stage:** reuses a valid `clips/<id>.track.json`, else analyses the clip; a track with a
  fallback reason renders as the centre crop; both model files are checked first and never
  downloaded. `clips/<id>.json` records `crop_mode`, `track_sha256`, `track_fallback`,
  `crop_changes`. On a resume a speaker clip is kept only while its track is valid and is the
  file it was rendered from.
- Built by a Sonnet 5.5 (Medium) subagent; each diff reviewed by the planner, who re-ran the
  checks. Tests: 1051 passed, 2 skipped. ruff, format, mypy and pip-audit clean. A real-ffmpeg
  pixel test renders a two-colour source and checks the colour on the frames either side of each
  crop change (with a non-zero seek).

**Real run** (T3, `video.crop: speaker`, `captions.offset_seconds: 0.15`, `--from-stage score`;
the files from before are in `runs/_scratch/`)
- **Score** (Brief 9's boundary repair on the frozen candidates): 1 call, 13,840 input and 2,316
  output tokens, **$0.0508**. 7 kept of 18 (was 6): 2 trims and 1 extension applied, none
  ignored, no overlaps.
  - Kept before and after: c05, c13, c15 (same times) and c09 (now 7.3 s longer at the start).
  - Newly kept: c07 (trimmed), c11 (start extended by one sentence, end trimmed), c12.
  - No longer kept: c06 (`too_long_or_rambling`) and c17 (`needs_context`).
- **Render:**

| Clip | Length | Cuts (exact time) | Switches | Crop changes | Mouth read | Analysis | Analysis + render | Size |
|---|---|---|---|---|---|---|---|---|
| c05 | 43.1 s | 16.4125 s | 0 | 20 | 72% | 24.4 s | 38.9 s | 13.7 MB |
| c07 | 29.9 s | 2.9945 s | 0 | 131 | 91% | 14.0 s | 26.4 s | 14.4 MB |
| c09 | 36.6 s | none | 0 | 12 | 100% | 16.9 s | 30.1 s | 14.1 MB |
| c15 | 32.6 s | none | 0 | 104 | 50% | 17.6 s | 28.7 s | 11.2 MB |
| c11 | 23.8 s | none | 0 | 15 | 100% | 11.1 s | 19.7 s | 8.6 MB |
| c12 | 35.6 s | none | 0 | 31 | 100% | 16.7 s | 29.2 s | 12.6 MB |
| c13 | 42.0 s | 34.852 s | 0 | 39 | 53% | 24.5 s | 41.3 s | 15.3 MB |

  - All seven: speaker mode, no fallback, face time 100%, 1080x1920 H.264 + AAC. Every track was
    analysed here (the older track files were schema 1).
  - **Cut frames checked in the rendered files:** at each of the three cuts, the three frames
    before it match the source cropped at the old x and the three from it on match the new x
    (mean grey difference about 1 against 27–45 for the other position).

**Gaps and risks**
- **No speaker switch was rendered.** The only clip with switches in the 8.1 run (c17) was
  rejected by the re-score, so a switch inside a camera shot is covered by unit tests and the
  pixel test only, not by real footage yet.
- Exact cut times rely on the source's frame timestamps; if they cannot be read the sample time
  is used (up to 3 frames late at 30 fps).
- The crop path is worked out at the source's reported frame rate, capped at 60 per second.

**Parked for a possible Brief 8.2** (seen by Igor on the 8.1 previews; not fixed here)
- The switch to a new speaker lags by a couple of seconds.
- A laugh or chuckle can steal the camera.
- A speaker turned away from the camera gives no mouth signal (see the 8.1 entry).

**Next:** Igor watches the seven clips and rates C6.

## 2026-10-11 — Brief 8.1: small faces in wide shots, camera reset at shot cuts (stopped at the checkpoint)

**Why:** Part A's checkpoint on the T3 run failed in wide shots. The Face Landmarker found no
faces there (c15: 0% face time) and "no face, keep last position" carried a close-up's crop x
into the next wide shot.

**Measured before choosing** (T3, 10 samples per second; frames stayed in memory, numbers only)

| Option | c15 (wide): 1+ faces / 2 faces | c17 (mixed): 1+ / 2 faces | Detect time per sample |
|---|---|---|---|
| Before: landmarker on the whole frame, 640 wide | 0% / 0% | 15.5% / 0% | 4–6 ms |
| Same at 1920 wide | 0% / 0% | 20.6% / 0% | 5–7 ms |
| (a) Tiling: 8 tiles of 648 px plus the whole frame | 96.0% / 7.7% | 100% / 52.8% | 53 ms |
| (b) YuNet at 960 wide, score 0.7 | 100% / 99.4% | 100% / 99.7% | 22 ms + about 10 ms per face |
| (c) MediaPipe BlazeFace full-range, score 0.5 | 97.5% / 23.0% | 100% / 52.0% | 6.4 ms |

- Chosen: **(b)**, D-016. Close-up clip c09: face 100%, mouth read 100%.
- Wide-shot faces measure 12–16% of the frame height (the brief guessed 8–10%).
- Cut detection, colour-histogram change between samples: 0.37–0.42 at the five cuts, at most
  0.031 anywhere else. Threshold 0.12.

**Done** (6 commits plus this one, nothing pushed)
- `opencv-contrib-python` declared as a direct dependency (already installed and locked at
  5.0.0.93 through mediapipe; nothing new downloaded).
- **`utils/faces.py`:** YuNet finds face boxes on the frame at `detect_width`; the Face Landmarker
  (IMAGE mode) reads the mouth on a crop of the full frame around each face. Faces below
  `min_face_height` are ignored. A face whose mouth cannot be read is still tracked
  (`mouth: None`). Two pinned model files; `fetch-face-model` fetches whichever is missing.
- **`utils/camera.py`:** `find_cuts` (a one-sample flash and a slow drift are not cuts) and
  `follow_segments`: every camera shot is followed on its own, so tracks, hysteresis and the crop
  position never cross a cut. Fresh choice: the speaker, else the largest face (ties: the most
  central), else centre.
- **`tracking.py`:** decodes once at `landmark_width` (never above the source), records cuts in
  `track.json`, new summary numbers (cuts, faces per sample, mouth read %), a yellow border in
  the preview after each cut. A track made with another detector file or OpenCV version is stale.
- **Config:** `speaker.detect_width` 960, `landmark_width` 1920, `detect_score` 0.7,
  `face_crop_scale` 2.0, `cut_threshold` 0.12. `analysis_width` is still accepted but unused.
- Built by a Sonnet 5.5 (Medium) subagent; each diff reviewed by the planner, who re-ran the
  checks. **Review caught one bug:** after a cut the speaker was chosen from clip-relative times
  instead of times counted from the cut; fixed with a regression test.
- Tests: 980 passed, 2 skipped. ruff, format, mypy and pip-audit clean. The opt-in real
  detector-plus-landmarker test passes with both model files present.
- YuNet model fetched with Igor's approval: 229,738 bytes, SHA-256 matches the pin.

**Real run** (`track runs/2026-10-11_t3 --preview`, local, $0)

| Clip | Face time (was) | Faces per sample | Cuts (s) | Switches | Mouth read | Analysis |
|---|---|---|---|---|---|---|
| c05 | 100% (14–45%) | 1.25 | 16.4 | 0 | 72% | 23.6 s |
| c06 | 100% (14–45%) | 1.57 | 8.3 | 0 | 58% | 12.8 s |
| c09 | 100% (96%) | 1.00 | none | 0 | 100% | 14.2 s |
| c13 | 100% (14–45%) | 1.83 | 34.9 | 0 | 53% | 23.3 s |
| c15 | 100% (0%) | 1.99 | none | 0 | 50% | 19.2 s |
| c17 | 100% (14–45%) | 1.63 | 9.5, 23.5 | 3 | 94% | 19.8 s |

- Targets met: c15 face time at least 80%; c09 at least 90%; cuts found in c05, c06, c13 and c17
  and none in c09 and c15.
- At every cut the crop x jumps on the first sample of the new shot (for example 1002 to 674 in
  c05, 270 to 974 in c17).
- c15 follows the person at 0.68 of the frame width for the whole clip: the one whose mouth is
  readable (98% of samples; the other person 3%).

**Gaps and risks**
- **A person turned away from the camera gives no mouth signal.** The landmarker reads only
  50–72% of faces in c05, c06, c13 and c15, even on a tight crop and at lower confidence settings.
  Such a person is framed correctly but can never win the speaker choice; if they speak, the
  camera shows the listener. Igor accepted this for now.
- In wide shots the mouth signal is weaker: where both mouths are readable, the leader beats the
  other by a median factor of 3.9–5.8, and by 1.5 or more in 31 of 39 speech windows.
- The cut threshold comes from one video (five cuts). A cut between two similar-looking angles
  could score lower.
- The YuNet download URL is pinned to a commit of a GitHub repository; the pinned SHA-256 is what
  protects against a changed file.

**Parked**
- Framing width: 9:16 crop cuts gesturing arms; Igor: not too bad; judge on finished clips
  (zoom-out + blurred fill vs tighter face framing).
- Possible Brief 8.2: pixel movement around the mouth as a speaker signal that needs no
  landmarks. Measured once: it ranked the camera-facing speaker first in 53 of 62 speech windows
  in c15, and one person led in 68 of 70 in c13's wide shot.
- Tiny background faces (3.6% of the frame height, mouth variance near zero, present in every
  sample of one shot in c06 and c17) are dropped by `min_face_height`; a still picture of a larger
  face would not be.

**Next:** Igor re-watches the previews, c15 and c17 first. Checkpoint pass: the right person at
least 90% of the time, no crop left on empty space after a cut, no flicker. Part B waits for it.

## 2026-10-11 — Brief 9: Boundary repair (built; real run done on T1)

**Done**
- **Score stage can repair boundaries:** `keep_sentences` may now start earlier or end later,
  not only trim. Each candidate shows up to `score.extend_before` (4) sentences before and
  `score.extend_after` (3) after, numbered continuously: before = ... -1, 0; clip = 1..n;
  after = n+1.... Times still come from word timestamps plus the snapping padding.
- **Rules:** the range must lie inside the shown numbers and include at least one of the clip's
  own sentences; the repaired clip must stay within `clip.min_seconds`..`max_seconds`. Otherwise
  the adjustment is ignored and noted, and a clip the model kept is dropped as `adjust_ignored`
  (`trim_ignored` stays readable in old files).
- **Overlap rule (added by Igor):** after `min_score` and `adjust_ignored`, before the `keep_top`
  cut, clips are walked best score first (ties to the earlier candidate). A clip whose final
  times overlap an already accepted clip by more than 50% of the shorter one is dropped as
  `overlap`; exactly 50% is kept. A dropped overlap takes no rank.
- **scored.json:** per clip `extended_start`, `extended_end`; summary `extensions_applied`,
  `adjust_ignored`, `dropped_overlap`. Old files load.
- **Prompts:** `score_clip.md` "Repair first, reject second" with the numbered context;
  `find_moments.md` gets the one setup/finished-point line.
- **Report:** "extended" badge; "dropped adjust_ignored" (counts old `trim_ignored` too) and
  "dropped overlap" in the summary.
- **Captions:** `captions.offset_seconds` default 0.0 -> 0.15 (config and model).
- **Tests:** 888 passed, 2 skipped; ruff, format, mypy and pip-audit clean.
  `tests/test_render.py` pins the offset to 0.0 in its helper so those tests keep checking
  caption timing at the spoken time.

**Real run** (T1, `--from-stage score`, candidates frozen, before-file in
`runs/_scratch/t1-scored-before.json`)
- **Kept:** 2 -> 8 of 19 (8 is `keep_top`). Rejected 17 -> 10. `needs_context` rejects 14 -> 6.
- **Counts:** `extensions_applied` 9, `adjust_ignored` 0, `dropped_overlap` 0, `dropped_top_n` 1,
  `dropped_min_score` 0, one trim applied. No warnings.
- **Cost:** $0.0505 (one Sonnet call). `min_score` untouched.
- **Igor's near-misses:** c03 (end +2), c06 (start -2) and c08 (start -1) became keeps at
  score 8. c17 became a score-7 keep but was cut by `keep_top` alone. **c19 was not repaired**
  (still 6, rejected `needs_context`, no extension).
- **Overlap:** kept c08 and c09 overlap by 12.1 s (39% of the shorter), under the 50% rule, so
  both are kept and rendered.
- **Score drift:** six clips with no adjustment (c04, c05, c10, c11, c14, c16) each gained 1
  point. That is either run-to-run variance or an effect of the new prompt wording.
- **New keeps nobody has heard:** c02, c07, c13.

**Igor's listening deferred:** boundary quality will be judged on the T3 re-score (1080p) after
Brief 8 Part B.

**Parked**
- c19 not repaired: the judge did not extend its end.
- Overlap threshold: consider about 30% if overlapping kept clips feel repetitive (c08/c09 are
  at 39%).
- Possible +1 score drift from the new prompt: watch precision on T3.

## 2026-10-11 — Brief 8, Part A: speaker-following crop analysis (stopped at the checkpoint)

**Done** (7 commits, nothing pushed)
- **Dependencies:** `mediapipe` 1.1.0 and `numpy` added with `uv add` (the lock pins 1.1.0).
  Transitive packages accepted: opencv-contrib-python 5.0.0.93, matplotlib 3.11.2, sounddevice,
  absl-py. `pip-audit`: no known vulnerabilities after the install and again at the end.
- **Docs:** D-014 (MediaPipe Face Landmarker + lip-movement speaker choice + cut-based camera;
  telemetry accepted by Igor, no hosts-file change). COSTS.md rows for mediapipe, its transitive
  packages and the model file, plus the telemetry finding and a privacy line.
- **Config:** `video.crop` is `center | speaker` (`face` was never implemented). New `speaker:`
  section, defaults for every key, so old run folders load. The render stage still refuses
  `speaker` until Part B.
- **`utils/camera.py`** (pure Python): track linking, speech windows, speaker choice with
  hysteresis, shots with a minimum length, camera path (smoothing, dead zone, clamp, hold),
  fallback reasons, summary.
- **`utils/ffmpeg.py`:** decode only the clip's range to RGB frames over a pipe; preview encoder.
- **`utils/faces.py`:** the only module that imports mediapipe (lazily); model-file check with a
  SHA-256 slot; explicit `fetch-face-model` command. The `track` command never downloads.
- **`tracking.py` and `clipmachine track <run-folder> [--preview]`:** writes
  `clips/<id>.track.json` (numbers only) and an optional `clips/<id>.track.preview.mp4` with face
  boxes, the active speaker and the 9:16 window. It does not touch `run.json`.
- Built by a Sonnet 5.5 (Medium) subagent; reviewed by the planner, who re-ran the checks.
  Tests: 865 passed, 2 skipped (was 729). ruff, mypy, pip-audit clean.

**Not yet done**
- **The model file is not downloaded and no real face has been analysed.** Speed numbers and the
  `min_face_height` default are still estimates. The real-landmarker test is opt-in
  (`CLIPMACHINE_MEDIAPIPE_TESTS=1`) and skipped.
- Whether mediapipe 1.1.0 really sends telemetry from a Python Tasks call, and what exactly, is
  unverified: reported by others, not measured here.

**Gaps and risks**
- **Model licence unconfirmed** (model card PDFs not readable by the fetch tool). Must be settled
  before any client work (COSTS.md).
- Face Mesh lip and chin indices (13/14, 10/152) and the frame-height rounding of `scale=W:-2`
  are checked only against synthetic data and one small real ffmpeg case.
- If the short-range detector misses small faces in a wide studio shot, the contained fix is
  tiling inside `utils/faces.py`; it would double analysis time.

**Parked**
- Part B (moving-crop render via `sendcmd`; times relative to the clip start because render seeks
  with `-ss` before `-i`; the pixel test must check colour at a known time after the cut).
- Linking uses position only; two faces that cross faster than they are apart can swap identity.

**Next:** Igor approves the model download and supplies a two-person studio test video, then
`track --preview` on its kept clips and the checkpoint review.

## 2026-10-11 — Brief 7: Report stage (report.html)

**Done**
- Stage 6 writes one offline `report.html` per run, laid out as in `docs/PRODUCT.md` section 5:
  summary bar (kept of candidates, cost from `costs.json`, processing time from `run.json`,
  reject-reason and dropped counts), one card per kept clip in rank order (relative-path `<video>`,
  title, hook, reason, collapsed transcript, C1–C9 reminder), and a collapsed rejected list.
- States: zero kept shows "No clips passed the judge." with the rejected list open; a kept clip
  whose `.mp4` or `.json` is missing, invalid or does not match `scored.json` shows
  "Render missing: <reason>" with no player and the other cards are normal.
- Standard library only: one `_esc()` helper (`html.escape`, quote=True) for every text value;
  it also breaks up `://`, so the page can never hold a URL even if model text contains one.
  Inline CSS, no JavaScript. Written atomically.
- CLI: `run` prints the report path when it exists; `report <run-folder>` prints the path, or says
  which command builds it (it used to print "not implemented yet").
- Built by a Sonnet 5.5 (Medium) subagent from the approved plan; reviewed and re-checked by the
  planner. Tests: 729 passed, 1 skipped (was 709). ruff, mypy, pip-audit clean.

**Real run** (`runs/2026-10-10_source --from-stage report`, local, no API cost)
- `report.html` is 4,549 bytes: 1 kept clip of 7 candidates, 6 rejected entries, 0 render missing,
  one `<video src="clips/c04.mp4">`, no `http(s)://` in the file.
- Ledger unchanged (8 lines); a rerun says "nothing to do".

**Decisions**
- Wording is "Render missing" (Igor's change); `docs/PRODUCT.md` states table updated to match.
- The page holds transcript text and lives only in the git-ignored run folder.

**Parked**
- `run` prints the report path whenever `report.html` exists, even for a stale file; harmless
  while report is the last stage.
- Igor's own review of c04, the text and the rejected list in the browser is still to be logged.

## 2026-10-11 — Brief 6.1: Caption timing offset

**Evidence:** in the Brief 6 real run Igor watched c04 and the yellow highlight landed on a word
before it was spoken (rubric C5, sync, failed). Whisper word start times probably run early.

**Done**
- `captions.offset_seconds` (float, default 0.0, range -1.0 to 1.0; positive = captions appear
  later). Applied to every finished caption event, rounded to ms and clamped to [0, clip length];
  events left with no length are dropped. Offset 0 gives the same events as before.
- It is a render setting: recorded in `clips/<id>.json` (`caption_offset_seconds`) and part of the
  "valid output" check, so a changed offset re-renders a clip on resume. Brief 6 JSON files without
  the field load as 0.0.
- Tests: 709 passed, 1 skipped (was 693). ruff, mypy, pip-audit clean.

**Tuning (one clip, by eye)**
- Value chosen: **0.15 s**, set only in that run's `config.resolved.yaml`
  (`runs/2026-10-10_source`). Igor judged it by watching c04; the highlight is now in sync.
- Render 7.1 s, 82 caption events (none dropped), ledger unchanged (8 lines).
- `config/default.yaml` stays at 0.0: one clip from one video is not enough to change a default.

**Parked**
- Confirm 0.15 on a second video before it becomes the default; if it varies a lot between videos,
  the D-013 fallback (WhisperX alignment) is the next step, not more offset logic.

## 2026-10-10 — Brief 6: Render (cut, 9:16 crop, burned-in captions)

**Done**
- Stage 5 renders every `kept` clip of `scored.json` to `clips/<id>.mp4` (1080×1920, H.264 +
  AAC), `<id>.ass` and `<id>.json` (times, crop box, source size/rotation/CFR-VFR, settings,
  ffprobe summary; no text). One ffmpeg pass per clip, always re-encoded.
- Crop math is a pure function (`utils/crop.py`): largest centred 9:16 box, even integers, rotation
  90/270 swaps width and height. Captions are pure functions (`utils/captions.py`): word groups of 3
  (or to `. ? !`), one event per word with the current word highlighted, gaps under 0.5 s filled,
  `{ } \` removed from words.
- Windows paths: ffmpeg runs with the run folder as working directory; the subtitle file and font
  folder are relative forward-slash paths and are checked before use (no drive letter or backslash
  can reach `-vf`).
- Font: static Montserrat Bold plus `OFL.txt` from the upstream release tag v9.000 in
  `assets/fonts/`; a missing font fails the stage, there is no fallback.
- Restart: `--from-stage render` is a forced redo of every kept clip; only a render stage that
  failed or was interrupted earlier is resumed, rendering just the clips without a valid output
  (JSON matches times, crop and settings, and ffprobe agrees). The pipeline tells the stage which
  case it is (`RunContext.resuming`).
- D-013 recorded (keep faster-whisper word timestamps).
- Tests: 693 passed, 1 skipped (was 598); `-m ffmpeg`: 9 passed, 1 skipped. ruff, mypy, pip-audit
  clean.

**Real run (same 15.4-min test interview, `general` client, 1 kept clip)**
- Source 640×360, rotation 0, 30 fps CFR; crop 202×360 at x=218; output 1080×1920, 26.8 s, 4.7 MB,
  render 7.6 s. Ledger unchanged (8 lines); rerun printed "nothing to do".
- libass log (verbose) shows it loaded `assets/fonts/Montserrat-Bold.ttf` and matched
  `(Montserrat, 700) -> Montserrat-Bold`; no system copy of Montserrat is installed.

**Observed**
- The 640×360 source is upscaled about 5.3× to 1080×1920, so the picture is soft.
- A centre crop of a two-speaker interview can cut a speaker off; a frame near the start of c04
  has the speaker at the right edge of the crop. Rubric C6 is Igor's to judge by watching.

**Parked**
- Face-aware crop (Phase 2) is the fix if C6 fails for this kind of footage.
- `video:` and `render:` both hold output settings; `video.width/height` is what render reads.
- Caption wrapping of very long words at size 80 is not tuned.

## 2026-10-10 — Brief 5.1: Score fixes (context, trim-first, more candidates)

**Evidence (Igor's re-listen after Brief 5):** the Brief 5 c08 start at 12:00 was clean, so Sonnet's
reject of it was a **false reject**.

**Root causes (judgment, from the brief)**
1. Sonnet judged "standalone" and "payoff complete" without seeing the text just before and after
   the clip, and a segment-edge start has no punctuation to go by.
2. The prompt let it reject rather than repair; `keep_sentences` was never used.
3. Too few candidates went in for a strict judge.

**Done**
- Context: up to 2 sentences `<before>` and 1 `<after>` per candidate (unnumbered, clipped at the
  transcript ends, never overlapping the clip), plus `start_at_segment_edge` / `end_at_segment_edge`
  from the snap notes. Prompt: trim first, reject second; score and verdict describe the trimmed clip.
- `candidates.per_block_max` 5 -> 8 (`candidates_target` stays 20). The existing run folder's
  `config.resolved.yaml` was edited for that one value so the real run used it.
- `scored.json`: edge flags, `sentences`, `kept_sentences`, and trim counts (`trims_applied`,
  `trims_ignored`, `trims_out_of_range`, `dropped_trim_ignored`); all with defaults, so Brief 5 files load.
- A clip the model kept whose trim was ignored (outside `clip_length`, or sentence numbers out of
  range) is not kept: `dropped_by: "trim_ignored"`. Precedence: `min_score` first.
- Commits: 4 feature commits. Tests: 598 passed, 1 skipped (was 582). ruff, mypy, pip-audit clean.

**Before vs after (same 15.4-min interview, `general` client)**

| | Brief 5 | Brief 5.1 |
|---|---|---|
| Candidates requested / returned / in | 10 / 8 / 8 | 16 / 7 / 7 |
| Kept | 1 | 1 |
| Rejected by the model | 7 | 6 |
| Trims used (applied / ignored) | 0 | 1 / 0 |
| Reject reasons | needs_context 4, weak_hook 1, off_brand 1, below_threshold 1 | needs_context 3, no_payoff 2, off_brand 1 |

**Cost:** candidates 2 calls, 9,020 in / 651 out, $0.0012 (worst-case estimate $0.0030); score 1
call, 4,770 in / 872 out / 0 thinking, $0.0183 (estimate $0.0905). Total $0.0195. Rerun: "nothing to
do", ledger unchanged (8 lines).

**Outcome: stop rule reached (1 kept, 2 or fewer).** No tuning was done: prompt, `min_score` and
`keep_top` are as in Brief 5.

**Observed**
- The quota was not the limit: Haiku returned 7 of the 16 moments asked for (0 invalid, 0 dropped,
  0 duplicates), so raising `per_block_max` added nothing here.
- The candidate set changed between runs: of the 7, only one span is identical to a Brief 5
  candidate (684.19-720.39 s); candidate ids are reassigned each run. No candidate now starts
  near 12:00, so Igor's clean start was not re-judged.
- First real use of `keep_sentences`: 1 trim applied (the kept clip, sentences 2-8 of 8). It scored
  exactly `min_score` (6).
- The same 684.19-720.39 s span was rejected again as `needs_context` although its start flag is
  false; the end flag is true. Whisper punctuation at that boundary may be wrong.

**Parked**
- Candidate pool size and run-to-run variation belong to the candidates stage (out of scope here).
- A way to extend a clip when the payoff falls just after its end (the model signals this; today it
  can only trim).

## 2026-10-10 — Brief 5: Score stage

**Facts**
- Commits: 6 feature/test commits plus this log entry. Tests: 582 passed, 1 skipped. ruff, mypy and
  pip-audit clean. No new dependency.
- Sonnet 5.5 rejects `thinking: disabled` with a 400; its lowest setting is `between_tools`
  (accepted at low, medium and high effort). Config: `score.thinking: between_tools`,
  `score.effort: medium`. Default effort for the model is `high`, so it is set explicitly.
- `call_structured` gained an optional `check` hook and a `repairs` count (0 or 1), so the id check
  uses the client's one repair call and the guard, ledger and cost log as before.
- Real run (15.4-min interview, `general` client, 8 candidates): 1 call, 1 chunk, no repair.
  4,298 input and 1,043 output tokens, 0 thinking tokens. Actual $0.019026 against a worst-case
  estimate of $0.089456 (the 8,000-token output ceiling). 8.9 s. About $0.074 per hour of video
  for stage 4. Rerun: "nothing to do", ledger unchanged (5 lines).
- Result: 1 kept, 7 rejected, 0 dropped by `min_score`, 0 dropped by `top_n`, 0 trimmed.
  Model reject reasons: needs_context 4, weak_hook 1, off_brand 1, below_threshold 1,
  no_payoff 0, too_long_or_rambling 0.
- Model vs Igor (c01, c03, c07, c08, from the Brief 4.1 spot check): **agreement 2 of 4**.
  - c03 and c07: both sides see a start mid-sentence (C1 fail). Agree on the defect; the model is
    harsher on "understandable".
  - c01: Igor rated it OK; the model rejected it under the client's "avoid intros" rule. Disagree.
  - c08: Igor rated it clean; the model rejected it as starting mid-sentence. Disagree, **pending
    Igor's re-listen**: its snap notes say the start is a segment edge with no sentence
    punctuation before it, and it starts exactly where c07 ends, which fits the model's claim.

**Observed**
- The model never used `keep_sentences`, so the trim path has only been exercised by tests, not by
  a real answer.
- 1 of 8 kept is strict but matches the brief ("it is normal to keep only a few"); neither
  `min_score` nor `keep_top` was the limit.
- The model's `below_threshold` reason is recorded as its own word; our own cuts are `dropped_by`
  (`min_score` / `top_n`) and are never written into `reject_reason`.

**Parked**
- Tell Sonnet which candidates start or end at a fallback segment edge (from `snap_notes`); may
  help with the c03/c07/c08 kind of C1 failure.
- Store word indices in `candidates.json` so the score stage need not locate candidates by time.

## 2026-10-10 — Brief 4.1: Candidate quality fixes

**Evidence (Igor's spot check of 4 of 8 candidates):** 2 of 4 were understandable alone. One had its payoff cut short; two started mid-sentence.

**Root causes**
- Snapping treated every Whisper segment edge as a sentence start. Whisper breaks segments mid-sentence: 4 of 8 starts and 3 of 8 ends were segment edges.
- All 8 moments were 20-27 s, right at the minimum. The model cut answers short.

**Done**
- A sentence ends only after `. ? !`. A segment edge is a boundary only after more than 40 unpunctuated words (`candidates.fallback_max_words`).
- Prompt asks for a new thought at the start and a complete payoff, preferring 30-50 s.
- New config `preferred_min_seconds` / `preferred_max_seconds`, validated against the clip range and clamped into a client's `clip_length`.
- Snapping grows short moments toward the end only, never past the maximum. Under the hard minimum it may still grow backward as a last resort; that is noted in `snap_notes` and counted in `summary.grew_backward`.

**Before vs after (same transcript, 8 candidates each)**
- Segment-edge starts: 4 -> 1. Segment-edge ends: 3 -> 2.
- Length min / median / max: 20.3 / 22.1 / 27.3 s -> 32.3 / 37.2 / 47.6 s.
- Grew backward: 0 of 8.
- No moments were invalid, dropped or duplicates.

**Cost:** $0.0013 for the re-run (2 calls); the earlier Brief 4 run cost $0.0013 too.

**Igor's spot check of the new candidates (rubric terms)**
- **c08:** clean. C1, C2 and C4 pass.
- **c01:** OK, but starts slightly late (a padding issue, parked for Brief 6). It is an intro moment.
- **c03 and c07:** start mid-sentence (C1 fail) but are understandable.
- Only 4 of the 8 new candidates were checked (c01, c03, c07, c08); c03 and c07 are open C1 failures to look at in Brief 5/6.

**Caught along the way**
- Claude Code copied `candidates.json` (private transcript text) to `%TEMP%` as a backup, outside the repo. It disclosed this; Igor deleted the file. Fix: CLAUDE.md now says scratch and backup files go only in `runs/_scratch/` (git-ignored).
- Claude Code used bare `python` for some file edits. Fix: CLAUDE.md now says always run Python through `uv run`.
- A path typed with backslashes (`runs\...`) was not recognised as a run folder in Git Bash. Use forward slashes.

**Parked:** start padding (Brief 6, when real clips can be heard); abbreviation handling.

## 2026-10-10 — Brief 4: Claude client and candidates stage

**Facts**
- Commits: 9 (two docs commits first). Tests: 485 passed, 1 skipped (was 310). ruff, mypy and
  pip-audit clean.
- New runtime dependency: anthropic 1.13.0 (MIT); new packages docstring-parser, jiter, sniffio.
- Real run (15.4-min interview, `general` client, Haiku 5.5, thinking disabled, effort low): 2 calls,
  8,810 input and 830 output tokens, 0 thinking tokens, actual $0.001296 against a worst-case
  estimate of $0.0029692. About 3-4 s per call. 8 candidates returned, 0 invalid, 0 dropped, 0
  duplicates. Rerun: "nothing to do", ledger unchanged (2 lines).
- Cost per hour of video for stage 3: about $0.005 (2 calls for 923 s).
- Kill switch and budget refusal both checked on a real run folder with no network use.
- `count_tokens` accepted `output_config` and `thinking`; the estimate source was `count_tokens`.

**Observed**
- Whisper words are contiguous (each word ends where the next starts), so the 0.2 s padding is
  limited to 0.00 s at almost every boundary. The padding setting has no effect on this transcript.
- 4 of 8 candidate starts and 3 of 8 ends sit at a segment edge with no sentence punctuation. These
  are the boundaries most likely to fail rubric C1; Igor's spot check decides.
- With the brief's numbers a 15-minute video gives at most 10 candidates, not about 20.

**Parked**
- Padding into the silence between words needs a different rule (midpoint of the gap, or a fixed
  lead-in taken from the neighbouring word's start).
- Per-block response caching, so a stage that fails midway does not repeat paid calls.
- Abbreviations ("Dr.") count as sentence ends; a short last block is not merged.
- The SDK still reads `ANTHROPIC_CUSTOM_HEADERS` from the environment; `ANTHROPIC_BASE_URL` and the
  key are passed explicitly.
- The ledger is not locked: two runs at the same moment could both pass the daily check.
- CLAUDE.md's opening line still says only `fetch` is real.

## 2026-10-10 — Session 2 (17:47–20:00 ET): Briefs 2 and 3

**Done**
- **Brief 2 (fetch)** and **Brief 3 (transcribe)** shipped. CI is green.
- The default transcription setting is GPU `small` (D-011). A 15-minute interview transcribes in about 50 s.
- **Laptop check:**
  - Hardware: HP Omen, Ryzen 7 4800H, 16 GB RAM, GTX 1660 Ti 6 GB.
  - Free RAM went from 1.9 GB to 8.3 GB after a restart and turning off unneeded startup apps. Disk is fine (300 GB free).

**Decisions**
- D-011: default transcription setting.
- D-012: test inputs. The fetch and transcribe checks used third-party clips, privately and locally only. They are deleted after testing and are not used for evaluation material, demos or samples. The sample run for the finish line needs a video Igor owns or a CC-licensed one.

**Caught in reviews**
- Extraction now picks a fixed audio track (`-map 0:a:0`).
- The audio-duration tolerance scales with length.
- One run folder per benchmark setting.
- `nvidia-smi` added to the allow-list, so routine GPU checks don't need a click each time.
- Hugging Face cache and telemetry are kept inside the repo.

**What I did vs what AI did**
- **Igor:** decisions D-011 and D-012, laptop cleanup, test inputs, pushes and checks.
- **Claude (chat):** Briefs 2 and 3, plan and report reviews, the laptop check, the cost register update.
- **Claude Code:** Opus 5.5 wrote the plans and Sonnet 5.5 wrote the code and tests.

**Cost:** $0. No API calls yet.

**Parked**
- A rejected input still creates a run folder.
- Probe the copied file rather than the original.
- The Purfview GPU library route is untested.
- Segment timestamps in batched mode.
- Optional RAM upgrade (a purchase; Igor's call).

## 2026-10-10 — Brief 3: Transcribe stage (benchmark done, default not chosen yet)

**Facts**
- New runtime dependency: faster-whisper 1.2.1 (MIT) with ctranslate2 4.8.2, onnxruntime, av,
  tokenizers, huggingface-hub (plus hf-xet, numpy, tqdm and a few small helpers). New optional
  group `gpu`: nvidia-cublas-cu12 12.9.2.10, nvidia-cudnn-cu12 9.27.0.42 and (transitively)
  nvidia-cuda-nvrtc-cu12 12.9.86. The newest cuDNN already has a Windows wheel, so no upper bound.
- GPU route that worked first time: the pip wheels in the `gpu` group, with
  `os.add_dll_directory`, a ctypes preload and a process-local PATH prefix. No Purfview archive.
- Benchmark on a 923 s two-speaker interview, GTX 1660 Ti: see the report; no default chosen yet.

**Parked**
- Batched mode (`batch_size > 0`) returns about 26 s segments because the library turns timestamps
  off per chunk; with timestamps on it gave 236 segments but ran about 40 % slower. A config option
  for this is not built.
- `turbo` lost punctuation and capitalisation in some stretches; `small` did not.
- Per-process VRAM shows N/A under Windows WDDM; GPU use is shown by the PID row, the total memory
  rise and utilisation instead.
- Word strings keep faster-whisper's leading space (" Hey"); caption code must strip it.
- Hugging Face prints an "unauthenticated requests" warning on first download; harmless.

## 2026-10-10 — Brief 2: Fetch stage

**Facts**
- Commits: 9 (8 feature/docs/test commits + this log entry), `24a0978` to this one.
- Tests: 232 passed, 0 skipped (was 104). 8 of them are `ffmpeg`-marked integration tests.
- ruff, mypy and pip-audit clean. No new dependencies.
- Real video (`3Dprinting_time laps.mp4`, 5,219,973 bytes, 66.3 s, 640x360 h264/aac): first fetch
  `fetch_seconds` 0.79, `--from-stage fetch` 0.253. Audio is 16000 Hz mono `pcm_s16le`. SHA-256 of
  the copy matches the original; original size and mtime unchanged.
- Rejections checked with real files: text renamed `.mp4` (exit 1, run folder keeps only
  `run.json` and `config.resolved.yaml`), `.mp3` (exit 2, no run folder), missing path (exit 2).
- `-protocol_whitelist file` and the `file:` prefix work on Windows paths with spaces. On ffmpeg
  9.0.1 no hostile playlist tried (HLS, DASH, concat) connected even without the flag, so the flag's
  own effect is unproven here.

**Parked**
- Rejected input still creates a run folder and the CLI suggests "rerun" for it; consider
  rejecting before the folder exists, or a clearer message.
- A copy-then-probe order (probe the run-folder copy, not the original) would close the small
  window where the original changes between probe and copy.

## 2026-10-10 — Session 1: Gate 0 + Brief 1 (02:11–04:00 ET)

**Done**
- **Gate 0:** plan review (top 5 risks), `COSTS.md` from live pricing pages, `MVP.md` (finish line + clip rubric), decisions D-001 to D-010.
  - Key finding: Max 5x includes $100/month of API credits that stop (not overcharge) when empty, so the cash budget is $0.
- **Setup (native Windows 11):**
  - Tools: Git 2.55.0, uv 0.11.21, Python 3.11.15, FFmpeg 9.0.1 essentials, Claude Code 2.1.296, VS Code + Claude Code extension.
  - Git commits use the GitHub noreply email.
- **Brief 1, walking skeleton:** 12 commits.
  - Checks: 104 tests pass; ruff, mypy and pip-audit are clean.
  - End-to-end checks pass: new run, rerun with "nothing to do", `--from-stage render`, and `--client ../evil` rejected.

**Caught along the way**
- Path escape: a run-folder slug like `../../evil` could resolve outside `runs/`. Fixed with a single-path-component check and a test.
- 3.12-only generic syntax: the ruff hook flagged it on a real edit. Fixed.
- Stray folder: a failed `cd` led Claude Code to create a folder outside the repo (`dev\utils`). It disclosed this and removed it, and now uses absolute paths. Brief 2 adds a "never write outside the repo" rule.
- Approval fatigue: about 100 routine prompts. Replaced with an allow-list (D-010).

**Cost:** $0. No Claude API calls yet; Claude Code ran on the Max plan.

**What I did vs what AI did**
- **Igor:** every decision (D-001 to D-010), installs and accounts, checkpoint reviews (guardrail test, commit-email check), and pushed back on approval fatigue.
- **Claude (chat):** research with sources, the cost register, the MVP and rubric, Brief 1, plan and report reviews, and this log.
- **Claude Code:** Opus 5.5 wrote the plan; Sonnet 5.5 wrote the code and tests.

**Parked**
- GitHub Actions are pinned by tag, not commit SHA (revisit before taking clients).
- Test video choice (D-006).
- Phase 0 niche and target list.

**Next:** push to GitHub (first CI run), then Brief 2 (Fetch: local file, ffprobe validation, audio extraction).
