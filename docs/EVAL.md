# EVAL.md

| Date | Run folder | Video (source, length) | Code version (commit) | Candidates | Kept | Usable / Needs edit / Reject | Failed checks | Cost ($) | Processing time | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-11 | `runs/2026-10-11_t3-2` | T3, third-party, private, 41:10 | `f36fc7f` | 17 | 8 | 6 / 2 / 0 | C6 ×2 | 0.0560 | 7 min 58 s (sum of the stage times in run.json: fetch 3 s, transcribe 165 s, candidates 14 s, score 16 s, render 280 s, report under 1 s; the render time is the speaker-crop re-render) | Rated by Igor. Usable: c05, c06, c12, c13, c01, c02. Needs edit: c09, c04 (the camera switches late in fast back-and-forth and misses short replies). No complaints on start or end cuts. Client `general`; Brief 10 settings (D-017, D-018); `video.crop: speaker` set in the run folder after the one-command run, then re-rendered from the render stage |
| 2026-10-12 | `runs/2026-10-12_t3` | T3, third-party, private, 41:10 | `0f086c2` | 16 | 6 | 5 / 0 / 1 | C1 + C2 on c09 | 0.0541 | 7 min 9 s (wall time of the one command; sum of the stage times in run.json 424 s: fetch 3 s, transcribe 166 s, candidates 13 s, score 16 s, render 226 s, report under 1 s) | **One command, no manual steps** (D-020: `video.crop: speaker` by default, `signal: mouth`, Brief 8.2 motion). Rated by Igor. Usable: c05, c11, c14, c01, c12. Reject: c09 (starts without its context, then runs into a different conversation, and ends mid-sentence). Cause of c09: the clip crosses an edit point of the source, which is a compilation (PROJECT_LOG 2026-10-12). No camera complaints. Client `general`. Candidate ids are new in this run, so they do not match row 1 |

## Other real runs (added for v1.0.0)

Collected from the cost ledger (`runs/_ledger/costs.jsonl`), the runs' `run.json` and
`PROJECT_LOG.md`. Sources are described, not named. Private third-party test videos (T1-T3) were
deleted after testing (D-012), so only the ledger and the log keep their numbers. Costs are the
booked USD of every call made for that video, reruns included. Whisper was `small` on an NVIDIA
GTX 1660 Ti (CUDA, int8_float16) unless a row says otherwise.

### Normal pipeline (`run`)

| Date | Video (source, length) | Candidates | Kept | Cost ($) | Processing time | Notes |
|---|---|---|---|---|---|---|
| 2026-10-10 | Two-person interview, third-party, private, 640x360, 15:23 | 8 (7 after Brief 5.1) | 1 | 0.0411 (8 calls, reruns included) | transcribe about 50-54 s, render 7.6 s | First end-to-end runs; used to check caption sync |
| 2026-10-11 | T1, screen-share podcast with picture-in-picture faces, third-party, private, 360p, about 35 min | 19 | 2, then 8 after Brief 9 | 0.0947 (6 calls) | transcribe 121 s (17.6x realtime); first one-command run about 2.5 min | The 2 kept before Brief 9 were rated usable |
| 2026-10-11 | T1 with Brief 10 settings | 19 | 8 | 0.0585 (5 calls) | transcribe 142 s | Not rated |
| 2026-10-11 | T2, studio test, third-party, private, at most 20 min | not recorded | not recorded | 0.0176 (3 calls) | not recorded | |
| 2026-10-11 | T3, 41:10, first run | 18 | 6, then 7 | 0.0981 (7 calls) | transcribe 138 s | 2 usable, 1 needed camera fixes, 4 failed on cut points; led to Briefs 9 and 10 |
| 2026-10-12 | A streamer interview (campaign source, permission to clip), 97:00, AV1 1080p, 1.03 GB | 20 | 3 (mean score 7.33) | 0.0730 (11 calls: find 0.0094, score 0.0636) | 12 min 40 s (stage sum 759.9 s: fetch 14.0 s, transcribe 596.7 s at 10.2x realtime, candidates 24.1 s, score 15.6 s, render 109.3 s for 3 clips, report 0.2 s) | The campaign allowed only human-picked moments, so its 32 clips were rendered in campaign mode at $0 (the transcript was reused) |
| 2026-10-15 | NASA in-flight interview (public domain, the README demo; `docs/media/SOURCE.md`), 12:42, 720p | 4 | 3 (scores 9, 8, 8) | 0.0181 (2 calls) | 6 min 56 s wall (stage sum 402.9 s: fetch 5.1 s, transcribe 164.2 s on the **CPU**, 5.6x realtime, candidates 3.0 s, score 6.9 s, render 223.6 s for 3 clips, report 0.1 s) | Fresh clone following the README, `--client general`, run cap set to $0.50. Commit `98c5e0c`. Not rated by Igor |

### Campaign mode with the AI scout (`produce`)

