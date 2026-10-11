"""Word-group captions as an ASS subtitle file. Pure functions: no I/O, no ffmpeg.

Transcript words are untrusted data. Before a word reaches the file, `{`, `}` and `\\` are removed
(so a word can never become an override tag) and control characters become spaces.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from clipmachine.models import HEX_COLOR_RE, TranscriptSegment

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
    groups: list[list[CaptionWord]], highlight_color: str, clip_seconds: float
) -> list[CaptionEvent]:
    """One event per word: its whole group on screen, that word in the highlight colour.

    An event runs from the word's start to the next word's start, also across groups. When the
    silence before the next word is GAP_FILL_SECONDS or more, it ends at the word's own end
    instead, so captions disappear in real pauses. The clip's last word runs to its end.
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
    return events


def ass_time(seconds: float) -> str:
    """H:MM:SS.cc, rounded to centiseconds."""
    total = max(round(seconds * 100), 0)
    hours, rest = divmod(total, 360_000)
    minutes, rest = divmod(rest, 6_000)
    secs, centis = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


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
    font_name = re.sub(r"[,\r\n]", " ", font).strip() or "Montserrat"
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
