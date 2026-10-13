"""campaign_report.html: the one-page review of a campaign's clips (Brief 11).

One offline file next to the clips: inline CSS, no JavaScript, no URLs, videos linked by their bare
file name. It is rebuilt on every run from the clip jsons that are in the folder and from
moments.yaml, so it lists every moment of the campaign, rendered or not. It holds the hook, the
post caption, the closer and the names, so it lives in runs/ (git-ignored); none of that text ever
goes to the console or a log. Everything that comes from a file is escaped by _esc().
"""

import contextlib
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import ValidationError

from clipmachine.campaign.loader import Campaign
from clipmachine.campaign.models import CHECKS, AutoSpelling, CampaignClip, Moment
from clipmachine.campaign.render import REVIEW_SUFFIX
from clipmachine.runs import write_text_atomic
from clipmachine.stages.report import CSS, _esc, _hms
from clipmachine.utils.image import color_type_name

REPORT_HTML = "campaign_report.html"

CHECK_LABELS = {
    "length_in_range": "length within range",
    "hook_from_start": "hook on screen from 0.0 s",
    "captions_on": "captions on",
    "watermark_full_and_clear": "watermark present for the full duration and not overlapped",
    "source_audio_only": "source audio only",
    "closer_present": "closer present",
    "not_previously_used": "moment not previously used",
    "names_listed": "names to check listed",
}
# a word and a symbol, never colour alone
CHECK_WORDS = {"pass": "&#10003; pass", "warn": "&#9650; warning", "fail": "&#10007; FAIL"}
BACKGROUNDS = {
    "transparent": "transparent",
    "opaque_black": "opaque black",
    "opaque_other": "opaque, not black",
}

EXTRA_CSS = """
.checks { list-style: none; padding: 0; margin: 6px 0; }
.checks li { margin: 2px 0; }
.ck { display: inline-block; min-width: 5.5em; font-weight: 600; }
.ck-pass { color: #1b6e3a; }
.ck-warn { color: #8a5a00; }
.ck-fail { color: #b3261e; }
.ck-none { color: #5b6475; }
pre.post { white-space: pre-wrap; background: #f1f3f7; border-radius: 6px; padding: 8px 10px;
  margin: 6px 0; overflow-wrap: anywhere; }
.names { margin: 4px 0; padding-left: 18px; }
.review { margin: 4px 0; padding-left: 18px; }
"""
WHERE = {"captions": "captions", "hook": "hook", "post": "post caption"}


def _clock(seconds: float) -> str:
    """m:ss.s, for a time inside a clip."""
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:04.1f}"


def _times(start: float | None, end: float | None) -> str:
    if start is None or end is None:
        return "unknown"
    return f"{_hms(start)}&ndash;{_hms(end)}"


def _checks(clip: CampaignClip) -> str:
    items = []
    for name in CHECKS:
        status = clip.checks.get(name)
        if status is None:
            word, css = "&ndash; not checked", "ck-none"
        else:
            word, css = CHECK_WORDS[status], f"ck-{status}"
        items.append(f'<li><span class="ck {css}">{word}</span> {_esc(CHECK_LABELS[name])}</li>')
    return '<ul class="checks">' + "".join(items) + "</ul>"


def _list(items: Sequence[str]) -> str:
    if not items:
        return ""
    return "<ul>" + "".join(f"<li>{_esc(item)}</li>" for item in items) + "</ul>"


def _requested(clip: CampaignClip) -> str:
    end = "auto" if clip.requested_end is None else _hms(clip.requested_end)
    return f"{_hms(clip.requested_start)}&ndash;{end}"


def _final(clip: CampaignClip) -> str:
    if clip.start is None or clip.end is None:
        return "no final times"
    length = f", {clip.duration:.2f} s" if clip.duration is not None else ""
    return f"{_times(clip.start, clip.end)} ({clip.start:.2f}&ndash;{clip.end:.2f} s{length})"


