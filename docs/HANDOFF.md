# HANDOFF — Clip Machine (read this first)

> **Written:** 2026-10-13 ~02:25 ET, end of session 6 (Mon Oct 12 14:38 → Tue Oct 13 ~03:00). Session 5 was a short planning session (Oct 12 01:33–02:30, briefs 8.3 and R1).
> **Read next:** `ROLE-instructions.md` (how to work with Igor; skip `<first_move>`; don't re-ask PC specs or budget; do ask for today's stop time), then `docs/ROADMAP.md`, `docs/DECISIONS.md`, and the newest `docs/PROJECT_LOG.md` entries.
> **Source of truth:** the repo (`C:\Users\15513\dev\clip-machine`, public as IgorMe1nyk/clip-machine). Repo docs beat any copy here.
> **Public repo rule:** no campaign, creator or show names, transcript text, hooks or frames in commits. Campaign specifics live only in the gitignored `campaigns/<slug>/` folder.

## 1. Where we are
**The product works end to end for paid clipping campaigns.** Igor's first 3 campaign clips are **scheduled for Tue Oct 13**, one per platform, each a different moment:
- TikTok 12:30 PM ET
- Instagram Reels 11:45 AM ET
- YouTube Shorts 1:00 PM ET

All three are marked posted in `used.json`.

**Check first:**
- Did Claude Code finish the R2 merge, push and the full re-render?
- `main` should contain R2/R2.1 and be pushed.
- About 32 unposted clips should be re-rendered with smooth zoom; posted moments are refused.

