# Clip Machine v1.0.0

The first tagged release: a local CLI that turns a long video into short vertical clips ready
for Shorts, Reels and TikTok, plus a campaign mode for clipping work with posting rules.

## What's in it

**Pipeline (`run`):** six restartable stages, each reading and writing files in one run folder.
- `fetch`: checks the input is real video within limits and extracts 16 kHz mono audio. The
  original file is never changed.
- `transcribe`: local faster-whisper with word timestamps. Uses the GPU when one is there and
  falls back to the CPU.
- `candidates`: Claude Haiku proposes moments per 10-minute block, snapped to sentence
  boundaries.
- `score`: Claude Sonnet judges the candidates together (up to 20 per call) and writes a
  score, keep or reject, a hook and a title. It picks sentences, never timestamps.
- `render`: one ffmpeg pass per clip with:
  - a speaker-following 9:16 crop (face tracking, active speaker, shot cuts)
  - a graphics-aware fit layout for shots with on-screen text
  - karaoke captions inside configured safe-area margins
  - a hook line for the first seconds
  - eased punch-in zooms
- `report`: one offline `report.html` to review the clips, with the score, reason, cost and
  timings.

**Campaign mode:** `campaign-init`, `produce`, `scout`, `campaign`, `plan`, `campaign-posted`
and `campaign-reject`.
- Clips come from human-picked moment lists or, when a campaign's rules allow it, from the AI
  scout.
- Each clip gets a watermark (auto-placed), word-list masking and name-spelling fixes.
- Each clip gets paste-ready post text for its platform.
- A combined posting plan covers all campaigns, with daily caps per platform and per campaign.
- Posts are recorded at their scheduled slot.

**Safety:**
- **Budget guard:** worst-case cost is checked per call against a run cap and a daily cap
  before anything is sent.
- **Kill switch:** `CLIPMACHINE_NO_API=1` blocks every API call.
- **Retries:** none on billing or permission errors.
- **Structured output:** schema-enforced JSON validated by strict pydantic models, with one
  repair attempt.
- **Untrusted text:** the transcript and briefs are fenced as data. Model text never becomes a
  path or a command.
- **Secrets:** the key lives only in `.env`, and logs never hold prompts, transcripts or keys.

## Numbers at release

- 2,320 tests (2,317 passed, 3 skipped). ruff, ruff format, mypy (`disallow_untyped_defs`
  on `src/`) and pip-audit are clean.
- All API spend to date: $4.01 over 466 calls. AI scouting averages $0.35 per episode (36.7
  hours of source in total) and $0.044 per proposed clip.
- 111 campaign clips rendered, 41 posted or scheduled.

## Requirements

uv, ffmpeg and git. Developed and run on Windows 11, and the test suite also runs on Ubuntu in
CI. macOS is untested. Python 3.11 is installed by uv. An NVIDIA GPU is optional. An
Anthropic API key is needed for the `candidates` and `score` stages and for the AI scout.

## Known limits

- The active-speaker choice doesn't beat "never switch" on fast two-person back-and-forth.
  The stacked two-person layout (Brief 8.3) is written but not built.
- A clip can still cross an edit point in a compilation source.
- There's no UI: everything runs from the terminal, with a static HTML review page.
- The daily-budget ledger isn't locked, so two runs at the same moment could both pass the
  daily check.
- There are no audience results yet.

See `docs/ROADMAP.md` and the README's "Limits and roadmap" section.

## License

No license is granted. © 2026 IgorMe1nyk. All rights reserved.