def _names(names: Mapping[str, Sequence[float]]) -> str:
    if not names:
        return '<p class="muted">No names to check.</p>'
    rows = []
    for name, times in names.items():
        said = ", ".join(_clock(t) for t in times) if times else "not said in this clip"
        rows.append(f"<li><b>{_esc(name)}</b>: {_esc(said)}</li>")
    return f'<p><b>Names to check</b></p><ul class="names">{"".join(rows)}</ul>'


def _review_block(clip: CampaignClip, review: str | None) -> str:
    """Masked words as a count (slurs counted apart) and the sensitive words by name, from the
    clip's review file; nothing when there is nothing to say."""
    rows = [line.split("\t") for line in (review or "").splitlines() if line.count("\t") == 3]
    slurs = sum(1 for what, _, _, kind in rows if what == "masked" and kind == "slur")
    parts = []
    if clip.masked_words:
        apart = f" ({slurs} slur{'s' if slurs != 1 else ''})" if slurs else ""
        count = f"{clip.masked_words} word{'s' if clip.masked_words != 1 else ''}"
        parts.append(f"<p><b>Masked:</b> {count}{apart}</p>")
    sensitive = [(where, word, kind) for what, where, word, kind in rows if what == "sensitive"]
    if sensitive:
        items = "".join(
            f"<li>{'a slur' if kind == 'slur' else _esc(word)} ({_esc(WHERE.get(where, where))})"
            "</li>"
            for where, word, kind in sensitive
        )
        parts.append(
            f"<p><b>Sensitive words</b> (reword, or add to <code>allowed:</code> in campaign.yaml "
            f'if the campaign is fine with it)</p><ul class="review">{items}</ul>'
        )
    elif clip.sensitive_words:
        parts.append(
            f"<p><b>Sensitive words:</b> {clip.sensitive_words} (the review file is missing)</p>"
        )
    return "".join(parts)


def _card(
    moment: Moment, clip: CampaignClip | None, post: str | None, review: str | None = None
) -> str:
    name = moment.clip_name
    heading = f"<h2>{_esc(moment.id)} &middot; {_esc(moment.platform)}</h2>"
    if clip is None:
        return (
            '<section class="card">\n'
            '<div class="player"><div class="missing">Not rendered yet</div></div>\n'
            f'<div class="info">\n{heading}\n'
            f'<p class="muted">requested {_hms(moment.start)}&ndash;'
            f"{'auto' if moment.end is None else _hms(moment.end)}</p>\n"
            f"<p><b>Hook:</b> {_esc(moment.hook)}</p>\n</div>\n</section>"
        )
    if clip.status == "rendered":
        player = f'<video controls preload="metadata" width="360" src="{_esc(name)}.mp4"></video>'
    else:
        player = f'<div class="missing">Not rendered: {_esc(clip.error or "unknown reason")}</div>'
    badges = "".join(f'<span class="badge">{_esc(note)}</span>' for note in clip.notes)
    end_line = ""
    if clip.end_mode == "auto" and clip.auto_end_target is not None:
        actual = f"{clip.duration:.2f}" if clip.duration is not None else "unknown"
        end_line = (
            f"<p>end: auto (target {_esc(f'{clip.auto_end_target:g}')} s, actual {actual} s)</p>\n"
        )
    parts = [
        '<section class="card">\n',
        f'<div class="player">{player}</div>\n',
        f'<div class="info">\n{heading}\n',
        f'<p class="muted">requested {_requested(clip)} &rarr; final {_final(clip)}</p>\n',
        end_line,
    ]
    notes = _list([*clip.snap_notes, *clip.warnings])
    if notes:
        parts.append(f"<p><b>Snapping and warnings</b></p>{notes}\n")
    if clip.status == "rendered":
        parts.append(f"<p><b>Hook:</b> {_esc(moment.hook)}</p>\n")
        if post is not None:
            parts.append(
                f'<p><b>Post caption</b> (copy this)</p><pre class="post">{_esc(post)}</pre>\n'
            )
        else:
            parts.append('<p class="muted">The post text file is missing.</p>\n')
        parts.append(f"<p><b>Closer:</b> {_esc(clip.closer or '')}</p>\n")
        parts.append(f"<p>{_esc(clip.spelling_replacements)} spelling fixes</p>\n")
        parts.append(_names(clip.names_to_check) + "\n")
    block = _review_block(clip, review)
    if block:
        parts.append(block + "\n")
    parts.append("<p><b>Compliance checklist</b></p>" + _checks(clip) + "\n")
    if badges:
        parts.append(f"<p>{badges}</p>\n")
    if clip.render_seconds is not None:
        parts.append(f'<p class="muted">rendered in {_esc(f"{clip.render_seconds:.1f}")} s</p>\n')
    parts.append("</div>\n</section>")
    return "".join(parts)


