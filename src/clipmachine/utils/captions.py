"""Word-group captions as an ASS subtitle file. Pure functions: no I/O, no ffmpeg.

Two styles. "legacy": the group on screen with the spoken word recoloured, white text with a black
outline at the bottom (`build_events`, `build_ass`). "box_karaoke" (Brief R1): one short line of
words at a time on a drawn rounded box, each word turning from the upcoming to the spoken colour
at its own start with native ASS `\\k` karaoke, placed inside the safe area (`build_karaoke`,
`build_ass_v2`). Box and text are sized from the caption font file's metrics.

Transcript words are untrusted data. Before a word reaches the file, `{`, `}` and `\\` are removed
(so a word can never become an override tag) and control characters become spaces.
"""

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from clipmachine.models import HEX_COLOR_RE, CaptionsConfig, TranscriptSegment
from clipmachine.utils.fontmetrics import FontMetrics
from clipmachine.utils.safezone import Rect, caption_anchor

GAP_FILL_SECONDS = 0.5  # a silence shorter than this keeps the previous caption on screen
MIN_EVENT_SECONDS = 0.1  # shortest time the last word of a clip stays up
SENTENCE_END = (".", "?", "!")
STYLE_NAME = "Clip"
OUTLINE_PX = 4
SIDE_MARGIN_PX = 60
ASS_WHITE = "&H00FFFFFF"
ASS_BLACK = "&H00000000"

_UNSAFE_CHARS_RE = re.compile(r"[{}\\]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f  \x85]")
_SPACES_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class CaptionWord:
    text: str  # cleaned: no leading/trailing spaces, nothing that ASS could read as a tag
    start: float  # seconds from the start of the clip
    end: float


@dataclass(frozen=True)
class CaptionEvent:
    start: float
    end: float
    text: str  # ASS text with the highlighted word's override tags


def clean_word(text: str) -> str:
    """One word as safe ASS text: control characters and newlines become spaces, `{ } \\` are
    removed, runs of spaces collapse, and both ends are stripped. May return an empty string."""
    text = _CONTROL_RE.sub(" ", text)
    text = _UNSAFE_CHARS_RE.sub("", text)
    return _SPACES_RE.sub(" ", text).strip()


def clip_words(
    segments: Iterable[TranscriptSegment], start: float, end: float
) -> list[CaptionWord]:
    """The transcript words that overlap [start, end], with times shifted so the clip begins at 0
    and clamped to [0, end - start]. Words with nothing left after cleaning are dropped."""
    duration = end - start
    found: list[CaptionWord] = []
    for segment in segments:
        for word in segment.words:
            if word.end <= start or word.start >= end:
                continue
            text = clean_word(word.word)
            if not text:
                continue
            # milliseconds: removes float noise so gap comparisons below are exact
            shifted_start = round(min(max(word.start - start, 0.0), duration), 3)
            shifted_end = round(min(max(word.end - start, shifted_start), duration), 3)
            found.append(CaptionWord(text, shifted_start, shifted_end))
    found.sort(key=lambda w: (w.start, w.end))
    return found


def group_words(words: list[CaptionWord], per_line: int) -> list[list[CaptionWord]]:
    """Groups of `per_line` words; a group also ends after a word ending in `. ? !`."""
    groups: list[list[CaptionWord]] = []
    current: list[CaptionWord] = []
    for word in words:
        current.append(word)
        if len(current) >= per_line or word.text.endswith(SENTENCE_END):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def ass_color(hex_color: str) -> str:
    """`#RRGGBB` as the ASS colour `&HBBGGRR&` (used inside override tags)."""
    if not HEX_COLOR_RE.fullmatch(hex_color):
        raise ValueError(f"colour must look like #RRGGBB, got {hex_color!r}")
    red, green, blue = hex_color[1:3], hex_color[3:5], hex_color[5:7]
    return f"&H{blue}{green}{red}&".upper()


