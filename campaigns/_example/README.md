# Example campaign

This folder shows the layout of a campaign folder. Everything in it is invented. Copy it to
`campaigns/<slug>/` and replace the content. The slug is lowercase letters, digits and dashes
(a leading underscore is allowed).

## New campaign in 5 steps (Brief 12.2)

1. Save the campaign's brief as a `.txt`, `.md` or `.pdf` file (text only; a scanned PDF will
   not do). Save the watermark PNG and any supplied moment list as files too.
2. `uv run python -m clipmachine campaign-init <slug> --brief <file> [--watermark <png>] [--moments <list.txt>]`
   drafts `campaigns/<slug>/campaign.yaml` with one model call (cents). The brief is read as
   data, never as instructions. `selection` is `human` unless the brief clearly allows tools.
3. Review every line of the draft (the `# confirm:` comments say what to check first).
4. Download an episode as the campaign instructs.
5. `uv run python -m clipmachine produce <episode> --campaign <slug>`: scout (if allowed),
   render, report and posting plan in one command. Then schedule from
   `runs/_campaigns/posting_plan.csv` (see the combined plan below) and `campaign-posted` each
   clip after it goes live. The daily routine is in `docs/RUNBOOK.md`.

## What a campaign folder holds

- `campaign.yaml`: the rules (hook, watermark, spelling fixes, post caption parts).
- `moments.yaml`: the moments you chose, one entry each.
- `used.json`: written by the tool; it records which moments were rendered and posted. Do not edit it.
- `assignments.json`: written by the tool; which platform each moment belongs to (`platform: auto`).
  A moment never moves once it is in here. Delete the file to assign again.
- `auto_position.json`: written by the tool with `watermark.position: auto`; the chosen spot. Delete it to choose again.
- `auto_spelling.json`: written by the tool when `names:` is set; the name variants found in the transcript.
- `scout.json`: written by `scout` (Brief 12); what the AI proposed and why (score, topic, host check, reason).
- `moments.yaml.bak`: the copy `scout` makes of moments.yaml before it appends to it.
- `assets/`: the watermark PNG named in `campaign.yaml`, if the campaign has one.

Every folder under `campaigns/` except `_example` is git-ignored and must never be committed.

## Commands

Run from the repo root.

```
uv run python -m clipmachine campaign-init <slug> --brief <file> [--watermark <png>] [--moments <list>] [--force]
uv run python -m clipmachine produce <source-video> --campaign <slug> [--rerender]
uv run python -m clipmachine campaign <source-video> --campaign <slug> [--only A03,B05] [--force-reuse]
uv run python -m clipmachine campaign-posted --campaign <slug> --id A03 [--platform tiktok] [--at "2026-10-16 12:30"]
uv run python -m clipmachine campaign-posted --date 2026-10-16 [--campaign <slug>]
uv run python -m clipmachine campaign-posted --campaign <slug> --id A03 --fix-platform instagram
uv run python -m clipmachine plan
```