A podcast campaign whose rules allow AI-picked moments. Per episode: fetch and transcribe, then
Haiku proposes candidates and Sonnet scores them (the scout), then the kept moments are rendered
at 1440p with the campaign watermark. "Kept" = proposed to Igor; "Posted" counts clips marked
posted by 2026-10-15 night (including the 8 marked ahead at their Oct 16 slots). Scout time is the
span from the first to the last API call of that episode, so it leaves out local work and includes
failed tries.

| Date | Episode | Length | Candidates | Kept | Posted / rejected | Cost ($) | Calls | Fetch + transcribe | Scout | Render |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-13 | P1 | 224.3 min | 46 | 8 | 7 / 1 | 1.0797 | 132 | 24.8 s + 1352.6 s (10.5x) | 4219 s (4 tries, see note 1) | 43.6-75.8 s per clip |
| 2026-10-14 | P2 | 102.3 min | 21 | 6 | 4 / 2 | 0.0917 | 14 | 18.1 s + 424.0 s (15.6x) | 51 s | 371.8 s (6 clips) |
| 2026-10-14 | P3 | 228.9 min | 23 | 0 | 0 / 0 | 0.0981 | 25 | 39.0 s + 1084.6 s (13.4x) | 67 s | none (the judge rejected 22, 1 adjustment ignored) |
| 2026-10-14 | P4 | 236.9 min | 24 | 7 | 6 / 1 | 0.1680 | 27 | 32.6 s + 1086.6 s (13.8x) | 91 s | 445.4 s (5 ok, 2 failed) |
| 2026-10-15 | P5 | 250.3 min | 50 | 11 (mean score 8.00) | 1 / 0 | 0.2868 | 31 | 49.0 s + 1106.1 s (14.8x) | 135 s | 902.9 s (11) |
| 2026-10-15 | P6 | 269.2 min | 52 | 7 | 0 / 0 | 0.5099 (2 tries) | 62 | 41.8 s + 1242.6 s (13.8x) | 256 s | 331.9 s (6 ok, 1 no hook) |
| 2026-10-15 | P7 | 95.0 min | 35 | 14 | 0 / 2 | 0.3842 (2 tries) | 26 | 13.8 s + 382.3 s (15.8x) | 173 s | 755.1 s (9 ok, 5 no hook) |
| 2026-10-15 | P8 | 241.5 min | 47 | 8 | 0 / 1 | 0.2915 | 30 | 33.6 s + 1014.7 s (15.1x) | 133 s | 341.9 s (4 ok, 4 no hook) |
| 2026-10-15 | P9 | 277.0 min | 55 | 14 | 0 / 1 | 0.2630 | 32 | 37.4 s + 1106.3 s (16.0x) | 138 s | 814.0 s (13 ok) |
| 2026-10-15 | P10 | 279.1 min | 56 | 5 | 0 / 1 | 0.3265 | 34 | 43.3 s + 1089.9 s (16.4x) | 152 s | 318.1 s (4 ok) |
| **Total** | 10 episodes | **2204.5 min (36.7 h)** | **409** | **80** | **18 / 9** | **3.4994** | 413 | | | |

- Cost per episode: mean $0.350, median $0.289, range $0.092-$1.080 ($0.269 mean without P1).
  Per hour of source: $0.095. Per kept clip: $0.044. Per posted clip so far: $0.194.
- Keep rate 80 of 409 (19.6 %). Scores of the 80: 9 ×4, 8 ×39, 7 ×30, 6 ×7 (mean 7.50).
- Note 1: P1 cost $1.08 because three of four scout tries died on the hook-length rule after their
  scoring calls were paid; the $1 run cap then refused a rerun. Three clips were recovered at $0
  with `scout --reuse-scored`. The hook length became a soft check on 2026-10-15 (`bc5c77c`).
- "No hook": 12 of the 59 overnight proposals still had a hook over 7 words after the repair call.
  Igor wrote 11 hooks; those re-renders ran with `CLIPMACHINE_NO_API=1` ($0, 0 calls).
- Rough wall-clock for a 4-4.6 h episode, end to end: about 35-37 min (P5: 2193 s; P9: 2095 s).
- The same campaign infrastructure rendered 32 human-picked clips for a second campaign (a
  streamer interview) at $0. Across both campaigns: 111 clips rendered, 41 posted or marked by
  2026-10-15, 18 rejected.

### Whole ledger

466 paid calls, all status ok, 2026-10-11 to 2026-10-15: **$4.0123**. Score (Sonnet 5.5) 69 calls,
$3.6685 (91.4 %); find_moments (Haiku 5.5) 396 calls, $0.3240 (8.1 %); campaign-init (Sonnet 5.5) 1
call, $0.0198. Repair calls (22, all scoring) cost $1.2118, 30.2 % of the total. The worst-case
estimate the budget guard checks came out at about twice the actual cost (for example $2.2137
against $1.0797 for P1).