def build_events(
    groups: list[list[CaptionWord]],
    highlight_color: str,
    clip_seconds: float,
    offset_seconds: float = 0.0,
) -> list[CaptionEvent]:
    """One event per word: its whole group on screen, that word in the highlight colour.

    An event runs from the word's start to the next word's start, also across groups. When the
    silence before the next word is GAP_FILL_SECONDS or more, it ends at the word's own end
    instead, so captions disappear in real pauses. The clip's last word runs to its end.

    offset_seconds then moves every event (positive = later), clamped to [0, clip_seconds];
    events left with no length are dropped.
    """
    colour = ass_color(highlight_color)
    flat = [word for group in groups for word in group]
    events: list[CaptionEvent] = []
    position = 0
    for group in groups:
        for index, word in enumerate(group):
            following = flat[position + 1] if position + 1 < len(flat) else None
            position += 1
            gap = None if following is None else round(following.start - word.end, 3)
            if following is not None and gap is not None and gap < GAP_FILL_SECONDS:
                end = following.start
            else:
                end = word.end
            if following is None:
                end = min(max(end, word.start + MIN_EVENT_SECONDS), clip_seconds)
            if end <= word.start:
                continue
            parts = [
                f"{{\\c{colour}}}{w.text}{{\\r}}" if i == index else w.text
                for i, w in enumerate(group)
            ]
            events.append(CaptionEvent(word.start, end, " ".join(parts)))
    return _shifted(events, offset_seconds, clip_seconds)


def _shifted(
    events: list[CaptionEvent], offset_seconds: float, clip_seconds: float
) -> list[CaptionEvent]:
    if offset_seconds == 0.0:
        return events
    shifted: list[CaptionEvent] = []
    for event in events:
        start = round(min(max(event.start + offset_seconds, 0.0), clip_seconds), 3)
        end = round(min(max(event.end + offset_seconds, 0.0), clip_seconds), 3)
        if end > start:
            shifted.append(CaptionEvent(start, end, event.text))
    return shifted


def ass_time(seconds: float) -> str:
    """H:MM:SS.cc, rounded to centiseconds."""
    total = max(round(seconds * 100), 0)
    hours, rest = divmod(total, 360_000)
    minutes, rest = divmod(rest, 6_000)
    secs, centis = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _font_name(font: str) -> str:
    """A font name that cannot break a Style line (no commas or newlines)."""
    return re.sub(r"[,\r\n]", " ", font).strip() or "Montserrat"


def build_ass(
    events: list[CaptionEvent],
    *,
    font: str,
    font_size: int,
    margin_v: int,
    width: int,
    height: int,
) -> str:
    """A complete ASS file: white bold text, black outline, no box, bottom centre."""
    font_name = _font_name(font)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: {STYLE_NAME},{font_name},{font_size},{ASS_WHITE},{ASS_WHITE},{ASS_BLACK},"
        f"{ASS_BLACK},-1,0,0,0,100,100,0,0,1,{OUTLINE_PX},0,2,{SIDE_MARGIN_PX},{SIDE_MARGIN_PX},"
        f"{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lines.extend(
        f"Dialogue: 0,{ass_time(e.start)},{ass_time(e.end)},{STYLE_NAME},,0,0,0,,{e.text}"
        for e in events
    )
    return "\n".join(lines) + "\n"


# --- box_karaoke style (Brief R1) --------------------------------------------------------------

BOX_RADIUS_PX = 20  # corner radius of the drawn box (smaller if the box is flatter than that)
BEZIER_CORNER = 0.5523  # control-point distance, x radius, that makes a Bezier arc a quarter circle
MIN_FONT_FRACTION = 0.5  # an overlong word is shrunk to at most this fraction of the font size
ELLIPSIS = "…"
CAP_STYLE = "Cap"  # the caption text
HOOK_STYLE = "Hook"  # the hook text (defined now; a later commit writes events in it)
BOX_STYLE = "Box"  # the drawn rectangle behind the text
LAYER_BOX = 0
LAYER_TEXT = 1