def _auto_spelling(auto: AutoSpelling | None) -> str:
    """The automatic spelling rules, the suggestions and the variants an explicit rule covers."""
    if auto is None:
        return ""
    lines = [f"<p><b>Names:</b> {_esc(', '.join(auto.names))}</p>"]

    def items(status: str, arrow: bool) -> str:
        rows = []
        for v in auto.variants:
            if v.status != status:
                continue
            seen = "; ".join(f"{_esc(s.clip)} {_clock(s.at)}" for s in v.seen)
            where = f", heard at {seen}" if seen else ""
            target = f" &rarr; {_esc(v.rule_to or v.name)}" if arrow else f" (for {_esc(v.name)})"
            rows.append(
                f"<li><b>{_esc(v.text)}</b>{target}: {v.count} time{'s' if v.count != 1 else ''}, "
                f"ratio {v.ratio:.2f}{where}</li>"
            )
        return f'<ul class="names">{"".join(rows)}</ul>' if rows else '<p class="muted">none</p>'

    lines.append(
        "<p><b>Automatic spelling rules</b> (from the transcript; to veto one, add "
        '<code>{from: "&lt;variant&gt;", to: "&lt;variant&gt;"}</code> to '
        "<code>spelling</code>)</p>" + items("apply", True)
    )
    lines.append(
        f"<p><b>Suggestions, not applied</b> (ratio {auto.suggest_threshold:g}&ndash;"
        f"{auto.apply_threshold:g} with the same sound; add a spelling rule to apply one)</p>"
        + items("suggest", False)
    )
    if any(v.status == "explicit" for v in auto.variants):
        lines.append("<p><b>Already in your spelling list</b></p>" + items("explicit", False))
    return "\n".join(lines)


def _auto_position(campaign: Campaign) -> str:
    """The chosen watermark position with its score, and the runner-up."""
    lock = campaign.auto
    if lock is None:
        return "<p><b>Watermark position:</b> auto, not chosen yet (no moment rendered)</p>"
    text = (
        f"<p><b>Watermark position:</b> auto &rarr; x {lock.x_center:.2f}, y {lock.y_center:.2f} "
        f"(face overlap {lock.face_cost:.2f}, background luminance {lock.luminance:.0f} of 255, "
        f"score {lock.score:.3f})"
    )
    if lock.runner_up is not None:
        r = lock.runner_up
        text += (
            f"; runner-up x {r.x_center:.2f}, y {r.y_center:.2f} (face {r.face_cost:.2f}, "
            f"luminance {r.luminance:.0f}, score {r.score:.3f})"
        )
    text += (
        f". Scored on {_esc(', '.join(lock.sampled))}: {lock.scored} of {lock.candidates} grid "
        f"positions had room. Locked in auto_position.json; delete it to choose again.</p>"
    )
    if lock.notes:
        text += _list(lock.notes)
    return text


