# Example campaign

This folder shows the layout of a campaign folder. Everything in it is invented. Copy it to
`campaigns/<slug>/` and replace the content. The slug is lowercase letters, digits and dashes
(a leading underscore is allowed).

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
uv run python -m clipmachine campaign <source-video> --campaign <slug> [--only A03,B05] [--force-reuse]
uv run python -m clipmachine campaign-posted --campaign <slug> --id A03 [--platform tiktok]
uv run python -m clipmachine campaign-posted --campaign <slug> --id A03 --fix-platform instagram
```

The first renders the moments. The second marks a moment as posted, so it is not reused by
accident; without `--platform` it uses the moment's assigned platform. The third corrects the
platform of a posted entry that was recorded wrongly.

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
- `campaign_report.html`: the review page, with the per-platform counts and, for scouted moments,
  the score, topic, host check and reason. A campaign without a watermark or closers shows those
  checks as "not required".
- `_meta/`: the clip jsons, caption files, review files and tracks; nothing to upload.

## Times

`start` and `end` are source times, `h:mm:ss` or `mm:ss`. Put them in quotes: an unquoted
`1:05:26` is read by YAML as a number. `end` may be `auto` or left out; the tool then picks a
sentence end near the target length set in `config/default.yaml` (`campaign.auto_end_target_s`).