@dataclass(frozen=True)
class KaraokeSettings:
    """The caption settings that shape lines and boxes (from CaptionsConfig, Brief R1)."""

    font_size: int
    max_words: int
    max_width_px: int  # the whole box, padding included
    word_gap_em: float
    letter_spacing_px: float
    spoken_color: str
    upcoming_color: str
    box_opacity: float
    box_padding_px: int

    @classmethod
    def from_config(cls, captions: CaptionsConfig) -> "KaraokeSettings":
        return cls(
            font_size=captions.font_size,
            max_words=captions.max_words,
            max_width_px=captions.max_width_px,
            word_gap_em=captions.word_gap_em,
            letter_spacing_px=captions.letter_spacing_px,
            spoken_color=captions.spoken_color,
            upcoming_color=captions.upcoming_color,
            box_opacity=captions.box_opacity,
            box_padding_px=captions.box_padding_px,
        )

    @property
    def text_limit_px(self) -> float:
        """The widest a line of text may be: the box width minus its padding on both sides."""
        return float(self.max_width_px - 2 * self.box_padding_px)


@dataclass(frozen=True)
class AssEvent:
    """One Dialogue line: layer, times in seconds, style name and ASS text."""

    layer: int
    start: float
    end: float
    style: str
    text: str


@dataclass(frozen=True)
class KaraokeLine:
    """One caption line: the words on screen together and when they are there."""

    words: tuple[CaptionWord, ...]  # text as displayed (an overlong word may be cut short)
    start: float
    end: float
    font_size: int  # the settings' size, or smaller for a word that was too wide
    width: float  # width of the text, spacing included


@dataclass(frozen=True)
class KaraokeLines:
    lines: list[KaraokeLine]
    words_shrunk: int  # words drawn at a smaller font size to fit
    words_truncated: int  # words cut short (and given an ellipsis) to fit


@dataclass(frozen=True)
class LinePlacement:
    cx: int  # centre x of the text and box, ASS \pos x
    y: int  # ASS \pos y of the text (alignment 5: the middle of the line box)
    box: Rect  # the box behind the text, from the text's real metrics


@dataclass(frozen=True)
class KaraokeResult:
    events: list[AssEvent]
    lines: int
    max_words: int  # most words in one line
    boxes: list[Rect]  # one per line, to check against the safe area
    notes: list[str]  # reason codes only, never text
    words_shrunk: int
    words_truncated: int


