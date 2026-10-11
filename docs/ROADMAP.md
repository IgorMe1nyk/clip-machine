# ROADMAP.md — From working pipeline to professional product

> **Written:** 2026-10-11 00:45 ET. Owner of the plan: Claude (senior engineer). Owner of every decision: Igor.
> **Labels:** [decided] Igor decided · [proposal] needs Igor's yes · [estimate] a guess, not a measurement.
> **Status:** v1, replaces the "next steps" in HANDOFF.md. Update at the end of every phase.

---

## 1. Where we are
- All 6 stages work: fetch → transcribe → candidates → score → render → report. 729 tests passing. API spend about $0.06.
- **Content:** proven only on a poor test video (1 clip kept of 7).
- **Captions:** in sync with a 0.15 s offset (one video).
- **Picture:** fixed center crop. **Fails on two-person video** (C6). This is the biggest gap.
- **Review:** a static `report.html`. No live progress, no buttons.

## 2. What "professional, finished" means [proposal]
On **3 real podcasts** of the target layout:
1. **≥3 usable clips per episode** (every rubric check passes: C1–C9).
2. **The camera keeps the speaker in frame,** follows movement, and cuts between speakers.
3. **No manual editing** of any delivered clip.
4. **Under 10 minutes of Igor's time per episode,** from upload to exported clips, all in the UI, no terminal.
5. **API cost under $0.25 per episode hour** (measured; currently about $0.08 per hour).

## 3. Engineering rules for every step
- **Evidence first:** fix what a real run shows is broken; don't guess.
- **One brief at a time:** plan → Igor approves → small commits → report → Igor reviews and pushes.
- **Quality before UI:** a nice screen around bad clips is worthless.
- **Gates:** a phase isn't done until its gate passes on real video.
- **Free tools only.** A new dependency needs Igor's OK and a COSTS.md row.
- **Privacy:** third-party test videos stay private and local and are deleted after testing (D-012). Public proof only from permissioned creator content.
- **Every phase ends with** a cost re-check, a PROJECT_LOG entry, an updated handoff, and a push with green CI.

## 4. Phase overview
| Phase | Goal | Gate (done when) | Size [estimate] |
|---|---|---|---|
| **A** Baseline on real input | Learn how the pipeline does on a real 35-min podcast | Full run completes; content ratings recorded | tonight |
| **B** Content quality | Enough good moments, correct captions | ≥3 clips pass C1–C5 and C9 on 2 videos | 1–2 sessions |
| **C** Picture | The camera follows the speaker | C6 passes on studio and solo video | 2–3 sessions |
| **D** Polish | Post-ready without touching | MVP finish line met; EVAL row 1 | 1 session |
| **E** UI | Run, watch, review and export in one app | One episode, upload → export, no terminal, <10 min | 3–4 sessions |
| **F** Ops and proof | Repeatable work and public proof | Batch runs; README, demo, case study | ongoing |
| **Business** (parallel) | Clients and permissioned proof | First permissioned clips posted | ongoing |

---

## 5. Detailed steps

