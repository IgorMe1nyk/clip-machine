# PRODUCT.md — Who it's for and what "done" means (v1)

> **Status:** DRAFT 2026-10-11, for Igor's approval. Labels: [decided] = Igor decided · [proposal] = Claude's recommendation, open until approved.

## 1. People
| Who | What they do in v1 |
|---|---|
| **Client** (a podcaster or YouTuber) | Sends the original episode file. Receives finished clips in a shared folder. **Never sees the review page.** [decided] |
| **Igor** (operator and reviewer) | Runs the pipeline, reviews every clip, rates it, delivers the usable ones. **The only reviewer in v1.** [decided] |
| **Clip Machine** | Finds, judges and renders clips. Never posts or sends anything itself. |

## 2. The flow (one episode)
1. The client sends the episode file. The default input is a file, not a link.
2. Igor runs one command: `uv run python -m clipmachine run <file> --client <name>`.
3. Igor opens `report.html` and watches each kept clip, phone-sized.
4. Igor rates each clip **usable / needs edit / reject** (rubric in MVP.md section 5), with the failed check numbers.
5. Usable clips go to the client's folder, each with its suggested title.
6. Ratings and run numbers go into `docs/EVAL.md`.

## 3. What "done" means for a client [proposal]
- **Per episode:** 5–10 clips rated usable (plan estimate; real numbers come from EVAL.md), each passing every Hard rubric check.
- **Each clip has:** a 1080×1920 MP4 with burned-in captions, and a suggested title.
- **Turnaround:** within 48 hours of receiving the file.
- **Never delivered:** a clip Igor hasn't watched in full.

## 4. Data and retention [proposal]
- Only transcript text leaves the machine (to the Claude API). Video and audio stay local.
- Delete `source.mp4` and `audio.wav` 14 days after delivery. Keep transcripts and scores for the clip brain.
- Unpublished client footage never goes to CapCut or any other cloud editor.

## 5. The review page (`report.html`)
**Its one job:** let Igor decide fast and confidently whether each clip is good enough to send.

### Layout (top to bottom)
```
┌────────────────────────────────────────────────┐
│ Episode name · date · source length            │
│ 3 kept of 7 candidates · $0.02 · 2m 10s        │  ← summary bar
│ Rejected: needs_context 3 · no_payoff 2 · ...  │
├────────────────────────────────────────────────┤
│ #1  c04  score 6 · 8:12–8:39 (27 s) · trimmed  │
│ ┌──────┐  Title: …                            │
│ │video │  Hook: …                             │  ← one card per kept clip,
│ │ 9:16 │  Why kept: …                         │    in rank order
│ │      │  Transcript ▸ (collapsed)            │
│ └──────┘  Rubric reminder: C1 C2 … C9         │
├────────────────────────────────────────────────┤
│ Rejected candidates ▸ (collapsed list)         │  ← id, times, score,
│   c01 0:34–1:04 · 2 · off_brand · reason       │    reason; no video
└────────────────────────────────────────────────┘
```
- **The video is phone-sized** (about 360 px wide), so Igor judges it as a viewer would.
- **The rejected list stays.** Where Igor disagrees with the model is the data that improves the prompt.
- **No approve/reject buttons in v1.** Ratings are typed into chat or EVAL.md. Buttons come with the Phase 4 dashboard.

### States
| State | What the page shows |
|---|---|
| Normal | Summary bar plus clip cards |
| **Zero kept** | "No clips passed the judge." The rejected list opens automatically, with the reject-reason counts |
| **A clip's render is missing or invalid** | That clip's card shows "Render missing: <short reason>" with no player; the other clips are unaffected |
| **Pipeline not finished** | The report isn't built; the CLI says which stage to run |

### Rules
- **Works offline:** one HTML file, no CDN, no internet. Videos are linked by relative path (`clips/c04.mp4`).
- **Escapes all text:** transcript, hook, title and reason are untrusted data and are HTML-escaped.
- **Lives only in the run folder,** which is git-ignored. It's never committed, because it contains transcript text.

## 6. Not in v1
Client-facing pages, logins, approve/reject buttons, automatic delivery to Drive, invoices, multiple episodes per report.
