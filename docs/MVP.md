# MVP.md — Walking Skeleton: Finish Line, Scope, Rubric

> **Status:** APPROVED by Igor 2026-10-10 (Gate 0). Test video (D-006) still to be picked from section 4.

## 1. Finish line (the sprint ends when ALL of these are true)

1. **One command:** `python -m clipmachine run <test-video> --client example-client` takes one 10–15 minute test video (rules in section 4) and finishes with no manual steps.
2. **Output:** vertical 1080×1920 MP4 clips with burned-in captions, plus a `report.html` review page.
3. **Quality:** at least **3 clips rated "usable"** under the rubric in section 5. Usable means you'd send it to a stranger as a sample. Not perfect.
4. **Measured:** the run's real cost (`costs.json`), processing time and clip ratings are recorded as the first row of `docs/EVAL.md`.
5. **Safe stop:** everything is committed, tests pass, and `docs/PROJECT_LOG.md` has the entry and the next step.

Not required for the finish line: polish, a second video, two-speaker framing, or nice caption styling.

## 2. Scope

| In (walking skeleton) | Out (parked) |
|---|---|
| Local video file input only | yt-dlp, URLs, "most replayed" heatmap |
| ffprobe validation: type, size, duration | Audio-energy and laughter signals |
| faster-whisper with word timestamps (CPU or GPU) | Face tracking (Phase 2) |
| One AI candidate pass and sentence snapping | Hook text overlay, progress bar, logo |
| AI scoring with API-enforced structured output and pydantic validation | Clip brain and niche patterns (Phase 3) |
| Budget guard and `costs.json` | Batch processing, database, dashboard |
| Cut, **center crop**, simple word-group captions (`.ass`) | Animated captions, Remotion |
| Basic static `report.html` (designed before it's built) | Approve/reject buttons, delivery to Drive |

Anything else that comes up goes to the parked list in PROJECT_LOG.md.

## 3. Brief sequence (each one ends committed and tested)

| # | Brief | Done when |
|---|---|---|
| 1 | Skeleton: package, config, CLI, run folders, stub pipeline, CI, guardrails | Acceptance checks pass |
| 2 | Fetch: local file → `source.mp4`, `audio.wav`, `meta.json` via ffprobe/ffmpeg | Real test video in; files out; bad input rejected |
| 3 | Transcribe: faster-whisper → `transcript.json` with words | Timestamps spot-checked against the video |
| 4 | Candidates: AI pass + snapping → `candidates.json` | Around 20 candidates, none starting mid-word |
| 5 | Score: structured output, budget guard, reject reasons → `scored.json`, `costs.json` | Every output validates; cost logged |
| 6 | Render: cut + center crop + captions → `clips/*.mp4` | Plays on a phone, captions in sync |
| 7 | Report: `report.html` (layout and states designed first) | Opens offline; every clip, score and reason shown |
| — | Run the rubric on the test video → EVAL.md | Finish line reached, or a list of what's wrong |

**Before Brief 7:** a short `docs/PRODUCT.md` (who uploads, who reviews, what "done" means for a client) and a sketch of the review page.

## 4. Test video rules

**Requirements:**
- 10–15 minutes, mostly speech, ideally **one speaker** facing the camera. Center crop is only a fair test for that.
- At least one stretch with a clear point or story, so a "usable" clip is possible at all.
- **Rights must be clean,** even though nothing gets posted.

**Allowed sources, best first:**
1. **Your own recording.** For example, 12 minutes of you on your phone explaining Svitlo or this project. You own it fully, and it doubles as build-in-public material.
2. **Wikimedia Commons** videos marked CC0, CC BY, CC BY-SA, or public domain. Commons only accepts licenses that allow commercial use and modification. [fact] Credit the author if the license is BY.
3. **Internet Archive** items whose page shows CC BY, CC BY-SA, CC0 or public domain. Check each item's page. [fact: rights vary by item]

**Not allowed:**
- **YouTube downloads.** YouTube's Terms reportedly forbid downloading except through YouTube's own features or with permission. [Igor to read the clause himself]
- **TED talks.** They're licensed CC BY-NC-ND, and "no derivatives" rules out clipping. [fact]
- Anything marked ND, or anything with unclear rights.

> **Learn — copyright vs terms of service.** "I won't post it" lowers the copyright risk. It doesn't change a platform's terms of service, which are a contract about *how you got the file*. The sources above avoid both problems.

## 5. Clip rubric (from plan section 10)

Rate every clip on these checks, then give one overall rating.

| # | Check | Type |
|---|---|---|
| C1 | Starts and ends on complete sentences; no cut-off words | Hard |
| C2 | Makes sense with zero context | Hard |
| C3 | First ~3 seconds give a reason to keep watching | Soft |
| C4 | Has a payoff: a point, punchline, insight or emotional peak | Hard |
| C5 | Captions correct (names and numbers checked) and in sync | Fixable |
| C6 | Face fully in frame; speaker reasonably centered | Hard |
| C7 | Audio levels fine; no clipping or silence gaps | Fixable |
| C8 | Length within the configured range | Fixable |
| C9 | No sponsor reads or off-limits topics | Hard |

**Overall rating:**
- **Usable:** passes every Hard check and every Fixable check; C3 is at least "okay." Ship it as a sample.
- **Needs edit:** passes every Hard check, but one or more Fixable checks fail and can be fixed in about 5 minutes.
- **Reject:** fails any Hard check.

**Each rating also records:**
- **Which checks failed,** using the check numbers (for example "C2, C4"), so failures can be counted over time.
- **The model's verdict next to yours.** Where you and the model disagree is the most useful data for improving the scoring prompt.

## 6. EVAL.md row format

| Date | Run folder | Video (source, length) | Code version (commit) | Candidates | Kept | Usable / Needs edit / Reject | Failed checks | Cost ($) | Processing time | Notes |
|---|---|---|---|---|---|---|---|---|---|---|

Every number in this table comes from a real run (`run.json`, `costs.json`, your ratings). Never from the plan or a guess.