def _header(
    campaign: Campaign,
    run_name: str,
    moments: Sequence[Moment],
    clips: Mapping[str, CampaignClip],
    auto: AutoSpelling | None = None,
) -> str:
    cfg, asset = campaign.config, campaign.asset
    states = [clips.get(m.clip_name) for m in moments]
    rendered = sum(1 for c in states if c is not None and c.status == "rendered")
    failed = sum(1 for c in states if c is not None and c.status == "failed")
    waiting = sum(1 for c in states if c is None)
    wm = campaign.watermark
    where = (
        f"visible box on the frame x {wm.rect.x1:g}&ndash;{wm.rect.x2:g}, "
        f"y {wm.rect.y1:g}&ndash;{wm.rect.y2:g}"
        if wm is not None
        else "position not chosen yet"
    )
    alpha = "yes" if asset.info.has_alpha else "no"
    background = BACKGROUNDS.get(asset.background, asset.background)
    lines = [
        f"<h1>{_esc(cfg.name)}</h1>",
        f'<p class="muted">{_esc(cfg.source_note)}</p>' if cfg.source_note else "",
        f'<p class="muted">Run folder: {_esc(run_name)}</p>',
        f"<p><b>{rendered} rendered &middot; {failed} failed &middot; {waiting} not rendered "
        f"yet</b> &middot; API calls: 0</p>",
        f"<p>Clip length {_esc(f'{cfg.clip_length.min:g}')}&ndash;"
        f"{_esc(f'{cfg.clip_length.max:g}')} s &middot; hook mode {_esc(cfg.hook.mode)}"
        + (f" ({campaign.hook_seconds:g} s)" if cfg.hook.mode == "intro" else "")
        + f", font size {_esc(campaign.hook_font_size)}"
        + f", position {_esc(campaign.hook_position)}</p>",
        f"<p>Watermark {_esc(cfg.watermark.file)}: "
        f"format PNG ({_esc(color_type_name(asset.info.color_type))}), alpha {alpha}, "
        f"background {_esc(background)}, "
        f"blend {_esc(asset.blend)}; {where}</p>",
    ]
    if cfg.watermark.position == "auto":
        lines.append(_auto_position(campaign))
    if campaign.warnings:
        lines.append("<p><b>Warnings</b></p>" + _list(campaign.warnings))
    lines.append(_auto_spelling(auto))
    return '<header class="summary">\n' + "\n".join(line for line in lines if line) + "\n</header>"


def build_html(
    *,
    campaign: Campaign,
    run_name: str,
    moments: Sequence[Moment],
    clips: Mapping[str, CampaignClip],
    posts: Mapping[str, str],
    reviews: Mapping[str, str] | None = None,
    auto: AutoSpelling | None = None,
) -> str:
    """The whole page. `clips`, `posts` and `reviews` (the .review.txt texts) are keyed by
    Moment.clip_name; a moment with no clip is listed as not rendered yet."""
    found = reviews or {}
    cards = [
        _card(m, clips.get(m.clip_name), posts.get(m.clip_name), found.get(m.clip_name))
        for m in moments
    ]
    body = "\n".join([_header(campaign, run_name, moments, clips, auto), *cards])
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>Campaign report: {_esc(campaign.config.name)}</title>\n"
        f"<style>{CSS}{EXTRA_CSS}</style>\n</head>\n<body>\n<main>\n{body}\n</main>\n</body>\n</html>\n"
    )


def write_report(
    out_dir: Path,
    campaign: Campaign,
    moments: Sequence[Moment],
    run_name: str,
    *,
    auto: AutoSpelling | None = None,
) -> Path:
    """Build the page from the jsons and post texts in `out_dir` and write it there. A json that
    is missing or does not validate counts as not rendered yet."""
    clips: dict[str, CampaignClip] = {}
    posts: dict[str, str] = {}
    reviews: dict[str, str] = {}
    for moment in moments:
        name = moment.clip_name
        try:
            clips[name] = CampaignClip.model_validate_json(
                (out_dir / f"{name}.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError, ValidationError):
            continue
        with contextlib.suppress(OSError):
            posts[name] = (out_dir / f"{name}.post.txt").read_text(encoding="utf-8")
        with contextlib.suppress(OSError):
            reviews[name] = (out_dir / f"{name}{REVIEW_SUFFIX}").read_text(encoding="utf-8")
    path = out_dir / REPORT_HTML
    write_text_atomic(
        path,
        build_html(
            campaign=campaign,
            run_name=run_name,
            moments=moments,
            clips=clips,
            posts=posts,
            reviews=reviews,
            auto=auto,
        ),
    )
    return path