def fmt_num(value: float) -> str:
    """At most 2 decimals, no trailing zeros: 1.0 -> `1`, 16.2 -> `16.2`."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def style_color(hex_color: str, alpha: int = 0) -> str:
    """`#RRGGBB` as the ASS style colour `&HAABBGGRR` (alpha 0 = opaque, 255 = invisible)."""
    return f"&H{alpha:02X}{ass_color(hex_color)[2:-1]}"


def space_spacing_px(
    metrics: FontMetrics, font_size: float, word_gap_em: float, letter_spacing: float
) -> float:
    """The `\\fsp` value the space between two words gets, so the visible gap is
    `word_gap_em * font_size`: the letter spacing after the last letter plus the space glyph plus
    this. Never negative (a font whose space is already wider than the gap keeps its space)."""
    gap = word_gap_em * font_size
    return max(gap - metrics.space_px(font_size) - letter_spacing, 0.0)


def spaced_width(
    metrics: FontMetrics,
    texts: Sequence[str],
    font_size: float,
    word_gap_em: float,
    letter_spacing: float,
) -> float:
    """Width of the words as `spaced_text` draws them on one line."""
    if not texts:
        return 0.0
    between = metrics.space_px(font_size) + space_spacing_px(
        metrics, font_size, word_gap_em, letter_spacing
    )
    words = sum(metrics.text_width(text, font_size, letter_spacing) for text in texts)
    return words + (len(texts) - 1) * between


def spaced_text(
    texts: Sequence[str],
    space_fsp: float,
    letter_spacing: float,
    k_values: Sequence[int] | None = None,
) -> str:
    """The words joined by a space that carries `space_fsp` extra pixels:
    `a{\\fsp<space_fsp>} {\\fsp<letter_spacing>}b`. With `k_values` (one per word; the first is
    ignored, the caller puts it in the line's first block) the second block also carries the
    next word's `\\k`. The texts must already be clean."""
    out = [texts[0]] if texts else []
    for index in range(1, len(texts)):
        karaoke = "" if k_values is None else f"\\k{k_values[index]}"
        out.append(f"{{\\fsp{fmt_num(space_fsp)}}} {{\\fsp{fmt_num(letter_spacing)}{karaoke}}}")
        out.append(texts[index])
    return "".join(out)


def shift_words(
    words: Sequence[CaptionWord], offset_seconds: float, clip_seconds: float
) -> list[CaptionWord]:
    """Every word moved by `offset_seconds` (positive = later), clamped to [0, clip_seconds] and
    rounded to milliseconds. A word with no length is kept (Whisper emits them); only a word the
    shift pushed outside the clip is dropped: one that starts at or after the clip's end, or lies
    entirely before its start."""
    shifted: list[CaptionWord] = []
    for word in words:
        raw_start = word.start + offset_seconds
        raw_end = word.end + offset_seconds
        if raw_start >= clip_seconds or (raw_end <= 0 and raw_start < 0):
            continue
        start = round(min(max(raw_start, 0.0), clip_seconds), 3)
        end = round(min(max(raw_end, start), clip_seconds), 3)
        shifted.append(CaptionWord(word.text, start, end))
    return shifted


def _fit_word(
    word: CaptionWord, settings: KaraokeSettings, metrics: FontMetrics
) -> tuple[CaptionWord, int, str]:
    """A word that is too wide alone, made to fit: (word, font size, "" | "shrunk" | "truncated").

    First the font size goes down, never below half; if the word still does not fit, characters
    are cut from its end and an ellipsis (three dots if the font lacks one) is appended."""
    size = settings.font_size
    spacing = settings.letter_spacing_px
    limit = settings.text_limit_px
    width = metrics.text_width(word.text, size, spacing)
    if width <= limit:
        return word, size, ""
    lowest = max(math.ceil(size * MIN_FONT_FRACTION), 1)
    size = max(math.floor(size * limit / width), lowest)
    while size > lowest and metrics.text_width(word.text, size, spacing) > limit:
        size -= 1
    if metrics.text_width(word.text, size, spacing) <= limit:
        return word, size, "shrunk"
    mark = ELLIPSIS if metrics.has_glyph(ELLIPSIS) else "..."
    body = word.text
    while body and metrics.text_width(body.rstrip() + mark, size, spacing) > limit:
        body = body[:-1]
    return CaptionWord(body.rstrip() + mark, word.start, word.end), size, "truncated"


def karaoke_lines(
    words: Sequence[CaptionWord],
    settings: KaraokeSettings,
    metrics: FontMetrics,
    *,
    clip_seconds: float,
    offset_seconds: float = 0.0,
) -> KaraokeLines:
    """The caption lines for a clip's words; exactly one line is on screen at a time.

    `offset_seconds` moves the words first. Then `group_words(words, max_words)` makes the groups
    (they also end after a sentence end), and inside a group a new line starts when the next word
    would make the line wider than the box allows, or after a pause of GAP_FILL_SECONDS or more.

    A line starts at its first word. It ends at the next line's start when the silence before it
    is shorter than GAP_FILL_SECONDS, else at its last word's end; the clip's last line lasts at
    least MIN_EVENT_SECONDS (inside the clip). A word too wide for a line of its own is shrunk or
    cut short (see `_fit_word`); the counts say how often.
    """
    shifted = shift_words(words, offset_seconds, clip_seconds)
    size = settings.font_size
    spacing = settings.letter_spacing_px
    groups: list[list[CaptionWord]] = []
    for group in group_words(shifted, settings.max_words):
        current: list[CaptionWord] = []
        for word in group:
            if current:
                texts = [w.text for w in current] + [word.text]
                too_wide = (
                    spaced_width(metrics, texts, size, settings.word_gap_em, spacing)
                    > settings.text_limit_px
                )
                paused = round(word.start - current[-1].end, 3) >= GAP_FILL_SECONDS
                if too_wide or paused:
                    groups.append(current)
                    current = []
            current.append(word)
        if current:
            groups.append(current)

    lines: list[KaraokeLine] = []
    shrunk = truncated = 0
    for index, members in enumerate(groups):
        line_size = size
        if len(members) == 1:
            fitted, line_size, outcome = _fit_word(members[0], settings, metrics)
            members = [fitted]
            shrunk += outcome == "shrunk"
            truncated += outcome == "truncated"
        last = members[-1]
        following = groups[index + 1][0].start if index + 1 < len(groups) else None
        if following is not None and round(following - last.end, 3) < GAP_FILL_SECONDS:
            end = following
        else:
            end = last.end
        if following is None:
            end = min(max(end, last.start + MIN_EVENT_SECONDS), clip_seconds)
        end = round(end, 3)
        if end <= members[0].start:
            continue
        width = spaced_width(
            metrics, [w.text for w in members], line_size, settings.word_gap_em, spacing
        )
        lines.append(KaraokeLine(tuple(members), members[0].start, end, line_size, width))
    return KaraokeLines(lines, shrunk, truncated)


def _centiseconds(seconds: float) -> int:
    """The same rounding `ass_time` applies."""
    return max(round(seconds * 100), 0)


def line_k_values(line: KaraokeLine) -> list[int]:
    """The `\\k` value (centiseconds) of each word. Word k turns to the spoken colour when the
    earlier values add up, which is `cs(word_k.start) - cs(line.start)`: every word switches at
    its own start to within one centisecond, with no drift. The last word's value runs to the
    end of the line."""
    base = _centiseconds(line.start)
    switches = [_centiseconds(word.start) - base for word in line.words]
    values = [max(later - now, 0) for now, later in zip(switches, switches[1:], strict=False)]
    values.append(max(_centiseconds(line.end) - _centiseconds(line.words[-1].start), 0))
    return values


def place_line(
    line: KaraokeLine, settings: KaraokeSettings, metrics: FontMetrics, safe: Rect
) -> LinePlacement:
    """Where a line and its box go: centred on the safe area, the box bottom `caption_anchor` up.

    The text anchor (alignment 5, \\pos) is an integer, as ASS requires; the box is then computed
    from that integer, so it follows where libass really draws the text: the baseline is
    `ascent` below the top of the line box, which is `font_size / 2` above the anchor.
    """
    centre_x, bottom = caption_anchor(safe)
    cx = round(centre_x)
    size = line.font_size
    pad = settings.box_padding_px
    ascent = metrics.ascent_px(size)
    descent = metrics.descent_px(size)
    wanted_baseline = bottom - pad - descent
    y = round(wanted_baseline - ascent + size / 2)
    baseline = y - size / 2 + ascent
    half = line.width / 2
    box = Rect(
        cx - half - pad,
        baseline - metrics.cap_px(size) - pad,
        cx + half + pad,
        baseline + descent + pad,
    )
    return LinePlacement(cx, y, box)


def rounded_box(rect: Rect, radius: float = BOX_RADIUS_PX) -> str:
    """ASS drawing commands (m, l, b; integer coordinates) for a rounded rectangle: straight
    sides and four Bezier corners. The radius is capped at half the box's height and width."""
    r = min(radius, (rect.y2 - rect.y1) / 2, (rect.x2 - rect.x1) / 2)
    c = r * BEZIER_CORNER
    x1, y1, x2, y2 = rect.x1, rect.y1, rect.x2, rect.y2
    path = [
        ("m", [x1 + r, y1]),
        ("l", [x2 - r, y1]),
        ("b", [x2 - r + c, y1, x2, y1 + r - c, x2, y1 + r]),
        ("l", [x2, y2 - r]),
        ("b", [x2, y2 - r + c, x2 - r + c, y2, x2 - r, y2]),
        ("l", [x1 + r, y2]),
        ("b", [x1 + r - c, y2, x1, y2 - r + c, x1, y2 - r]),
        ("l", [x1, y1 + r]),
        ("b", [x1, y1 + r - c, x1 + r - c, y1, x1 + r, y1]),
    ]
    return " ".join(f"{cmd} " + " ".join(str(round(v)) for v in values) for cmd, values in path)


def line_events(
    line: KaraokeLine,
    placement: LinePlacement,
    settings: KaraokeSettings,
    metrics: FontMetrics,
) -> list[AssEvent]:
    """The two events of a line: the box (layer 0, style Box) and the karaoke text (layer 1,
    style Cap), both from the line's start to its end."""
    box = AssEvent(
        LAYER_BOX,
        line.start,
        line.end,
        BOX_STYLE,
        f"{{\\pos(0,0)\\p1}}{rounded_box(placement.box)}{{\\p0}}",
    )
    k_values = line_k_values(line)
    size_tag = f"\\fs{line.font_size}" if line.font_size != settings.font_size else ""
    first = f"{{\\pos({placement.cx},{placement.y}){size_tag}\\k{k_values[0]}}}"
    space_fsp = space_spacing_px(
        metrics, line.font_size, settings.word_gap_em, settings.letter_spacing_px
    )
    body = spaced_text(
        [w.text for w in line.words], space_fsp, settings.letter_spacing_px, k_values
    )
    return [box, AssEvent(LAYER_TEXT, line.start, line.end, CAP_STYLE, first + body)]


def build_karaoke(
    words: Sequence[CaptionWord],
    settings: KaraokeSettings,
    metrics: FontMetrics,
    safe: Rect,
    *,
    clip_seconds: float,
    offset_seconds: float = 0.0,
) -> KaraokeResult:
    """Lines, placement and events for a clip's words. Pass the events (plus any extra ones) to
    `build_ass_v2`. `boxes` are the rectangles to check against the safe area."""
    built = karaoke_lines(
        words, settings, metrics, clip_seconds=clip_seconds, offset_seconds=offset_seconds
    )
    events: list[AssEvent] = []
    boxes: list[Rect] = []
    for line in built.lines:
        placement = place_line(line, settings, metrics, safe)
        events.extend(line_events(line, placement, settings, metrics))
        boxes.append(placement.box)
    notes: list[str] = []
    if built.words_shrunk:
        notes.append("caption_word_shrunk")
    if built.words_truncated:
        notes.append("caption_word_truncated")
    return KaraokeResult(
        events=events,
        lines=len(built.lines),
        max_words=max((len(line.words) for line in built.lines), default=0),
        boxes=boxes,
        notes=notes,
        words_shrunk=built.words_shrunk,
        words_truncated=built.words_truncated,
    )


def build_ass_v2(
    events: Sequence[AssEvent],
    extra_events: Sequence[AssEvent] = (),
    *,
    font: str,
    settings: KaraokeSettings,
    width: int,
    height: int,
) -> str:
    """A complete ASS file for the box_karaoke style: styles Cap (spoken colour as Primary,
    upcoming as Secondary, which is what `\\k` karaoke needs), Hook (white) and Box (black with
    the box opacity), then `events` followed by `extra_events` (the hook's, from a later commit).
    """
    font_name = _font_name(font)
    spacing = fmt_num(settings.letter_spacing_px)
    alpha = round((1 - settings.box_opacity) * 255)
    spoken = style_color(settings.spoken_color)
    upcoming = style_color(settings.upcoming_color)
    white = style_color("#FFFFFF")
    clear = ASS_BLACK
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: {CAP_STYLE},{font_name},{settings.font_size},{spoken},{upcoming},{clear},"
        f"{clear},-1,0,0,0,100,100,{spacing},0,1,0,0,5,0,0,0,1",
        f"Style: {HOOK_STYLE},{font_name},{settings.font_size},{white},{white},{clear},"
        f"{clear},-1,0,0,0,100,100,{spacing},0,1,0,0,5,0,0,0,1",
        f"Style: {BOX_STYLE},{font_name},20,{style_color('#000000', alpha)},"
        f"{style_color('#000000', alpha)},{clear},{clear},0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lines.extend(
        f"Dialogue: {e.layer},{ass_time(e.start)},{ass_time(e.end)},{e.style},,0,0,0,,{e.text}"
        for e in (*events, *extra_events)
    )
    return "\n".join(lines) + "\n"