### Phase A — Baseline on real input (tonight)
| # | Step | Owner | Done when |
|---|---|---|---|
| A1 | Open `report.html` of the test run; `git push`; CI green | Igor | Green tick |
| A2 | **D-006 update:** private validation on third-party podcasts (D-012); public proof only from permissioned creators | Igor decides → Claude Code logs it | Recorded in DECISIONS.md |
| A3 | **Build a private test set** (outside the repo, never committed): T1 = the 35-min screen-share AI podcast (have); T2 = a two-person **studio** podcast (people at a table, real cameras); T3 = a solo talking head | Igor | 3 files in `C:\Users\15513\Videos\cm-test\` |
| A4 | **Full run on T1:** `uv run python -m clipmachine run "<T1>" --client general`. Watch for: the duration limit in fetch, 2 score calls (>20 candidates), total time and cost. If anything fails: paste the error, don't edit around it | Igor | Run finishes, or the error is reported |
| A5 | Set offset 0.15 in T1's run config; `--from-stage render` | Igor | Re-rendered |
| A6 | **Rate T1** in chat: per kept clip C1–C5, C9 (ignore C6) + usable / needs edit / reject | Igor | Ratings in chat |
| A7 | **Brief 8 plan** (Opus 5.5 High, Plan mode) → review here; **not built tonight** | Claude Code → Claude reviews → Igor approves | Plan approved |
| **Gate A** | We know kept count, content quality and runtime on real input | | |

### Phase B — Content quality (driven by A's evidence)
| # | Step | Owner / model | Done when |
|---|---|---|---|
| B1 | **If T1 keeps <3:** diagnose from the rejections, then Brief 9 (candidate pool or scoring). **Freeze `candidates.json`** when comparing scorer versions (Haiku varies between runs) | Claude writes; Sonnet Medium builds | ≥3 kept on T1 |
| B2 | **Caption offset:** if 0.15 also looks right on T1 or T2, make it the default (tiny brief) | Sonnet Medium | `default.yaml` = 0.15 |
| B3 | **If captions misspell names or jargon (C5):** pass a per-client vocabulary to Whisper (`initial_prompt` in the client rules) | Sonnet Medium | Names correct on a re-run |
| B4 | Run T2 end to end; rate content | Igor | Ratings in chat |
| **Gate B** | ≥3 clips pass C1–C5 and C9 on T1 and T2 | | |

### Phase C — Picture: the camera follows the speaker (Brief 8)
| # | Step | Owner / model | Done when |
|---|---|---|---|
| C1 | Approve the new dependency (MediaPipe, Apache-2.0) and the telemetry finding; record D-014 | Igor | Plan approved |
| C2 | **Part A:** face tracking + active speaker + camera path → `track.json` and a **debug preview** | Sonnet subagent builds; Opus reviews | Tests green |
| C3 | **Checkpoint:** Igor watches the T2 debug preview: right person, no flicker, cuts at speaker changes | Igor | "Right person ≥90% of the time" |
| C4 | **Part B:** moving-crop render (`video.crop: speaker`) | Sonnet subagent; Opus reviews | T2 clips rendered |
| C5 | Rate C6 on T2 and T3; if both pass, make `speaker` the default | Igor | C6 passes |
| C6 | **Later:** split-screen layout for screen-share/PIP podcasts like T1. Only if the target niche needs it (Business step 1 decides) | Brief 10 | — |
| **Gate C** | C6 passes on studio and solo video | | |

### Phase D — Polish to "post as-is"
| # | Step | Owner / model | Done when |
|---|---|---|---|
| D1 | **Audio:** loudness levelling with ffmpeg's built-in `loudnorm` (free) if C7 fails | Sonnet Medium | C7 passes |
| D2 | **Captions:** max characters per line, safe zones (clear of the platform UI), position relative to the face | Sonnet Medium | Looks right on a phone |
| D3 | *Optional:* the hook line as on-screen text for the first 2 s | Sonnet Medium | Igor likes it |
| D4 | **MVP finish line:** one run of T2 with ≥3 usable clips, rated with the full rubric; **EVAL.md row 1** (video named only as "T2, third-party, private") | Igor rates; Claude Code writes | Row committed |
| **Gate D** | Igor would post these clips without touching them | | |

### Phase E — The UI (design first, then build)
| # | Step | Owner / model | Done when |
|---|---|---|---|
| E1 | **Design (PRODUCT.md v2):** screens (Jobs, Job detail with live progress, Clip review, Client page, Export), states (queued, running, failed, done, zero clips), data, and the **stack decision D-015** (proposal: a local FastAPI app with server-rendered pages, SQLite for clients and decisions, light polling for progress; Streamlit is the alternative) | Claude designs; Igor approves | Design approved |
| E2 | **Job runner:** start a run from the UI; the pipeline runs in the background; progress per stage from `run.json`; cancel; errors shown in plain words | Opus plans, Sonnet builds | A run started from the browser finishes |
| E3 | **Clip review:** player, score, reason, transcript; approve/reject; rubric rating; edit the title; notes. Decisions saved (SQLite) and fed into EVAL | Opus plans, Sonnet builds | All of T2 reviewed in the browser |
| E4 | **Clients and export:** a client page (rules, history), export approved clips plus titles into a delivery folder | Sonnet builds | Export folder ready to send |
| **Gate E** | Upload → export without the terminal, in under 10 minutes of Igor's time | | |

### Phase F — Operations and proof (after Gate E)
| # | Step | Done when |
|---|---|---|
| F1 | Queue several episodes overnight; automatic deletion of video and audio 14 days after delivery | Batch of 3 runs unattended |
| F2 | README with an architecture diagram and setup from a clean machine; demo video (permissioned content only) | README reviewed |
| F3 | Case study with real numbers from EVAL.md | Published |

### Business track (Igor, in parallel)
| # | Step | When |
|---|---|---|
| 1 | **Pick the niche.** It decides which layout matters most (studio vs screen-share) | Before C6 |
| 2 | **Join creator clipping campaigns.** Read each campaign's rules; they give permission to clip and post, so they're the source of **public proof** | During Phase C |
| 3 | Target list: 30–50 creators (full episodes, few Shorts, 5K–100K subscribers) | After Gate D |
| 4 | Pitches with permissioned samples | After Gate D |

---

## 6. Risks
| Risk | Mitigation |
|---|---|
| MediaPipe is "Alpha" | Fallback to center crop is always there; pin the version |
| CPU speed for face analysis on long clips | Analyse at ~5 fps on downscaled frames; measure in Part A |
| Haiku candidates vary between runs | Freeze candidates when comparing; judge on several videos |
| Max weekly usage (planning on Opus is costly) | Opus only for plans and reviews; Sonnet builds; keep briefs tight |
| Monthly API credit (current $100 expires Oct 23) | Spend is about $0.06; no risk at this rate |
| Svitlo opens Oct 20 and the job | Work in sessions with safe stops; flag overload early |
| Third-party test footage | Private, local, deleted after testing; never in commits, demos or samples |

## 7. Tonight (until 02:00)
1. A1: push and check the report.
2. A2: decide D-006.
3. A4–A6: run T1, re-render at 0.15, rate.
4. A7: Brief 8 plan on Opus High → review here.
5. 01:15 checkpoint: where we stand. 02:00: stop at a commit.
