# COSTS.md — Cost & Dependency Register

> **Status:** APPROVED by Igor 2026-10-10 (Gate 0). v3 (2026-10-10, after Brief 3) adds the measured transcription numbers, the NVIDIA GPU libraries, and local disk use. No new paid dependency.
> **Prices checked:** 2026-10-10, from live pages (sources at the bottom). Re-check at the end of every phase.
> **Labels:** **[fact]** = read on a live page today · **[estimate]** = my math on stated assumptions · **[judgment]** = my recommendation · **[gap]** = could not confirm.

---

## 1. Bottom line

1. **Cash cost to build and run v1: $0/month on top of what you already pay.** [estimate]
   - Everything in the pipeline except the Claude API is free, local, open-source software. [fact]
   - The Claude API is billed separately from Claude subscriptions. [fact]
   - But **Max plans include monthly API credits**: $100/month on Max 5x, $200/month on Max 20x. [fact]
2. **API cost per hour of source video: about $0.17–$0.60.** [estimate, section 5] At that rate the included credits cover hundreds of hours a month.
3. **What happens when the included credits run out:** API requests stop. Nothing is charged to your plan. [fact] Leave it that way: buy no credits and keep auto-reload off. Then your included credits are a hard cap, and you can't get surprise bills.
4. **The real cost of this business is your time, not money.** [judgment] At realistic volume, payment fees cost more than the AI.

---

## 2. Decisions this register needs from you

| ID | Decision | Status |
|---|---|---|
| D-003 | Budget | **Approved 2026-10-10 (Igor):** $0 cash. Only the Max-included API credits; no purchased credits, no auto-reload; $5 Console spend limit as a backup. Revisit when the first client pays. |
| D-004 | Max plan | **Max 5x → $100/month in API credits.** Claimable 7 days after starting the plan [fact]; the claim screen will confirm. |
| D-005 | GitHub repo | **Approved 2026-10-10 (Igor):** public repo. Actions are free on public repos. [fact] |
| D-006 | Test video source | **Open.** Igor will pick a video; it must come from the allowed sources in MVP.md section 4. No YouTube downloads (its terms reportedly forbid it without permission). |

---

## 3. Spend controls (must exist before the first API call)

1. **Console account** at platform.claude.com, with its own organization for this project.
2. **Claim the Max API credits** on claude.ai under Settings → Billing → API credits, then link that Console organization. [fact]
   - You can link only one organization, and changing it means contacting support. [fact]
3. **Don't buy credits, and keep auto-reload off.** Auto-reload triggers from the *purchased* balance, even while included credits remain. [fact]
4. **Set a Console spend limit** anyway, as a second lock: Settings → Billing → Spend limits. [fact] Recommended: $5/month. [judgment]
5. **Budget guard in code** (arrives in Brief 5):
   - Compute the cost of every call from the token counts and a price table in config.
   - Write `costs.json` for each run.
   - Abort a run that would exceed its cap. Proposed caps: $1 per run, $5 per day. [judgment]
