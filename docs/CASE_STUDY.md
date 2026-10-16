# Case study: Clip Machine (draft)

*Draft, 2026-10-15. Numbers come from the cost ledger, the run records and the project log; see
`docs/EVAL.md` for the per-run tables.*

## The problem

Clipping long podcasts into Shorts, Reels and TikToks is paid work. Clipping campaigns pay per
view, and creators pay editors to do it. Most of the time goes into three jobs: finding the 30-90
seconds worth posting in a 3-4 hour episode, cutting them so they start and end on whole thoughts,
and reframing a wide 16:9 studio shot into a 9:16 phone frame that follows whoever is talking.
Commercial tools do this, but they're closed, priced per minute, and hard to adapt to one
campaign's rules (watermarks, banned words, one platform per clip, a posting cap per day).

The goal was a local tool that does the whole job for one operator. API spend had to stay inside
the $100 monthly credit, with $0 cash, and nothing could be posted without a human looking at it.

## The approach

A Python CLI with six restartable stages: fetch, transcribe, candidates, score, render, report.
Each stage reads and writes files in a run folder, so a failed or disappointing stage can be
rerun without redoing the ones before it.

- **Local where possible.** Whisper (faster-whisper) transcribes on the GPU when there is
  one. MediaPipe and YuNet track faces, and ffmpeg cuts, crops and burns in captions. Only text
  goes to the API: the transcript, the client's rules and campaign briefs.
- **Two models with different jobs.** Claude Haiku proposes candidates per 10-minute block, for
  about a tenth of a cent per call. Claude Sonnet then judges them together, up to 20 per
  call: a score, keep or reject, a hook and a title. The model never supplies timestamps. It
  picks sentence numbers, and the times come from Whisper's word timestamps, so every clip
  starts and ends on a sentence boundary.
- **Guard rails before the first API call.** A budget guard prices the worst case of every call
  before sending it. There's a kill switch, schema-validated output with one repair attempt, and
  the transcript is fenced as untrusted data.
- **Campaign mode on top.** It handles watermark placement, caption word fixes, per-platform
  post text, a posting plan across campaigns with daily caps, and marking posts and rejections.
  An AI "scout" proposes moments when a campaign's rules allow AI-picked clips.

## How the work was split

- **Me (the owner):** every decision (40 decision records), checkpoint reviews, clip ratings,
  the hooks and titles I rewrote, rejections, and the business side: picking campaigns, the
  posting accounts, posting and pinned comments.
- **AI chat (Claude):** research with sources, the cost register, the MVP definition and the
  rating rubric, the roadmap, one brief per step, and reviews of plans and reports.
- **Claude Code:** implementation. It planned each brief, then built in small commits with tests,
  ran ruff, mypy and pytest separately on every change (pip-audit when dependencies
  changed), and had a fresh-context reviewer read risky diffs before merge. It ran inside a
  permission allow-list; its file tools were denied the secrets file, and it never pushed
  without me.

## Timeline and numbers

- **Six days**, 2026-10-10 to 2026-10-15: 242 commits up to the release prep, from 24
  written briefs.
- **Tests:** 104 after the first session, 729 when all six stages worked (night of day 1), and
  2,320 at v1.0.0 (2,317 passed, 3 skipped in the last run).
- **Day 2:** the MVP finish line was met. On a 41-minute two-person podcast, 6 of 8 kept clips
  were rated usable, 2 needed edits and none was rejected. A day later, a one-command run on
  the same video gave 5 usable clips out of 6.
- **Days 3-4:** captions with a hook line, punch-in zooms, campaign mode, the AI scout and the
  graphics-aware layout. The first clips were posted on day 4.
- **By day 6:**
  - 10 episodes of a long-form podcast scouted: 36.7 hours of video, 409 candidates, 80 clips
    proposed.
  - 111 campaign clips rendered across two campaigns: a podcast campaign and a streamer
    interview.
  - 41 clips posted or scheduled, 18 rejected by me.
- **Cost:**
  - **All API spend to date:** $4.01 over 466 calls, paid from the included credit.
  - **AI scouting:** $0.35 per episode on average (median $0.29), $0.095 per hour of source
    and $0.044 per proposed clip.
  - **Normal pipeline:** a 41-minute episode cost $0.05-0.06.
  - **Human-picked campaign clips:** $0, because campaign rendering runs with the API switched
    off.
