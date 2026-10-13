# HANDOFF — Clip Machine (read this first)

> **Written:** 2026-10-12 01:32 ET, end of session 4 (Sun Oct 11 15:48 → Mon Oct 12 ~01:35).
> **Read next:** `ROLE-instructions.md` (how to work with Igor; skip its `<first_move>`, since Gate 0 is done; don't re-ask PC specs or budget; do ask for today's stop time), then `docs/ROADMAP.md`, `docs/EVAL.md`, and the newest `docs/PROJECT_LOG.md` entries.
> **Source of truth:** the repo (`C:\Users\15513\dev\clip-machine`, public as IgorMe1nyk/clip-machine). Repo docs are newer than any copy here.

## 1. Where we are
**The MVP finish line is met, with one command and no manual steps.**
- `uv run python -m clipmachine run <video> --client general` → finished vertical clips with the speaker-following crop, synced captions, clean cuts, and a review page.
- **EVAL.md row 2** (`runs/2026-10-12_t3`, T3 = 41-min two-person studio podcast, private): 16 candidates → 6 kept → **5 usable, 0 needs edit, 1 reject**, in **7 min 9 s**, **$0.054**.
- **EVAL.md row 1** (`runs/2026-10-11_t3-2`, one manual step): 8 kept → 6 usable, 2 needs edit (camera switching), 0 reject.
- Caveat: test videos are third-party and private (D-012). **Public samples must come from permissioned creator content.**

**Check first:** commit `f6c6654` (EVAL row 2 + c09 diagnosis) and possibly later docs commits are local. **Push, and check CI green.**

## 2. What was built in session 4
| Brief | Result |
|---|---|
| 8.1 small faces + cut detection | YuNet detector (D-016): wide-shot faces 0% → 100%; camera resets at shot cuts (frame-exact) |
| 8 Part B moving crop | ffmpeg `sendcmd` crop path; verified frame-exact at cuts |
| 9 boundary repair | Judge can extend clips; T1 kept 2 → 8 |
| 10 clean boundaries | Whisper `small` + style prompt in every window + conditioning (D-017): unpunctuated runs T3 45% → 5%; cuts only at real sentence ends with a pause ≥ 0.15 s; mid-pause cut times; 80 ms audio fades; max clip 90 s (D-018) |
| 8.2 camera polish | Smooth "operator" motion (moving episodes 64 → 24). The smarter switching signal (mix) **failed the held-out test** (false cuts); default stays `signal: mouth` (D-019). `video.crop` default = `speaker`, with an early model-file check (D-020) |
| Other | Speaker/no-pause rules measured against Igor's hand labels; sound hooks in Igor's user settings (chime = needs input, ding = finished) |