6. **Never retry these errors** (they mean money or limits, not a temporary hiccup) [fact]:
   - HTTP 400: "You have reached your specified API usage limits…" (your own spend limit)
   - HTTP 429 with `error_code: "enforced_spend_limit_reached"` and no `retry-after` header (Anthropic's monthly cap)
   - "Your credit balance is too low to access the Anthropic API…"

   Retry with backoff only on HTTP 429 *with* a `retry-after` header, and on temporary server errors.
7. **Key handling:** the key lives only in `.env` as `CLIPMACHINE_ANTHROPIC_API_KEY`. It is never put in your shell environment.
   - Claude Code uses an exported `ANTHROPIC_API_KEY` ahead of your subscription once you approve it. [fact]
   - The included API credits don't cover Claude Code anyway. [fact]

> **Learn — two different bills.** Your Max subscription pays for chatting and for Claude Code. The API is a separate meter for *your own code* calling Claude. Max happens to come with a monthly API allowance attached. Think of it as a prepaid card that refills monthly and stops working when empty, rather than a credit card.

---

## 4. The register

Column key:
- **Free tier ends?** What happens when you outgrow the free option.
- **Alt.** A free or local alternative.
- **$/hr video:** cost per hour of source video processed.

### 4.1 Build tools

| Item | Purpose | Price / limits | License, commercial use | Free tier ends? | Alt. | $/hr video |
|---|---|---|---|---|---|---|
| Claude Code (your Max plan) | Implements the briefs | Max 5x $100/mo, Max 20x $200/mo. Already paid, so no new cost. [fact] | Subscription terms | n/a | n/a | $0 extra |
| Python 3.11 | Language | Free [fact] | PSF license, commercial OK [fact] | n/a | — | $0 |
| uv | Virtual env, lock file, installs | Free [fact] | MIT or Apache-2.0 [fact] | n/a | pip + pip-tools | $0 |
| pydantic, PyYAML | Data models, config | Free [fact] | MIT [fact] | n/a | — | $0 |
| ruff, mypy, pytest, pip-audit | Lint, types, tests, audit | Free [fact] | MIT (pip-audit: Apache-2.0) [fact] | n/a | — | $0 |
| Git + GitHub Free | Version control, portfolio | Unlimited public and private repos. Actions: free on public repos; 2,000 min/mo on private (Linux $0.006/min after that). [fact] | Service terms | Private CI over 2,000 min/mo gets billed; we're nowhere near it [estimate] | Public repo | $0 |

### 4.2 Pipeline (runs on your PC)

| Item | Purpose | Price / limits | License, commercial use | Free tier ends? | Alt. | $/hr video |
|---|---|---|---|---|---|---|
| FFmpeg (gyan.dev Windows build) | Cut, crop, captions, audio | Free [fact] | gyan.dev builds are GPLv3. The GPL only matters if you *give copies* of FFmpeg to others; using it on your own PC and delivering videos is fine. [fact, GPL FAQ] | n/a | BtbN LGPL build | $0 |
| faster-whisper + CTranslate2 | Local transcription with word timestamps | Free [fact] | MIT [fact] | n/a | — | $0 cash. Time depends on hardware: **measure it**. |
| Whisper model weights (downloaded from Hugging Face) | Speech model | Free download [fact] | MIT, including the weights [fact] | n/a | — | $0 |
| NVIDIA cuBLAS 12 + cuDNN 9 + nvrtc (`gpu` dependency group, Windows wheels) | GPU speed-up for Whisper on the GTX 1660 Ti | Free; about 1.3 GB download [fact, from Brief 3] | **NVIDIA proprietary license** (package metadata). Local use only; optional group; never bundled or redistributed | n/a | CPU mode (measured 3.9× realtime) | $0 |
| Whisper model files in `<repo>/models/` | Speech models | Free. Disk: tiny 75 MB, small 464 MB, turbo 1.6 GB [measured] | MIT | n/a | — | $0 |
| Claude API: Haiku 5.5 `claude-haiku-5-5` | Stage 3: cheap candidate pass | $0.10 input / $0.50 output per million tokens, for prompts up to 100K tokens [fact] | API commercial terms | Credits run out → requests stop, no charge [fact] | Skip this stage; use Sonnet only (section 5) | ~$0.004–0.01 [estimate] |
| Claude API: Sonnet 5.5 `claude-sonnet-5-5` | Stage 4: scoring | $2 input / $10 output per million tokens. Batch API is 50% off. [fact] | API commercial terms | Same as above | Haiku for testing | ~$0.16–0.56 [estimate] |
| anthropic Python SDK 1.13.0 | API client; we call `messages.create` with `output_config.format` and the SDK's `transform_schema()`, then validate with Pydantic | Free | **MIT** [fact, package metadata]. New transitive packages: docstring-parser (MIT), jiter (MIT), sniffio (MIT or Apache-2.0); httpx2, anyio and typing-extensions were already installed | n/a | — | $0 |
| Montserrat font | Captions | Free [fact] | SIL OFL 1.1. Burning it into video needs no permission or credit. [fact] | n/a | Any OFL font | $0 |

**Model IDs:** read from the live models page on 2026-10-10. These IDs are pinned snapshots, not moving aliases. [fact] The plan's `claude-haiku` and `claude-sonnet` are placeholders; config will use the exact IDs.

### 4.3 Later phases (parked; listed so nothing surprises you)

| Item | Phase | Purpose | Price / limits | License | Free tier ends? | Alt. |
|---|---|---|---|---|---|---|
| MediaPipe / OpenCV | 2 | Face-tracked crop | Free [fact] | Apache-2.0 [fact] | n/a | — |
| yt-dlp | Optional | URL download | Free [fact] | Unlicense. Its Windows `.exe` bundles GPL code; the pip version doesn't. [fact] | n/a | **Client-sent files (default)** |
| Remotion | 3+ | Animated captions | Free for individuals and companies of 1–3 people. Company license after that: Creators $25/seat/mo; Automators $0.01/render with a $100/mo minimum. [fact] | Remotion license | You must pay once the business has 4+ people [fact] | FFmpeg `.ass` captions (our default) |
| SQLite | 4 | Clients, runs, results | Free (built into Python) | Public domain [judgment: well known, not re-checked] | n/a | — |
| Supabase | 4, if ever | Hosted database | Free: 500 MB DB, 1 GB storage, max 2 projects; **pauses after 1 week inactive**. Pro from $25/mo. [fact] | Service terms | Paused projects, size limits | SQLite (recommended) |
| FastAPI / Streamlit dashboard | 4 | Approve and reject UI | Free (local) | Open source (licenses to check in Phase 4) [gap] | n/a | Static HTML report (v1) |
| Hosting | — | None needed through Phase 4 (everything runs locally) | $0 | — | — | — |
| Google Drive | 4 | Client delivery | 15 GB free per account (shared with Gmail and Photos). Google One 100 GB: $1.99 (billing period not shown on page). [fact + gap] | Service terms | Upload fails when full | Delete delivered clips after a set number of days |
| Apify | 3, research only | Scraping clip stats | $5 free usage/mo; Starter $19/mo [fact] | Service terms; **scraping Instagram/TikTok breaks their terms** (plan section 8) | — | **Manual spreadsheet (recommended)** |

### 4.4 Business (Phase 0 and getting paid)

| Item | Purpose | Price / limits | Notes |
|---|---|---|---|
| OpusClip free | Hand-made sample clips | 60 credits/mo, watermark, no editing, exports expire after 3 days. Starter $15/mo removes the watermark. [fact] | What a credit buys and the commercial-use terms: [gap]. Watermarked samples look amateur. [judgment] |
| CapCut free | Hand editing | Free | **Uploading footage gives CapCut a broad, perpetual, sublicensable license to it.** [fact] Never upload client or unpublished footage. Its stock music is mostly personal-use only. [fact] |
| Our own pipeline | Samples | $0 | The best sample tool once the walking skeleton works. [judgment] |
| Stripe Invoicing | Getting paid | Card: 2.9% + 30¢, plus 0.4% per paid invoice (Starter plan). [fact] A $400 invoice costs about $13.50. [estimate] | — |
| PayPal invoice | Getting paid | Card 2.99% + $0.49; PayPal balance 3.49% + $0.49; bank (ACH) 1% capped at $10. [fact] On $400 that's about $12.45 by card or **$4.00 by ACH**. [estimate] | Ask clients to pay by bank. |
| Contract review (lawyer) | Before the first paying client | Not priced [gap] | Flagged in the legal gate; a simple template is fine to start. |

---

## 5. Unit economics

### Cost per hour of source video [estimate]

**Assumptions** (to be replaced by real measurements from `costs.json`):
- About 150 spoken words per minute, so ~9,000 words per hour.
- About 20,000 input tokens for the transcript including timestamps. The current models' tokenizer produces ~30% more tokens than older ones. [fact]
- 20 candidates scored, about 2,000 input and 400 output tokens each.

| Stage | Tokens | Cost |
|---|---|---|
| 3. Haiku 5.5 candidate pass | ~26K in / ~4K out | ≈ $0.004 (≈ $0.01 if the model thinks more) |
| 4. Sonnet 5.5 scoring | ~40K in / ~8K out | ≈ $0.16 |
| 4. Same, if Sonnet's default high effort adds ~2K thinking tokens per candidate | +40K out | ≈ $0.56 |
| **Total** | | **≈ $0.17–$0.60 per hour** |

**Comparison point:** one Sonnet call over the whole transcript (≈20K in, 5K out) would cost about $0.09.
- So the two-model design is **not** clearly cheaper at this scale. Its value would be better per-candidate judgment, not saving money. [judgment]
- Per your rule, we measure first. Both model choices stay in config; we decide after real runs.

### Measured so far (real runs, 2026-10-10)

Measured on one 15.4-minute, two-speaker interview with these settings: GTX 1660 Ti, `small`, `int8_float16`.

| Stage | Time | Cash |
|---|---|---|
| Fetch | 0.3–0.8 s | $0 |
| Transcribe, GPU (D-011) | 48–55 s (≈17–19× realtime) | $0 |
| Transcribe, CPU fallback | 173–236 s (≈4–5× realtime) | $0 |
| Candidates, Haiku 5.5 (Brief 4; 2 calls, 8,810 in / 830 out tokens) | about 7 s | $0.0013 (worst-case estimate $0.0030) |

Local disk used:
- Models: 2.1 GB
- uv package cache: 2.4 GB (in the user folder)
- Each run folder: roughly 75 MB per 15 min of 640×360 video

Stage 3 measured: $0.0013 for 15.4 minutes, about $0.005 per hour of video (the estimate was $0.004–0.01). Stage 4 (Sonnet) is still an estimate until Brief 5.

### Per client per month [estimate, using the plan's own estimates]

- **Volume:** a "small" client is ~20 clips/month. Assume about 4 one-hour episodes.
- **Cash cost:**
  - API: about $0.70–$2.40 (covered by Max credits)
  - Payment fees: $4–$14.50
  - Drive: $0
  - **Total: about $5–$17 per client per month.**
- **Revenue (plan estimate):** $300–$500/month.
- **Your time (plan estimate):** 3–6 hours/month.
- **If those estimates hold,** money isn't the constraint. **Demand and your hours are.**

---

## 6. Data and privacy facts that affect cost decisions

- **What leaves your machine:** only transcript text goes to the API. Video and audio never do. [design rule]
- **Training:** API inputs and outputs aren't used for training by default. [fact]
- **Retention:** API inputs and outputs are deleted within 30 days, with exceptions such as policy enforcement and legal requirements. Content flagged by safety systems can be kept up to 2 years. [fact]
- **Local retention** (decide in PRODUCT.md): proposed default is to delete `source.mp4` and `audio.wav` 14 days after delivery, and keep transcripts and scores for the clip brain. [judgment]

---

## 7. Re-check triggers

Re-open this file and tell Igor before continuing if:
- a new paid dependency appears,
- real cost per hour exceeds **$1**,
- included API credits get more than 50% used in a month, or
- any price above changes at a phase-end re-check.

---

## Sources (opened 2026-10-10)

**Anthropic**
- [Models overview](https://platform.claude.com/docs/en/about-claude/models/overview) · [Model IDs](https://platform.claude.com/docs/en/about-claude/models/model-ids-and-versions) · [API pricing](https://platform.claude.com/docs/en/about-claude/pricing) · [claude.com/pricing](https://claude.com/pricing)
- [API credits for subscribers](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers) · [Subscription vs API](https://support.claude.com/en/articles/9876003) · [Max plan](https://support.claude.com/en/articles/11049741) · [Prepaid credits](https://support.claude.com/en/articles/8977456)
- [Rate limits and spend limits](https://platform.claude.com/docs/en/api/rate-limits) · [Usage tiers](https://support.claude.com/en/articles/10366389) · [Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- [API retention](https://privacy.claude.com/en/articles/7996866) · [Training on API data](https://privacy.claude.com/en/articles/7996868) · [Claude Code authentication](https://code.claude.com/docs/en/authentication.md)

**Open-source licenses**
- [faster-whisper](https://raw.githubusercontent.com/SYSTRAN/faster-whisper/master/LICENSE) · [faster-whisper README](https://raw.githubusercontent.com/SYSTRAN/faster-whisper/master/README.md) · [CTranslate2](https://raw.githubusercontent.com/OpenNMT/CTranslate2/master/LICENSE) · [Whisper README](https://raw.githubusercontent.com/openai/whisper/main/README.md) · [HF faster-whisper-small](https://huggingface.co/api/models/Systran/faster-whisper-small)
- [FFmpeg legal](https://ffmpeg.org/legal.html) · [gyan.dev builds](https://www.gyan.dev/ffmpeg/builds/) · [GPL FAQ](https://www.gnu.org/licenses/gpl-faq.html#InternalDistribution) · [yt-dlp license](https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/LICENSE)
- [MediaPipe](https://raw.githubusercontent.com/google-ai-edge/mediapipe/master/LICENSE) · [OpenCV](https://opencv.org/license/) · [uv](https://raw.githubusercontent.com/astral-sh/uv/main/LICENSE-MIT) · [pydantic](https://raw.githubusercontent.com/pydantic/pydantic/main/LICENSE) · [Montserrat OFL](https://raw.githubusercontent.com/google/fonts/main/ofl/montserrat/OFL.txt) · [OFL FAQ](https://openfontlicense.org/ofl-faq/)
- [Remotion license](https://raw.githubusercontent.com/remotion-dev/remotion/main/LICENSE.md) · [Remotion pricing](https://www.remotion.dev/docs/license/pricing) · [NVIDIA CUDA on WSL](https://docs.nvidia.com/cuda/wsl-user-guide/index.html) · [Microsoft WSL GPU](https://learn.microsoft.com/en-us/windows/wsl/tutorials/gpu-compute) · [PSF license](https://docs.python.org/3/license.html)

**Services**
- [GitHub pricing](https://github.com/pricing) · [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [Stripe pricing](https://stripe.com/pricing) · [Stripe Invoicing](https://stripe.com/invoicing/pricing) · [PayPal fees](https://www.paypal.com/us/business/paypal-business-fees)
- [Google One plans](https://one.google.com/about/plans) · [OpusClip pricing](https://www.opus.pro/pricing) · [CapCut ToS](https://www.capcut.com/clause/terms-of-service) · [CapCut materials license](https://www.capcut.com/clause/material-license-agreement?lang=en)
- [Supabase pricing](https://supabase.com/pricing) · [Apify pricing](https://apify.com/pricing)

**Platform terms and test-video sources**
- [YouTube Terms](https://www.youtube.com/t/terms): couldn't be opened with the normal fetch tool, so treat it as reported; please read the "Permissions and Restrictions" section yourself.
- [TED usage policy](https://www.ted.com/about/our-organization/our-policies-terms/ted-talks-usage-policy) · [Wikimedia Commons licensing](https://commons.wikimedia.org/wiki/Commons:Licensing) · [Internet Archive rights](https://help.archive.org/help/rights/)

**Gaps still open:** Internet Archive Terms of Use text; Google One billing period; OpusClip credit value and commercial terms; Console minimum credit purchase (moot if we buy none); FastAPI/Streamlit licenses (the anthropic SDK is MIT, checked in Brief 4).