- **Speed:** a 4-4.6-hour episode took about 35-37 minutes end to end on a GTX 1660 Ti.
  Transcription ran at about 15x realtime (10-16x).

## Six bugs worth telling

**1. Cuts that weren't detected.** I noticed it while reviewing clips: in a dark studio the
crop lagged on camera cuts, showing a table and a toy instead of the speaker. Shot-cut detection
compared colour histograms with a threshold tuned on one video, where real cuts measured
0.37-0.42. In that studio the close-up and the wide shot share their colours, so real cuts
measured 0.077-0.114 and none was detected; the crop stayed on the old shot for 0.5-1.7 seconds.
Simply lowering the threshold would have made photo overlays and gestures count as
cuts. The fix pairs a lower colour threshold with a layout-change test on a small grey grid.
Measured on 52 rendered clips from both campaigns, all 16 real cuts are found and none of the 10
in-shot changes are.

**2. A client's moment list running 25 seconds late.** A campaign supplied chapter timestamps
for the moments it wanted. An audit script printed the transcript from 45 seconds before each
listed start. It showed that times after about minute 25 ran about 25 seconds late, so one
posted clip started just after the line its own hook was about. The fix was data, not code:
the affected moments moved 21-27 seconds earlier and were re-rendered. Client timestamps are
now treated as claims to check against the transcript.

**3. Data loss caught in review.** The combined posting plan rewrote its CSV from the campaigns
that loaded. An earlier review fix had made a broken campaign file "left out with a warning", so
one typo in a campaign's moment list would have dropped that campaign's rows from the plan for
good, including the result columns typed in by hand. A reading guide written for the merge
review flagged it as the main risk in the branch. Now a campaign that fails to load keeps its
rows exactly as they were. Three tests that fail on the old code pin this down.

**4. The allocator that forgot what was already posted.** The first combined scheduler gave
every day a full set of slots. Before trusting the plan, I compared it with my real hand-made
schedule: it wanted 10 extra posts that day, because posts already made that day weren't
counted. The fix starts each day's
counters from the posts already dated that day and gives the free slots the latest times. A
second allocator bug turned up in the real marking run on day 6. A post stamped on a slot's
exact minute used up the earliest free slot instead of its own, and that was fixed too.

**5. A state change that didn't apply.** A planned swap was meant to un-post five clips from one
campaign and post five from another. At the start of the next day's run, a verify step read the
state back and found the swap had never happened. The records still had the old five posted.
A one-off script with assertions on the exact five ids and backups taken first applied it. The
rebuilt plan was then diffed against the backup row by row.

**6. A posted time that recorded the command, not the slot.** `campaign-posted` stamped the
current time. That only showed up when marking the next day's scheduled posts the night before.
They would have been dated today, today's slots would have filled twice, and every later clip
would have shifted a day earlier. A post is now recorded at its slot in the plan, with an
override flag. A new `--date` option marks a whole day after listing it and checks every mark
before writing anything. A fresh-context review found four more edge bugs, all fixed in the same
commit with a test each.

## Lessons

- **A threshold tuned on one video is a guess.** Measure it on many real clips, including the
  ugly ones, before trusting it.
- **Read state back after you change it.** Two of the six bugs were caught that way, not by
  tests.
- **"Skip with a warning" in a writer is data loss.** Anything that rewrites a file must
  carry over what it couldn't read.
- **Record intent, not the moment of the command.** Scheduling ahead is when that difference
  shows.
- **Put the money guard in before the first call.** It refused a real rerun that would have
  passed the $1 run cap. Repair calls for invalid model output
  were 30 % of all spend, so output validity is a cost issue too.
- **Small briefs, small commits, fresh-context reviews.** Each review that read a diff with no
  memory of writing it found something the author had missed.

## What isn't proven yet

- **Results:** there are no view or payout numbers yet. The results columns are empty until
  the first full posting week ends.
- **Camera:** the active-speaker choice doesn't beat "never switch" on fast back-and-forth
  between two people, so the default stays conservative.
- **Product:** there's no UI yet. Everything runs from the terminal.
