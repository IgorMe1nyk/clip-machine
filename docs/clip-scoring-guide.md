# Clip Scoring Guide — what makes a short clip perform (v1.1)

> **Written:** 2026-10-11. v1 was a focused pass; **v1.1 adds a primary-source pass**: claims checked against the platforms' own pages and one peer-reviewed study, with two corrections (marked "Corrected"). For `prompts/score_clip.md` and `prompts/find_moments.md`.
> **Labels:** [primary] from the platform's own page or a peer-reviewed paper, opened directly · [reported] official statements known only through secondary reports · [industry] creator/vendor blogs, not verified (vendors sell tools, so treat numbers as rough) · [judgment] Claude's recommendation for our scorer.
> **Scope:** generic, cross-platform principles. The niche-specific "clip brain" (real clips + views from Igor's niche, plus client results) is Phase 3 and will refine or override this.

## 1. What the platforms actually reward

| Platform | What it says | Label |
|---|---|---|
| **YouTube Shorts** | YouTube's own Analytics help pages define a Shorts metric called **"Viewed (vs. swiped away)"**: the percentage of times viewers watched a Short instead of swiping past it, alongside **average percentage viewed**. | [primary] YouTube Help |
| YouTube Shorts | That this ratio drives **recommendations**, and the advice to **prioritise storytelling over a target length**, come from an interview with YouTube's Shorts product lead. | [reported] SEJ |
| YouTube Shorts | Shorts can be **up to 3 minutes** for square or vertical video (since Oct 15, 2024; rollout completed Jan 2025). | [reported], consistent across many outlets |
| **TikTok** | For You ranks videos on **user interactions** (likes, shares, follows, comments, content created), **video information** (captions, sounds, hashtags) and **device/account settings**, the last with **lower weight**. A **strong signal such as finishing a longer video from beginning to end** gets more weight than a weak one. **Follower count and past hits are not direct factors.** | [primary] TikTok newsroom, June 18, 2020 |
| **Instagram Reels** | Instagram's own ranking post says its most important **Reels predictions** are how likely you are to **reshare** a reel, **watch it all the way through**, **like** it, and **go to its audio page**. It also surveys people on whether a reel was **"worth their time"**. | [primary] Instagram, Mosseri, May 31, 2023 |
| Instagram Reels | **Less visible:** low-resolution or watermarked reels, muted reels, reels with borders, reels that are mostly text, and reels already posted on Instagram. | [primary] same post |
| Instagram Reels | **Corrected:** v1 called "sends per reach is the top signal for non-followers" a fact. It comes from Mosseri's **video statements (Jan 2025)**, known only through secondary reports, and **no written Instagram document ranks sends first**. The written post does put **resharing first** among Reels predictions, so the direction holds. | [reported] |
| All three | A new video is shown to a **small test audience first**; distribution grows only if that group watches and engages. | [industry] |

**Primary research [primary, ECCV 2024, abstract and supplementary snippets only]:** a study of **90,000 real short videos from Snapchat Spotlight** found that classic **video-quality scores do not strongly correlate with engagement**; engagement is better predicted from the **content itself** (visuals, music, text). One of its two engagement metrics captures whether viewers **keep watching past the first ~5 seconds**. Takeaway: picture quality is necessary but not sufficient; **the moment matters more**, and the opening is measurable.

**Corrected [v1 → v1.1]:** v1 said "watch time is TikTok's most important signal" as a fact. TikTok's own post doesn't rank signals that way; that claim comes from **WSJ reporting** [reported]. TikTok's post does confirm that **watching to the end is weighted strongly** [primary].

**Bottom line [judgment]:** every platform asks the same three questions:
1. **Stop:** did the opening make people stay instead of swiping?
2. **Hold:** did they watch to the end, or rewatch it?
3. **Spread:** did they share it (especially by DM)?

## 2. Sound-off viewing and captions
- In a 2019 survey of about 5,600 US adults (Verizon Media and Publicis Media), **69% watch video with the sound off in public**, and **80% say they're more likely to finish a video when captions are available**. [fact, primary study]
- So the **words on screen carry the clip.** A moment that only works with tone of voice or visuals (laughter without a line, a reaction shot) is weaker for us. [judgment]
- We already burn in captions; caption **accuracy on names** matters (Whisper's "Claude → Claw"). [fact, from our own run]

## 3. The opening (the "stop")
- Creator and vendor data all agree the swipe decision happens in roughly the **first 2–3 seconds**, and that most loss happens there. Exact percentages vary by source and are vendor-reported. [industry]
- **What stops a swipe** in a talking-head podcast clip (no fancy visuals):
  - a **bold claim or contrarian line** ("Most people get X wrong.")
  - a **specific number or result** ("We lost $40k in one week.")
  - a **direct question** the viewer wants answered
  - **conflict or tension** (disagreement, a mistake, a confession)
  - **"you"-framing**: directly useful to the viewer
- **What kills it:** filler and throat-clearing ("so, um, yeah", "as I said", "that's a great question"), greetings, starting mid-thought, and needing earlier context to understand the first line. [industry + judgment]

## 4. The middle and the end (the "hold")
- **One idea per clip.** A tangent mid-clip is where viewers leave. [industry]
- **The payoff must land inside the clip:** the punchline, the answer, the lesson. Cutting before the conclusion wastes the hook. Our own evidence: Igor flagged 2 of 5 near-miss clips for **ending before the thought finished**. [fact, from our run]
- **Length:** there is **no magic number**. The right length is the **shortest version that delivers the idea**. Short clips that get rewatched can score very high on watch time. [industry; consistent with YouTube's "storytelling over length" advice]
- **The ending:** end on the strongest line (the payoff or a punchline), not on a trailing "and so…" into the next topic. [judgment]

## 5. Sharing (the "spread")
- Instagram's own ranking post lists **likelihood to reshare** first among its Reels predictions [primary]; Mosseri's later video statements single out **DM sends** for reaching new people [reported].
- **The test [judgment]:** *"Would a viewer send this to a specific friend?"* Clips get sent when they are:
  - **useful** (a tip someone needs)
  - **surprising** (a fact that changes your mind)
  - **relatable** ("this is so us")
  - **quotable** (one line worth repeating)
  - **debate-starting** (a strong opinion people will argue with)

## 6. Podcast clip types that tend to work [industry + judgment]
1. **Contrarian take:** "Unpopular opinion: …" plus the reasoning
2. **Specific result or number:** a concrete outcome with a short explanation
3. **Story with a twist:** setup → turn → lesson, under a minute
4. **Myth-busting:** "Everyone thinks X. Actually Y."
5. **Practical how-to:** a 3-step answer to a common question
6. **Confession or emotional peak:** a mistake, a fear, a turning point
7. **Funny exchange:** only if it's funny **as text** (sound-off test)

## 7. Rules for our scorer (to fold into `score_clip.md`) [judgment]
Keep the existing 5 checks, but make them concrete:

1. **Hook (0–3):** the first sentence alone makes a stranger want the next one. Bold claim, number, question, conflict or "you"-framing = 2–3. A neutral statement = 1. Filler, a greeting or a mid-thought start = 0 (**repair first:** move the start to the setup sentence).
2. **Standalone (0–2):** a stranger understands it with no earlier context. Pronouns like "this" or "he" with no referent inside the clip lower this; **repair by extending the start** before rejecting.
3. **Payoff (0–2):** the point, answer or punchline is **inside the clip**, near the end. If it's just after the end, **extend the end.**
4. **Shareability (0–2):** "Would someone DM this to a friend?" Useful, surprising, relatable, quotable or debate-starting.
5. **Fit (pass/fail):** the client's niche, tone and rules; no sponsor reads; nothing off-limits.
6. **Score = hook + standalone + payoff + shareability + 1** (range 1–10), only if Fit passes.
7. **Sound-off check:** if the moment only works with tone of voice or visuals, deduct a point from Hook or Payoff.
8. **Length:** prefer the **shortest repaired version** that keeps hook → payoff. Never pad.

Add to `find_moments.md`: look for the **7 clip types** in section 6, and **start each moment at its setup and end it at the payoff.**

**Rules for render and delivery** (from Instagram's "less visible" list) [primary → judgment]:
- **Never add a watermark or logo** by default (a client logo only on request, small).
- **No letterbox borders:** future split-screen layouts must fill the 9:16 frame.
- **Warn on low-resolution sources:** flag in the report any source under 1080p tall-equivalent after cropping (our T1 test was 360p → about 5× upscale).
- **Clips stay unmuted;** captions don't replace audio.

## 8. What this guide can't tell us (next steps)
- **Which of these matter most for Igor's niche.** That needs the Phase 3 clip brain: 30–50 real niche clips with view counts, comparing top vs bottom.
- **Real performance.** Only posted clips' views (from permissioned campaigns or clients) can validate the scorer. Log them in EVAL.md once there are any.
- **Calibration against Igor's ratings.** After Brief 9, compare the scorer's verdicts with Igor's on the same clips; disagreements are the best prompt-improvement data.

## Sources (opened 2026-10-11)
**Primary (opened directly)**
- [Understand your YouTube content performance (YouTube Help)](https://support.google.com/youtube/answer/12220281)
- [How TikTok recommends videos #ForYou (TikTok newsroom, June 18, 2020)](https://newsroom.tiktok.com/en-us/how-tiktok-recommends-videos-for-you/)
- [Instagram Ranking Explained (Adam Mosseri, May 31, 2023)](https://about.instagram.com/blog/announcements/instagram-ranking-explained)
- [Delving Deep into Engagement Prediction of Short Videos (ECCV 2024, arXiv)](https://arxiv.org/abs/2410.00289v1) (abstract and supplementary snippets read, not the full paper)

**Platforms (reporting on official statements)**
- [YouTube explains how the Shorts algorithm works (Search Engine Journal)](https://searchenginejournal.com/youtube-explains-how-shorts-algorithm-works/494953)
- [YouTube expands 3-minute Shorts to all users (Social Media Today)](https://www.socialmediatoday.com/news/youtube-expands-3-minute-shorts-to-all-users/736967/)
- [YouTube makes Shorts a lot longer (Thurrott)](https://www.thurrott.com/?p=310746)
- [How TikTok recommends videos #ForYou (TikTok newsroom)](https://newsroom.tiktok.com/en-us/how-tiktok-recommends-videos-for-you/)
- [TikTok tracks watch time (Business Insider, on WSJ reporting)](https://businessinsider.nl/the-secret-factors-that-influence-your-tiktok-algorithm-and-for-you-page-were-just-revealed-the-app-is-tracking-how-long-you-watch-or-hover-over-videos/)
- [How the Instagram algorithm works in 2026 (eClincher, summarising Mosseri)](https://www.eclincher.com/articles/how-the-instagram-algorithm-works-in-2026)

**Captions (primary study, via secondary reports)**
- [Verizon Media survey: 69% watch without sound in public (MarketingCharts)](https://www.marketingcharts.com/digital/video-108656/page/2?et_blog)
- [80% of caption users aren't hearing impaired (Streaming Media)](https://www.streamingmedia.com/Articles/News/Online-Video-News/80-of-Video-Caption-Users-Arent-Hearing-Impaired-Finds-Verizon-131860.aspx)

**Industry (vendor/creator blogs; directional only)**
- [Understanding the YouTube Shorts algorithm (TrueFuture Media)](https://www.truefuturemedia.com/articles/youtube-shorts-algorithm-data-backed-guide)
- [YouTube Shorts retention research (Bytecap)](https://www.bytecap.io/research/youtube-shorts-retention-benchmarks)
- [TikTok algorithm explained (SocialKit)](https://socialk.it/en/blog/tiktok-algorithm-explained)
- [YouTube Shorts hook formulas (OpusClip)](https://www.opus.pro/blog/youtube-shorts-hook-formulas)
