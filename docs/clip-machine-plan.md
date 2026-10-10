# Clip Machine — Project Plan

> **Note (2026-10-10):** Section 15's Brief 1 is superseded by `docs/brief-01-skeleton.md`. Where this plan conflicts with the docs in `docs/` (brief, COSTS.md, MVP.md, DECISIONS.md), those docs win. Prices and conversion figures below are estimates, not facts.

> Working name. An AI system that turns long videos (podcasts, interviews, YouTube episodes) into short vertical clips (Shorts / Reels / TikTok), chooses the moments based on what actually performs, and sells that as a clipping service to creators.
>
> Owner: Igor · Started: October 2026 · Status: planning → building v1

---

## 1. What it is (in one minute)

You give the system one long video. It:

1. Transcribes it with exact word timings.
2. Finds 15–20 *possible* clip moments cheaply.
3. Asks Claude to score each one, with a reason.
4. Keeps the best 5–10.
5. Cuts each one, makes it vertical (9:16), and burns in captions.
6. Gives you a review page where you approve or reject.

You then deliver the approved clips to a client (a YouTuber or podcaster), who posts them on **their** channels.

**Why this beats generic tools like OpusClip:** generic tools know how to *cut*, but not *which moment deserves to be a clip* in a given niche. Our edge is the **clip brain**: scoring rules and patterns learned from clips that actually went viral in the client's niche, plus per-client rules.

---

## 2. Why build it (goals)

| Goal | What "success" looks like |
|---|---|
| **Portfolio** | Public GitHub repo, a clean README with an architecture diagram, a demo video, a written case study with real numbers |
| **Side hustle** | 1 paying client in the first ~2 months, then 3 clients at roughly $300–800/month each |
| **Learning** | Hands-on with LLM APIs, structured outputs, speech-to-text, video processing (ffmpeg), pipelines, and later databases and dashboards |

---

## 3. How it makes money

**Model:** a done-for-you clipping service with a monthly retainer. You are not selling software; you are selling clips that perform. The software is what lets one person do the work of a small team.

