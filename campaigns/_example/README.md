# Example campaign

This folder shows the layout of a campaign folder. Everything in it is invented. Copy it to
`campaigns/<slug>/` and replace the content. The slug is lowercase letters, digits and dashes
(a leading underscore is allowed).

## What a campaign folder holds

- `campaign.yaml`: the rules (hook, watermark, spelling fixes, post caption parts).
- `moments.yaml`: the moments you chose, one entry each.
- `used.json`: written by the tool; it records which moments were rendered and posted. Do not edit it.
- `assets/`: the watermark PNG named in `campaign.yaml`.

Every folder under `campaigns/` except `_example` is git-ignored and must never be committed.

## Commands

Run from the repo root.

```
uv run python -m clipmachine campaign <source-video> --campaign <slug> [--only A03,B05] [--force-reuse]
uv run python -m clipmachine campaign-posted --campaign <slug> --id A03 --platform tiktok
```

The first renders the moments. The second marks a moment as posted, so it is not reused by
accident. Outputs go to `runs/<run>/campaign/<slug>/`.

## Times

`start` and `end` are source times, `h:mm:ss` or `mm:ss`. Put them in quotes: an unquoted
`1:05:26` is read by YAML as a number. `end` may be `auto` or left out; the tool then picks a
sentence end near the target length set in `config/default.yaml` (`campaign.auto_end_target_s`).
