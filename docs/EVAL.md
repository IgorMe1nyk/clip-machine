# EVAL.md

| Date | Run folder | Video (source, length) | Code version (commit) | Candidates | Kept | Usable / Needs edit / Reject | Failed checks | Cost ($) | Processing time | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-11 | `runs/2026-10-11_t3-2` | T3, third-party, private, 41:10 | `f36fc7f` | 17 | 8 | 6 / 2 / 0 | C6 ×2 | 0.0560 | 7 min 58 s (sum of the stage times in run.json: fetch 3 s, transcribe 165 s, candidates 14 s, score 16 s, render 280 s, report under 1 s; the render time is the speaker-crop re-render) | Rated by Igor. Usable: c05, c06, c12, c13, c01, c02. Needs edit: c09, c04 (the camera switches late in fast back-and-forth and misses short replies). No complaints on start or end cuts. Client `general`; Brief 10 settings (D-017, D-018); `video.crop: speaker` set in the run folder after the one-command run, then re-rendered from the render stage |