**Market prices** (from a clipping-tool company's guide; treat as the optimistic end):

| Client size (listens/views per episode) | Clips per month | Price per month |
|---|---|---|
| Small (1K–5K) | ~20 | $300–500 |
| Mid (5K–25K) | 30–40 | $500–1,000 |
| Larger (25K–100K) | 40–60 | $1,000–1,500 |

**Realistic plan for you:**
- First client: discounted trial (2 weeks, $0–150) to get a result and a testimonial.
- Next clients: $300–500/month.
- Your time per client once the system works: about 3–6 hours a month (review, small fixes, delivery).
- 3 clients ≈ $1,000–1,500/month for about 10–20 hours of work.

**Business rules:**
- Clips go on the **client's** channel, with written permission. This avoids copyright problems.
- You never claim guaranteed views.
- Ask clients to send the **original video files** when possible (better quality, and no need to download from YouTube).

---

## 4. How it works: the pipeline

The system is an **assembly line of separate stages**. Each stage does one job, reads the previous stage's output file, and writes its own output file. If a stage crashes, you rerun only that stage.

```
 ┌──────────┐   ┌────────────┐   ┌─────────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
 │ 1. FETCH │ → │ 2. TRANS-  │ → │ 3. FIND     │ → │ 4. SCORE │ → │ 5. RENDER│ → │ 6. REVIEW│
 │ video in │   │   CRIBE    │   │ CANDIDATES  │   │ (Claude) │   │ cut+crop │   │ report   │
 └──────────┘   └────────────┘   │ (cheap)     │   └──────────┘   │ +captions│   └──────────┘
                                 └─────────────┘                  └──────────┘
   source.mp4    transcript.json   candidates.json   scored.json    clips/*.mp4   report.html
```

### The most important design idea: cheap first, expensive last

In a reference build we studied (a Twitch/Kick clip bot), only **3 of 100** candidate moments survived. So:
- **Stage 3 (cheap)** throws out most of the junk with simple signals and a cheap AI pass.
- **Stage 4 (expensive)** spends real AI effort only on the best 15–20 candidates.

This keeps the API cost per episode low.

### Stage 1 — Fetch
- **Input:** a local video file (preferred) or a YouTube URL.
- **Tool:** `yt-dlp` for URLs, used only for your own tests and for clients who ask you to.
- **Output:** `source.mp4`, `audio.wav` (16 kHz mono, for transcription), `meta.json` (title, duration, channel, and YouTube's "Most replayed" heatmap when it's available).

### Stage 2 — Transcribe
- **Tool:** `faster-whisper`. It runs locally and is free; it works on CPU and is faster with an NVIDIA GPU.
- **Must have:** **word-level timestamps**. They're needed for precise cuts and word-by-word captions.
- **Output:** `transcript.json` with segments, and the words inside each segment with start and end times.

### Stage 3 — Find candidates (cheap)
Goal: about 20 possible moments, 20–60 seconds each.

Signals to combine (start with the first two, add the rest later):
1. **AI proposal pass:** split the transcript into ~10-minute blocks and ask a cheap model (Claude Haiku) for 3–5 moments per block, with timestamps and a one-line reason.
2. **"Most replayed" peaks** from the YouTube heatmap, when available. These are strong evidence of moments viewers rewatch.
3. **Audio energy spikes** (loudness, laughter).
4. **Text signals:** strong opinions, numbers, stories ("so one day…"), questions followed by answers, controversy.

**Boundary snapping:** move each start to the beginning of a sentence and each end to the end of a sentence, using word timestamps, with about 0.2s padding so clips don't start or end mid-word.

**Output:** `candidates.json`.

### Stage 4 — Score (Claude)
- **Model:** Claude Sonnet for quality scoring (or Haiku while testing to save money).
- Claude gets: the candidate's transcript text, the client's rules file, and (later) the niche patterns from the clip brain.
- Claude returns **structured JSON** for each candidate:

```json
{
  "candidate_id": "c07",
  "score": 8,
  "hook_line": "Most people think X. They're wrong.",
  "suggested_title": "Why everyone gets X wrong",
  "start": 1834.20,
  "end": 1879.65,
  "verdict": "keep",
  "reason": "Strong contrarian opening line, complete story with a payoff, no outside context needed",
  "reject_reason": null
}
```

- Fixed **reject reason categories** (copied from the reference build) so you can count them:
  `no_payoff`, `needs_context`, `weak_hook`, `too_long_or_rambling`, `off_brand`, `below_threshold`.
- Keep the top N by score (default 5–10, set in config).
- **Output:** `scored.json`. Every rejection reason is logged, because those logs show you how to improve the prompt.

### Stage 5 — Render
All with **ffmpeg**:
1. **Cut** the clip from `source.mp4` using the snapped start and end times.
2. **Make it vertical (9:16, 1080×1920):**
   - v1: center crop (simple, works for single-camera talking heads).
   - v2: **face tracking.** Detect faces with MediaPipe or OpenCV and keep the active speaker centered. Needed for two-person podcasts.
3. **Captions:** generate an `.ass` subtitle file from the word timestamps (2–4 words on screen at a time, current word highlighted), then burn it in with ffmpeg.
4. Optional later: the hook line as a text overlay for the first 2 seconds, a progress bar, or the client's logo.

**Output:** `clips/c07.mp4` plus a matching `clips/c07.json` (title, score, reason).

### Stage 6 — Review
- Generates `report.html`: one card per clip with the video player, score, hook, reason, and transcript.
- You watch and approve or reject.
- v1: a simple HTML page. Later: a small web dashboard with approve and reject buttons.
- **Human review always stays in.** Even fully automated systems end with a review queue, and clients pay for quality.

---

## 5. Tech stack

| Job | Tool | Why | Cost |
|---|---|---|---|
| Language | Python 3.11+ | Best ecosystem for AI and video | Free |
| Download | yt-dlp | Standard, reliable | Free |
| Video processing | ffmpeg | Cuts, crops, captions | Free |
| Transcription | faster-whisper | Local, word timestamps | Free |
| AI scoring | Claude API (Haiku + Sonnet) | Structured JSON output, strong judgment | Pay per use |
| Face tracking (v2) | MediaPipe / OpenCV | Speaker-centered crop | Free |
| Config | YAML files | Easy to edit per client | Free |
| Storage | v1: JSON files per run → v3: SQLite → later Supabase if needed | Start simple | Free |
| Dashboard (v3) | FastAPI + simple HTML, or Streamlit | Approve/reject UI | Free |
| Fancy rendering (optional, v3+) | Remotion (React) | Animated captions and templates | Free for individuals; check license |
| Builder | Claude Code (your Max plan) | Implements one brief at a time | Included |

**Cost per episode:** Whisper is local and free; Claude calls should be roughly cents to under a dollar per 1-hour episode. **Log the actual cost of every run** (`costs.json`), because it's useful for pricing and impressive in the portfolio. Check current Claude API prices before estimating for clients.

**Your machine:** works on your Windows PC. Install ffmpeg and add it to PATH. An NVIDIA GPU makes Whisper much faster but isn't required.

---

## 6. Project structure

```
clip-machine/
├── README.md                 # portfolio-quality: what, why, diagram, demo, results
├── requirements.txt
├── .env.example              # ANTHROPIC_API_KEY=...
├── config/
│   └── default.yaml          # clip length min/max, clips to keep, models, caption style
├── clients/
│   └── example-client/
│       └── rules.yaml        # niche, tone, banned topics, preferred length, caption style
├── prompts/
│   ├── find_moments.md       # stage 3 prompt (cheap model)
│   └── score_clip.md         # stage 4 prompt (scoring model)
├── src/clipmachine/
│   ├── cli.py                # entry point
│   ├── pipeline.py           # runs stages in order, tracks status, resumes
│   ├── models.py             # data shapes (pydantic): Word, Segment, Candidate, ScoredClip
│   ├── stages/
│   │   ├── fetch.py
│   │   ├── transcribe.py
│   │   ├── candidates.py
│   │   ├── score.py
│   │   ├── render.py
│   │   └── report.py
│   └── utils/
│       ├── ffmpeg.py         # wrappers around ffmpeg commands
│       ├── captions.py       # word timestamps → .ass file
│       ├── claude.py         # API calls, retries, JSON parsing, cost logging
│       └── snapping.py       # move cut points to sentence boundaries
├── runs/                     # one folder per processed episode (git-ignored)
│   └── 2026-10-12_episode-slug/
│       ├── source.mp4  audio.wav  meta.json
│       ├── transcript.json  candidates.json  scored.json
│       ├── clips/
│       ├── report.html
│       ├── run.json          # status of each stage (done/failed + timestamps)
│       └── costs.json
└── tests/
```

**Commands it should support:**
```
python -m clipmachine run <file-or-url> --client example-client
python -m clipmachine run <run-folder> --from-stage render   # rerun from a stage
python -m clipmachine report <run-folder>
```

### Example config files

`config/default.yaml`
```yaml
clip:
  min_seconds: 20
  max_seconds: 60
  keep_top: 8
  candidates_target: 20
models:
  find: claude-haiku       # cheap pass (use the current model ID)
  score: claude-sonnet     # quality pass (use the current model ID)
transcribe:
  model: small             # faster-whisper size: base / small / medium
video:
  width: 1080
  height: 1920
  crop: center             # v2: face
captions:
  words_per_line: 3
  font: Montserrat
  highlight_color: "#FFD400"
```

`clients/example-client/rules.yaml`
```yaml
name: Example Fitness Podcast
niche: fitness / nutrition
audience: men 20-35 trying to build muscle
tone: direct, practical, a bit funny
prefer:
  - specific actionable tips
  - myth-busting
  - personal transformation stories
avoid:
  - supplement sponsor reads
  - inside jokes that need context
clip_length: { min: 25, max: 50 }
```

---

## 7. The scoring prompt (first draft)

Save as `prompts/score_clip.md`. You'll improve it using the rejection logs.

```
You are a short-form video editor who picks clips that perform on
YouTube Shorts, TikTok and Reels.

CLIENT RULES:
{rules_yaml}

NICHE PATTERNS THAT PERFORM (may be empty in v1):
{patterns}

CANDIDATE CLIP (timestamps in seconds):
{start} - {end}
TRANSCRIPT:
{text}

Judge it as a viewer scrolling with no context:
1. Hook: do the first ~3 seconds give a reason to keep watching?
2. Standalone: does it make sense without the rest of the episode?
3. Payoff: is there a clear point, punchline, insight, or emotional peak?
4. Fit: does it match the client's niche, tone and rules?
5. Length: is it tight, with no rambling?

Return ONLY JSON:
{
  "score": 1-10,
  "verdict": "keep" | "reject",
  "hook_line": "...",
  "suggested_title": "...",
  "start": number, "end": number,   // you may tighten the boundaries
  "reason": "...",
  "reject_reason": null | "no_payoff" | "needs_context" | "weak_hook" |
                   "too_long_or_rambling" | "off_brand" | "below_threshold"
}
```

---

## 8. The clip brain (what makes it special)

Built in phase 3, after v1 works and ideally after you have a client.

**Idea:** learn which kinds of moments go viral in a niche, and feed those patterns into the scoring prompt.

**Step by step:**
1. **Collect examples by hand first:** in a spreadsheet, record 30–50 short clips from big accounts in the niche: link, views, the hook line, the length, and the source episode with its timestamp.
2. **Match clips to their source:** take the full-episode transcript and have Claude find where each clip came from.
3. **Analyze:** give Claude the top 10 and bottom 10 clips and ask: what do the winners do that the losers don't (hook types, topics, length, structure)?
4. **Save the patterns** to `clients/<client>/patterns.md` (or a niche file). The scoring prompt reads it.
5. **Close the loop:** once a client posts your clips, record their real views. Over time the system learns from *that client's* own results. This is the long-term edge.

**Automating collection (later):** scraping tools like Apify can collect clips and stats in bulk, but scraping Instagram and TikTok breaks their terms. Keep it small, for research only, or stick to manual and official data.

---

## 9. Build roadmap

Workflow: **you decide → Claude chat writes specs and briefs and reviews → Claude Code implements one brief at a time.** Commit to git after every working step.

### Phase 0 — Validate demand (start now, in parallel with Phase 1)
Don't wait for the software to see whether anyone will pay.
- [ ] Pick **one niche** you understand (fitness, business, college life, Ukrainian creators…)
- [ ] Build a list of 30–50 targets: YouTube podcasts in that niche with full episodes but few or no Shorts
- [ ] Make 2–3 sample clips for 10 of them (by hand, or with OpusClip/CapCut)
- [ ] Send personal pitches (template in section 11)
- [ ] Track everything in a spreadsheet: name, link, size, date contacted, reply, status
- **Signal:** any positive reply from about 30 pitches means demand is real. Zero replies means fix the niche or pitch, not the code.

### Phase 1 — v1 MVP (about 1–2 weeks)
Goal: **one episode in → 5 reviewed clips out.**
- [ ] Brief 1: project skeleton, config loading, CLI, run folders, `run.json` status tracking
- [ ] Brief 2: Fetch stage (local file + yt-dlp URL, audio extraction)
- [ ] Brief 3: Transcribe stage (faster-whisper, word timestamps)
- [ ] Brief 4: Candidates stage (Haiku proposal pass + sentence snapping)
- [ ] Brief 5: Score stage (Sonnet, JSON output, reject reasons, cost logging)
- [ ] Brief 6: Render stage (cut + center crop + burned-in captions)
- [ ] Brief 7: Report stage (HTML review page)
- [ ] Test on 3 different episodes; write down what's bad
- **Done when:** you'd be comfortable sending these clips to a stranger as a sample.

### Phase 2 — Quality (about 1–2 weeks)
- [ ] Face-tracked vertical crop (two-person podcasts)
- [ ] Nicer captions (styles, highlight colors, emoji off/on per client)
- [ ] Client rules files wired into scoring
- [ ] Hook text overlay for the first 2 seconds
- [ ] "Most replayed" heatmap and audio-energy signals in candidates
- [ ] Rejection-reason summary in the report (counts per reason)

### Phase 3 — Clip brain (about 2–3 weeks)
- [ ] Manual dataset of 30–50 niche clips with views
- [ ] Clip-to-source matching
- [ ] Pattern analysis → `patterns.md`
- [ ] Patterns used in scoring
- [ ] Feedback loop: record client clip performance

### Phase 4 — Operations (when you have 2+ clients)
- [ ] SQLite (or Supabase) for clients, runs, clips and results
- [ ] Web dashboard: queue, approve/reject, download
- [ ] Batch processing (several episodes overnight)
- [ ] Delivery: a shared folder link per client (Google Drive)
- [ ] Simple invoicing (Stripe or PayPal invoices)

---

## 10. Testing & quality checklist

Before any clip goes to a client:
- [ ] Starts on a complete sentence, no cut-off words
- [ ] Makes sense with zero context
- [ ] The first 3 seconds give a reason to keep watching
- [ ] Captions are correct (check names and numbers; Whisper misspells them)
- [ ] The face isn't cropped out, and the speaker is centered
- [ ] Audio levels are fine
- [ ] Length within the client's range
- [ ] No sponsor reads or off-limits topics

**Numbers to track every run** (great for the portfolio): processing time, cost, candidates found, number kept, reject reasons, and later each client clip's views.

---

## 11. Getting clients

**Ideal target:**
- Posts full episodes on YouTube weekly
- Few or no Shorts (check their Shorts tab)
- Mid-size: roughly 5K–100K subscribers
- In your chosen niche

**Where to find them:** YouTube search in your niche, Apple/Spotify podcast charts (ranks around 50–200 in a category), X/Twitter, podcaster communities, and **locally**: Rowan student groups, Glassboro-area businesses and local podcasters (in person converts much better).

**Pitch template (email works better than DMs; check their YouTube "About" page):**

> Subject: 3 Shorts from your last episode (free)
>
> Hi {name},
>
> I listened to "{episode title}" and noticed you're not posting Shorts from your episodes. That's a lot of reach left on the table.
>
> I made 3 clips from it, ready to post, free to use either way: {link}
>
> I pick moments based on what's actually performing in {niche} right now, not just random cuts. If you like them, I can do 5–10 per episode every week. Happy to start with a cheap 2-week trial.
>
> — Igor

**Rules:**
- Always personal: mention a specific episode or moment.
- Always attach the free sample clips; that's the whole pitch.
- Follow up once after about 5 days, then move on.
- Expect roughly 1–2 clients per 50–100 good pitches at first (an estimate).

**Delivery workflow (once hired):** they send the episode file or link → you run the pipeline → review → upload approved clips to their shared folder with titles → weekly 2-line update with what you posted and the best performer.

**Basic agreement (keep it simple, and get it in writing):** what you deliver, how many clips per week, the price, payment date, that they own the clips, and that you may show them in your portfolio. I'm not a lawyer; for anything larger, get a simple contract template reviewed.

---

## 12. Showing it in your portfolio

- **GitHub repo** with a strong README: the problem, an architecture diagram (section 4), setup steps, and design decisions ("cheap first, expensive last"; "every stage restartable").
- **Demo video** (60–90s): a long episode goes in, the review page appears, and the final clips play.
- **Case study** (1 page): "Processed X hours of video at $Y per episode; Z% of candidates kept; client clips got N views." Real numbers beat buzzwords.
- **Resume bullet example:**
  *Built an AI video-clipping pipeline (Python, Whisper, Claude API, ffmpeg) that turns long-form podcasts into captioned vertical clips; multi-stage cheap-to-expensive filtering kept API cost under $X per hour of video; used for paying clients.*
- **LinkedIn / build-in-public posts** while you build. They double as marketing for the service.

---

## 13. Risks and how to handle them

| Risk | How to handle it |
|---|---|
| No clients reply | Phase 0 tests this early; change niche or pitch, not code |
| Generic tools are cheaper | Sell judgment and results (the clip brain), not cutting |
| Bad clip quality | Human review always; quality checklist; improve the prompt with rejection logs |
| YouTube download terms | Prefer client-supplied files; use yt-dlp only for testing or with client permission |
| Scraping platforms | Manual data first; small-scale research only |
| Cost creep | Cheap-first design, cost logged every run |
| Overload (job, Svitlo, this) | Small briefs, one stage at a time; v1 scope is fixed — no extras until it works |

---

## 14. Glossary (plain English)

- **Pipeline / stage:** an assembly line; each stage is one station doing one job.
- **Transcript with word timestamps:** the text of the video, plus exactly when each word is spoken.
- **Candidate:** a *possible* clip, before it's judged.
- **Structured output / JSON:** the AI answers in a fixed format the code can read, not free text.
- **Snapping:** moving cut points to sentence boundaries so clips don't start mid-word.
- **9:16 / vertical:** phone-screen format used by Shorts, Reels and TikTok.
- **Burned-in captions:** subtitles drawn into the video itself, because most people watch muted.
- **Retainer:** a fixed monthly payment for ongoing work.

---

## 15. Brief 1 for Claude Code (SUPERSEDED)

> Superseded on 2026-10-10 by `docs/brief-01-skeleton.md`. Do not implement the old version (it installed heavy dependencies early, had no pyproject.toml, and used `ANTHROPIC_API_KEY` as the env name).

---

## Next steps (this week)

1. **Pick your niche** — the one decision everything else depends on.
2. **Create the GitHub repo** `clip-machine` and give Claude Code Brief 1.
3. **Start the target list** (Phase 0): 10 creators who post episodes but no Shorts.
4. Come back to the chat with the Brief 1 results, and we'll write Brief 2 (Fetch).