## 3. Open problems (evidence-based, in priority order)
1. **Wide two-person shots: who to show.** Lip reading fails on people turned sideways; the mixed signal gave false cuts. **Proposed fix: Brief 8.3, a stacked layout** (both people, top and bottom, filling 9:16, no borders) whenever two faces share a wide shot. That removes most switching decisions. Reaction shots on punchlines are wanted (Igor liked one).
2. **Compilation edit points** (T3 is a "best of" compilation): c09 crossed a source edit. Short moments (< 20 s) get grown across edits, and judge extensions can too. Fix: detect edit points (camera-cut burst with no face, topic jump) and forbid clips from crossing them. One case seen.
3. **A Whisper "." at a mid-sentence pause** passes the clean-cut rules (one case: c09's end). Watch for more before fixing.
4. **The judge's extension reasons can be wrong** ("ends cleanly on the finding" when the finding came next). Brief 9.1 (rubric) may help.
5. **Names misspelled** ("Claude" → "Claw"): the vocabulary brief, via Whisper hotwords (the same mechanism as D-017's style prompt).

## 4. Order of work (next sessions)
1. **Brief 8.3: stacked layout for wide two-shots** (design with Igor first: when to stack, how to frame each half, transitions).
2. **Edit-point guard** (small brief).
3. **Vocabulary brief** (per-client hint words).
4. **Brief 9.1: scoring rubric** from `docs/research/clip-scoring-guide.md` section 7 (test on frozen candidates).
5. **Polish:** loudness, caption placement and safe zones, a low-resolution warning, never a watermark.
6. **The UI** (design first: PRODUCT.md v2, stack decision D-0xx).
7. Then ops (batch, retention) and proof (README, demo, case study).

## 5. Decisions added this session
D-016 (YuNet small-face detection) · D-017 (Whisper bhc punctuation setting) · D-018 (max clip 90 s) · D-019 (speaker signal: mouth stays; mix kept in code, failed held-out) · D-020 (default crop = speaker, early model check). **Still open:** D-006 update (private validation on third-party videos; public proof only from permissioned creators). Igor hasn't formally confirmed it; ask.

## 6. Parked
Framing width (arms cut at the edges; judge on finished clips) · laughs in pixel-motion windows · pauses measured from audio instead of Whisper gaps · turbo + conditioning as a fallback transcription setting · Haiku candidate variance · overlap threshold maybe ~30% · score drift +1 after the Brief 9 prompt · **clean up risky "always allow" entries in `C:\Users\15513\.claude\settings.json`** (`Read(//c//**)`, `Bash(uv lock *)`, `Bash(python -)`, `Bash(python - *)`, `Bash(git stash *)`, `Bash(git rm *)`) · the model licence [gap] (confirm before clients) · telemetry measurement.

## 7. Standing rules (abbreviated)
- **Secrets:** the API key lives only in `.env` as `CLIPMACHINE_ANTHROPIC_API_KEY`. Never export `ANTHROPIC_API_KEY`.
- **Money:** $0 cash; Max API credit only (current $100 expires Oct 23, renews monthly); Console spend limit $5. New paid dependency → ask Igor.
- **Privacy:** no transcript text, hooks, titles, frames or video names in commits, tests or logs. Test videos live in `C:\Users\15513\Videos\clip-tests\` (t1.mp4, t3.mkv). Never cloud-sync clips; delete after testing.
- **Repo hygiene:** always `uv run`; scratch only in `runs/_scratch/`; never `uv lock --upgrade` without a brief; never pipe checks without pipefail.
- **Workflow:** Opus plans (High for risky briefs) and reviews; a Sonnet 5.5 Medium subagent builds; each check run separately. **Measure before choosing** (use Igor's hand labels as ground truth; keep a held-out window). One brief at a time per code area; planning sessions can run in parallel (read-only).
- **Reply format:** a senior colleague; "Learn:" notes; label fact, judgment or estimate; status shows Build, Business, time and API; end with the next step and its owner; check the clock with the time tool.

## 8. Numbers
API spend about **$0.45** of $100. Tests **1250**. A 41-min episode: about 7 min end to end (transcribe about 165 s on GPU, render about 230 s for 6 speaker-crop clips), about $0.05.

## 9. Business track
Not started. Next: **pick a niche** (it decides layout priorities), **join creator clipping campaigns** (permissioned public proof), then a target list. **Svitlo opens Oct 20.**

## 10. Session history
| Session | Date and time (ET) | What happened |
|---|---|---|
| 1 | Oct 10, 02:11–04:00 | Gate 0, setup, Brief 1 |
| 2 | Oct 10, 17:47–20:00 | Briefs 2 and 3 |
| 2b | Oct 10, ~20:00–22:00 | API key; Briefs 4 and 4.1 |
| 3 | Oct 10 22:39 – Oct 11 ~01:55 | Briefs 5.1, 6, 6.1, 7, 8 Part A; first full run; ROADMAP; research guide |
| 4 | Oct 11 15:48 – Oct 12 ~01:35 | T2/T3 tests; 8 checkpoint; 8.1; 8 Part B; 9 real run; 10; 8.2; **MVP finish line met (EVAL rows 1–2)** |