## 2. What was built in sessions 5–6
| Brief | Result |
|---|---|
| R1 captions + hook | Karaoke box captions (spoken words yellow, wide word gaps), size 96, safe zones (bottom 560, D-022/D-023). Hook: own size 80, **top of frame, first 3 s** (`hook.position: top`, `hook.seconds: 3`) |
| 11 campaign mode | `clipmachine campaign <video> --campaign <slug>`: the campaign's own moment list in, rule-compliant clips out (watermark overlay with screen blend, auto end, start snapping ±1.5 s, spelling fixes, `.post.txt` with rotating approved closer, used-moments log, `campaign-posted`, compliance report). **No API calls, $0.** Human/campaign picks the moments (campaign rules ban auto-clipping tools) |
| 11.1 auto setup | `watermark.position: auto` (least face overlap + darkest background, locked per campaign; avoids TikTok's button column), `names:` auto spelling variants (≥0.8 apply, 0.7–0.8 suggest only, under 4 letters suggest only), profanity masking `f***` in captions, hook and post text (audio never changed), sensitive-word warnings. Word lists in `config/wordlists/` (3 tiers, from `docs/wordlists.md`) |
| R2 / R2.1 zooms | Deterministic zooms on the first line + loudest sentences; **smooth cosine ease 0.4 s, scale 1.15**, max upscale 2.1; no zoom across camera cuts. `style: punch` kept as an option |
| Housekeeping | All third-party test videos and their runs deleted (D-012 done); about 3.9 GB freed |

## 3. Decisions added (check DECISIONS.md)
- D-021 stacked layout (brief only, not built)
- D-022/D-023 + updates
- D-024 public proof via campaigns on Igor's own accounts
- D-025 source footage obtained per campaign instructions (neutral wording)
- D-026 campaign mode: human/campaign-picked, software-rendered
- D-027 zooms + update
- D-029 auto mode allowed in Claude Code (`disableAutoMode` removed; push still asks; bypass never)
- D-030 auto setup

**⚠️ D-028 (Fable may write code within its included weekly limit, usage credits off) was approved by Igor but may not be in DECISIONS.md yet. Add it.**

## 4. Business track (finally running)
- **Brand: MiniCast.**
  - TikTok **@minicast.tv**. Display name is stuck as "minicast.tv" until about Oct 19 (7-day name lock), then rename to MiniCast.
  - Instagram **@minicast.tv**, Business account.
  - YouTube **@watchminicast**, verified intermediate; advanced (ID check) pending.
  - Purple/black/white brand. Logo, banner and watermark were made with ChatGPT.
- **Campaign 1** (podcast clipping on Whop Content Rewards, joined Oct 12):
  - Pay: $1 per 1K views, min payout at 2K views, max $300 per clip.
  - Rules: ≥40% US/CA/UK viewers, ≥1% engagement, supplied watermark on every clip, approved closer on every caption, each clip posted once (different moments per platform).
  - Submit **48 h after posting** with analytics screenshots including **viewer location**.
  - Moments, closers and spelling are in `campaigns/<slug>/`.
- **Campaign 2/3** (another marketplace's sports-podcast campaigns): researched, not joined for posting. Their rules ticket was never sent. Parked.
- Accounts are 2+ months old; Igor chose a shortened/no warm-up to catch budget. **The first posts are the experiment:** watch views, % US/CA/UK and engagement.

## 5. Next steps (in order)
0. **First thing next session:** build **Brief 11.2** (`docs/brief-11-2-per-platform-outputs.md`): one folder per platform, the right caption per platform, `posting_plan.csv`, and `--fix-platform`. Then fix the records (A03 → instagram, B05 → youtube; tonight they were marked under "tiktok" as a workaround) and re-render.
   - **Volume plan (Igor, Oct 13):** **3 posts per platform per day** (9 total) at about 12:30 / 3:30 / 6:30 PM ET. Go to **4 per platform** (the campaign cap is 4 a day per account) if the first results are healthy.
   - The 32 remaining moments last about 3 days. The campaign pool is expected to run out within a week, so **submit each clip promptly at 48 h** (budget dry-up = unpaid views).
   - **Join 1–2 more podcast campaigns** so the queue doesn't run dry. Mind Igor’s own business launch (Svitlo, opens Oct 20).
1. **Tue morning (Igor):** pin an opinion comment on each post and reply to comments in the first hour.
2. **Thu Oct 15, from ~noon (48 h after posting):** analytics screenshots (views, likes, comments, **location**) + a post screenshot → campaign form → post in `#content-rewards`. Record the results.
3. **Set up Buffer** (free: 3 channels, 10 queued posts each) to schedule the buffer of clips. Test whether TikTok posts publish fully or need a phone tap.
4. **Disclosure question:** decide on platform paid-promotion labels; ask the campaign. Not legal advice; flagged.
5. **Check the auto-spelling "curious like" → show name** rule (listen at its timestamp in `auto_spelling.json`); veto with an identity rule if it's normal speech.
6. Watch one clip for **wide two-person shots**. Only then build Brief 8.3 (stacked layout).
7. After real results: tune hook, zoom and caption config from data, not taste.

## 6. Parked
- Cover image export per clip
- Buffer API push from Clip Machine
- R3 B-roll (off by default)
- R4 music bed (campaigns mostly ban music)
- Lavender caption colour test
- Closers advance on every render, not only on post (harmless)
- AI-mode extras (scoring rubric 9.1, edit-point guard, vocabulary hotwords)
- Loudness, low-res warning
- UI, batch, README, demo, case study
- **EVAL row 3:** the T3 videos were deleted before rating, so log it as "not rated"
- The `docs/brief-11-*.md` files stay untracked (real names); keep them local
- Clean up risky "always allow" entries in `C:\Users\15513\.claude\settings.json` (from session 4)

## 7. Standing rules (abbreviated)
- **Secrets:** the API key only in `.env` as `CLIPMACHINE_ANTHROPIC_API_KEY`. Never export `ANTHROPIC_API_KEY`.
- **Money:** $0 cash. Max API credits only; Console spend limit $5. Fable within its included weekly limit, usage credits off. A new paid dependency means asking Igor.
- **Privacy:** campaign data and run folders are gitignored. Video frames may be viewed for **public, campaign-authorized** footage only, never private client video.
- **Claude Code:** auto mode OK, push always asks, one session at a time (two sessions collided once), Opus/Fable plans and reviews, Sonnet subagents build, checks run separately, branches per brief, merge after Igor's phone check.
- **Campaign integrity:** never post AI-picked moments to campaigns that ban auto-clipping; never burn the MiniCast logo into clips; each clip posted once.
- **Reply format:** senior colleague; "Learn:" notes; label fact, judgment or estimate; status line shows Build, Business, time and API; end with the next step and its owner.

## 8. Numbers
- API spend about **$0.6** of $100 (campaign renders cost $0)
- Tests: **2043** passing
- Campaign render about **20 s per clip** (tracks reused); 35 clips rendered for campaign 1

## 9. Session history
| Session | Date (ET) | What happened |
|---|---|---|
| 1–4 | Oct 10–12 | Gate 0 → MVP finish line (see the previous handoff in git history) |
| 5 | Oct 12 01:33–~02:30 | Briefs 8.3 (stacked layout) and R1 written; music/B-roll research; campaign search started |
| 6 | Oct 12 14:38 – Oct 13 ~03:00 | R1 built; MiniCast brand on 3 platforms; Briefs 11, 11.1, R2, R2.1 built and merged; cleanup; **first 3 posts scheduled** |
