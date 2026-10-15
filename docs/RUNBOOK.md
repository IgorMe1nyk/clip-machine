# RUNBOOK — the daily routine (Brief 12.2)

Run everything from the repo root with `uv run python -m clipmachine ...`. Nothing below calls
the API except `campaign-init` (one call, cents) and `produce` on a campaign with `selection: ai`
(the scout: about $0.10–0.15 per hour of source video).

## New campaign (under 10 minutes)

1. Save the campaign's brief as `.txt`, `.md` or `.pdf` (text, not scans) anywhere in the repo,
   for example `runs/_scratch/briefs/<slug>.txt`. If the campaign supplies a watermark, save the
   PNG; if it supplies a list of moments with timestamps, save that as a text file too.
2. `campaign-init <slug> --brief <file> [--watermark <png>] [--moments <list.txt>]`
3. Open `campaigns/<slug>/campaign.yaml` and review **every line**. The header says it is a
   draft; the `# confirm:` lines say what the model was unsure of. Check in this order:
   `selection` (human unless the rules clearly allow tools), `platforms`, `post.prefix`,
   `post.closers`, `post.hashtags`, `clip_length`, the watermark, `names`. Remove `scout:` or
   leave it; it only runs with `selection: ai`.
4. Add the campaign to `campaigns/schedule.yaml` (`priority`, and `max_per_day` if it must share
   the days with another campaign).
5. Download an episode (per the campaign's instructions) and run `produce`.

## Each episode

```
produce <episode-video> --campaign <slug>
```

- `selection: ai`: the scout proposes moments for this episode (once per episode; `scout` can
  redo it), then everything unposted is rendered.
- `selection: human`: add the moments to `moments.yaml` first (with `source:` when the campaign
  has several episodes), then `produce` renders them.
- Clips whose entry did not change are kept; `--rerender` redoes them. Posted and rejected
  moments are never touched.
- It prints the new clips per platform, their slots and the API cost, and rebuilds the report
  and the posting plan.

## Review, schedule, post

1. Open `runs/_campaigns/<slug>/campaign_report.html`. Watch each new clip once. A moment that
   should not go out: `campaign-reject --campaign <slug> --id <id>`, then `produce` again. A
   wrong hook or time: edit `moments.yaml`, then `produce` again (only that clip re-renders).
2. Schedule from **one list**: `runs/_campaigns/posting_plan.csv` (open in Excel). Each row
   names the campaign, the platform, the date and time, the file to upload
   (`<slug>/<platform>/<id>.mp4`) and the text to paste (`<slug>/<platform>/<id>.txt`).
   The per-campaign `posting_plan.csv` files are read-only views of the same rows.
3. After each post goes live: `campaign-posted --campaign <slug> --id <id>`. The plan updates
   itself (the row shows `posted` yes with the real date and platform). A wrong platform:
   `campaign-posted ... --fix-platform <platform>`.
4. **At 48 h:** submit to the campaign (analytics screenshots with viewer location), then fill
   the row's result columns (`views_48h`, `likes`, `comments`, `shares`, `pct_us_ca_uk`,
   `avg_watch_s`, `pct_completed`, `notes`). What you type is never overwritten.

## Changing the schedule

Edit `campaigns/schedule.yaml` (times, posts per platform per day, campaign order and caps),
then `plan`. Unposted rows move to the new slots; posted rows and your result columns stay.

## When something looks wrong

- `produce` says a moment has no `source:` and was rendered from another episode: add
  `source:` to that entry (the campaign has more than one episode now).
- A clip was re-rendered that you did not expect: its entry, its source file or its files on
  disk changed; the console line says "unchanged ... kept" for the ones that did not.
- The combined plan lost a campaign: its `campaign.yaml` is missing or the folder name is not a
  valid slug; `plan` lists only folders with a `campaign.yaml`.
- The first `plan` after the switch copied each old per-campaign plan to
  `runs/_campaigns/<slug>/posting_plan.pre-12-2.bak`; those copies are never touched again.