`campaign-init` drafts a new campaign from its brief (Brief 12.2); it never overwrites a file
without `--force`, which keeps the old one as `.bak`. `produce` is the one command per episode:
it scouts when `selection: ai`, renders this episode's unposted moments (clips whose entry did
not change are kept; `--rerender` redoes them), and rebuilds the report and the plan; posted and
rejected moments are left alone. `campaign` renders the listed moments (every one, every time).
`campaign-posted` marks a moment as posted, so it is not reused by accident; without
`--platform` it uses the moment's assigned platform. The post is recorded at the clip's slot
in the posting plan, so marking a scheduled clip ahead of its day leaves the plan as it was;
`--at` gives another local time, and a clip with no slot is recorded now. `--date` marks every
unposted clip with a slot that day (all campaigns, or only `--campaign`'s), listing them first. `--fix-platform` corrects the platform of a
posted entry that was recorded wrongly. `plan` rebuilds the combined posting plan.

A moment without `source:` belongs to whichever episode the command is given. Once a campaign
has more than one episode, give every entry its `source:`; `produce` refuses to cut a sourceless
moment from a second episode.

### AI-picked moments (Brief 12)

Only for a campaign whose rules allow auto-clipping. Set `selection: ai` and a `scout:` section
in `campaign.yaml` (see the comments there); any other campaign refuses the command.

```
uv run python -m clipmachine scout <episode-video> --campaign <slug> [--dry-run]
uv run python -m clipmachine campaign-reject --campaign <slug> --id S03
```

`scout` transcribes the episode (reused if seen before), runs the normal AI candidates and scoring
with the campaign's rules and clip length, and appends the best moments to `moments.yaml` as
`S01, S02, ...` with `source`, `start`, `end`, `hook`, `caption` and the platform from the topic
map (a comment line above each carries the score, topic, host check and reason). Your own entries
and comments are kept byte for byte; the previous file is copied to `moments.yaml.bak`. It prints
counts and the real cost, then the `campaign ... --only S01,S02` command to render them. A moment
that overlaps one already listed, rendered, posted or rejected for the same episode is skipped.
`campaign-reject` records a moment you do not want: never rendered, never proposed again.

### Several source videos

An entry may name its own `source:` (a path, relative to the repo root or absolute). Entries
without one use the video given on the command line. Each video keeps its own run folder.

## Outputs

Everything goes to `runs/_campaigns/<slug>/`, whatever the source video:

- `tiktok/`, `instagram/`, `youtube/`: one folder per platform in `platforms`, holding only
  `<id>.mp4` and `<id>.txt`. Upload the file, paste the text. TikTok and Instagram text is the
  platform's prefix and the caption, the hashtags, then the closer. YouTube text starts with
  `TITLE: ...` (the hook in sentence case, names and acronyms kept, at most 100 characters), a
  blank line, then the description with the YouTube prefix, the hashtags and the closer.
- `posting_plan.csv` (open in Excel): one row per clip, with its source video's name. Unposted
  clips get a date and time from `posting_times`, one slot each per platform per day from
  tomorrow, strongest first. Posted clips stay with `posted` yes, their posted date and real
  platform. The columns `views_48h`, `likes`,
  `comments`, `shares`, `pct_us_ca_uk`, `avg_watch_s`, `pct_completed` and `notes` are yours: what
  you type is never overwritten when the plan is regenerated (only an unposted clip's slot is).
- `posting_plan.pre-12-2.bak`: the plan as it was before the combined plan took over (Brief
  12.2); written once, never touched again.
- `campaign_report.html`: the review page, with the per-platform counts and, for scouted moments,
  the score, topic, host check and reason. A campaign without a watermark or closers shows those
  checks as "not required".
- `_meta/`: the clip jsons, caption files, review files, tracks and graphics analyses
  (`<id>_<platform>.graphics.json`, Brief 12.1); nothing to upload.

## One posting plan across campaigns (Brief 12.2)

Copy `campaigns/_example/schedule.yaml` to `campaigns/schedule.yaml` (git-ignored) and set:

- `posting_times`: the slots of a day, 24-hour Eastern, in quotes;
- `max_per_platform_per_day`: how many of them a platform takes (default: all);
- `priority`: the campaigns in strict order; each may carry `max_per_day`, the most of a
  platform's slots it takes in a day, so the next campaign still gets the rest. Campaigns not
  listed come last, alphabetically.

While the file exists, `runs/_campaigns/posting_plan.csv` is the one list to schedule from:
`date, time_et, platform, campaign, source, id, file, text_file, posted` and the result columns.
Slots run per platform from now (a time that has passed today is never used). Posted rows
follow each campaign's used.json. The result columns are yours and are never overwritten, keyed
by campaign and id. On the first build every per-campaign `posting_plan.csv` is copied to
`posting_plan.pre-12-2.bak` and its result columns are carried over; from then on each
campaign's `posting_plan.csv` is a read-only view of its rows (its first line says so). Edit
only the combined file. `campaign`, `produce`, `campaign-posted` and `--fix-platform` rebuild it;
`plan` rebuilds it on demand. Without `campaigns/schedule.yaml` every campaign keeps its own
plan as before.

## On-screen graphics

With `video.graphics.enabled: true` in `config/default.yaml` (the default; it needs
`clipmachine fetch-text-model` once) a camera shot whose on-screen text reaches outside the
speaker crop is rendered as the whole frame on a blurred fill, captions below it, zooms off; the
chapter title and show logo that sit in every frame are learned as overlays and ignored. The
report card says how many seconds of a clip use the fit layout and where the overlays sit. A
campaign may override any `video.graphics` key in campaign.yaml, like `video.zoom`.

## Times

`start` and `end` are source times, `h:mm:ss` or `mm:ss`. Put them in quotes: an unquoted
`1:05:26` is read by YAML as a number. `end` may be `auto` or left out; the tool then picks a
sentence end near the target length set in `config/default.yaml` (`campaign.auto_end_target_s`).
