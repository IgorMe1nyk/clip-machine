import math
import re
from dataclasses import replace
from pathlib import Path

import pytest

from clipmachine.models import CaptionsConfig, TranscriptSegment, TranscriptWord
from clipmachine.utils.captions import (
    HOOK_SIZE_FRACTIONS,
    AssEvent,
    CaptionWord,
    HookResult,
    KaraokeLines,
    KaraokeResult,
    KaraokeSettings,
    ass_color,
    ass_time,
    build_ass,
    build_ass_v2,
    build_events,
    build_hook,
    build_karaoke,
    caption_top,
    clean_word,
    clip_words,
    fmt_num,
    group_words,
    karaoke_lines,
    rounded_box,
    space_spacing_px,
    spaced_width,
)
from clipmachine.utils.fontmetrics import FontMetrics, load_font
from clipmachine.utils.safezone import Rect, outside_count, safe_rect


def word(text: str, start: float, end: float) -> TranscriptWord:
    return TranscriptWord(start=start, end=end, word=text, probability=0.9)


def segment(*words: TranscriptWord) -> TranscriptSegment:
    return TranscriptSegment(
        id=0,
        start=words[0].start,
        end=words[-1].end,
        text="",
        avg_logprob=0.0,
        no_speech_prob=0.0,
        words=list(words),
    )


def cw(text: str, start: float, end: float) -> CaptionWord:
    return CaptionWord(text, start, end)


def seq(texts: list[str], step: float = 0.5) -> list[CaptionWord]:
    return [cw(t, i * step, i * step + step * 0.8) for i, t in enumerate(texts)]


# --- cleaning and escaping ---------------------------------------------------------------------


def test_leading_and_trailing_spaces_are_stripped() -> None:
    assert clean_word("  alpha ") == "alpha"
    assert clean_word("\talpha\n") == "alpha"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("{\\an8}hi", "an8hi"),  # braces and backslash removed: no override tag survives
        ("a{b}c", "abc"),
        ("back\\Nslash", "backNslash"),
        ("line\nbreak", "line break"),
        ("x\r\ny", "x y"),
        ("\\", ""),
        ("{}", ""),
        ("tab\tsep", "tab sep"),
    ],
)
def test_untrusted_text_cannot_become_an_ass_tag(raw: str, expected: str) -> None:
    out = clean_word(raw)
    assert out == expected
    assert not any(c in out for c in "{}\\\n\r")


def test_events_never_contain_tags_from_words() -> None:
    words = clip_words([segment(word("{\\fs500}big", 0, 1), word("ok", 1, 2))], 0, 5)
    events = build_events(group_words(words, 3), "#FFD400", 5.0)
    for event in events:
        # only our own colour and reset tags remain
        stripped = event.text.replace("{\\c&H00D4FF&}", "").replace("{\\r}", "")
        assert "{" not in stripped and "\\" not in stripped


# --- words in the clip, shifting and clamping --------------------------------------------------


def test_words_are_shifted_so_the_clip_starts_at_zero() -> None:
    words = clip_words([segment(word(" a", 10.0, 10.4), word(" b", 10.5, 11.0))], 10.0, 12.0)
    assert [(w.text, w.start, w.end) for w in words] == [("a", 0.0, 0.4), ("b", 0.5, 1.0)]


def test_words_partly_outside_are_clamped_and_words_outside_dropped() -> None:
    words = clip_words(
        [
            segment(
                word("before", 8.0, 9.0),  # ends before the clip
                word("early", 9.8, 10.3),  # starts before: clamped to 0
                word("mid", 11.0, 11.5),
                word("late", 11.8, 12.4),  # ends after: clamped to the duration
                word("after", 12.5, 13.0),
                word("edge", 12.0, 12.2),  # starts exactly at the end: outside
            )
        ],
        10.0,
        12.0,
    )
    assert [(w.text, round(w.start, 2), round(w.end, 2)) for w in words] == [
        ("early", 0.0, 0.3),
        ("mid", 1.0, 1.5),
        ("late", 1.8, 2.0),
    ]


def test_empty_after_cleaning_is_dropped() -> None:
    words = clip_words([segment(word(" ", 1, 2), word("{}", 2, 3), word("ok", 3, 4))], 0, 10)
    assert [w.text for w in words] == ["ok"]


def test_words_from_all_segments_in_time_order() -> None:
    words = clip_words([segment(word("c", 2, 3)), segment(word("a", 0, 1), word("b", 1, 2))], 0, 10)
    assert [w.text for w in words] == ["a", "b", "c"]


# --- grouping ---------------------------------------------------------------------------------


def test_groups_of_n_words() -> None:
    groups = group_words(seq(list("abcdefg")), 3)
    assert [[w.text for w in g] for g in groups] == [["a", "b", "c"], ["d", "e", "f"], ["g"]]


def test_group_ends_after_sentence_punctuation() -> None:
    groups = group_words(seq(["Hi.", "How", "are", "you?", "Fine!", "ok"]), 3)
    assert [[w.text for w in g] for g in groups] == [
        ["Hi."],
        ["How", "are", "you?"],
        ["Fine!"],
        ["ok"],
    ]


def test_one_word_per_line() -> None:
    assert len(group_words(seq(["a", "b", "c"]), 1)) == 3


# --- colour and time --------------------------------------------------------------------------


def test_ass_colour_is_bgr() -> None:
    assert ass_color("#FFD400") == "&H00D4FF&"
    assert ass_color("#102030") == "&H302010&"
    assert ass_color("#abcdef") == "&HEFCDAB&"
    with pytest.raises(ValueError, match="#RRGGBB"):
        ass_color("FFD400")


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0:00:00.00"), (1.234, "0:00:01.23"), (59.999, "0:01:00.00"), (3725.5, "1:02:05.50")],
)
def test_ass_time(seconds: float, expected: str) -> None:
    assert ass_time(seconds) == expected


# --- events: highlight and gap filling --------------------------------------------------------


def test_one_event_per_word_with_that_word_highlighted() -> None:
    groups = group_words([cw("one", 0.0, 0.4), cw("two", 0.5, 0.9), cw("three", 1.0, 1.5)], 3)
    events = build_events(groups, "#FFD400", 2.0)
    assert [e.text for e in events] == [
        "{\\c&H00D4FF&}one{\\r} two three",
        "one {\\c&H00D4FF&}two{\\r} three",
        "one two {\\c&H00D4FF&}three{\\r}",
    ]
    # runs to the next word's start; the last word runs to its end
    assert [(e.start, e.end) for e in events] == [(0.0, 0.5), (0.5, 1.0), (1.0, 1.5)]


def test_small_gaps_are_filled_even_across_groups() -> None:
    words = [cw("a", 0.0, 0.2), cw("b", 0.4, 0.6), cw("c", 0.8, 1.0), cw("d", 1.3, 1.5)]
    events = build_events(group_words(words, 3), "#FFD400", 3.0)
    assert [e.end for e in events] == [0.4, 0.8, 1.3, 1.5]  # "c" is held to d's start


def test_a_pause_of_half_a_second_or_more_is_left_empty() -> None:
    words = [cw("a", 0.0, 0.2), cw("b", 0.7, 0.9)]  # gap is exactly 0.5 s
    events = build_events(group_words(words, 3), "#FFD400", 3.0)
    assert (events[0].start, events[0].end) == (0.0, 0.2)
    words = [cw("a", 0.0, 0.2), cw("b", 0.69, 0.9)]  # 0.49 s: filled
    events = build_events(group_words(words, 3), "#FFD400", 3.0)
    assert events[0].end == 0.69


def test_overlapping_words_do_not_overlap_events() -> None:
    words = [cw("a", 0.0, 0.6), cw("b", 0.4, 0.9)]
    events = build_events(group_words(words, 3), "#FFD400", 3.0)
    assert events[0].end == 0.4


def test_last_word_gets_a_minimum_length_inside_the_clip() -> None:
    events = build_events([[cw("a", 1.0, 1.0)]], "#FFD400", 5.0)
    assert (events[0].start, events[0].end) == (1.0, 1.1)
    events = build_events([[cw("a", 4.98, 4.98)]], "#FFD400", 5.0)  # near the end: clamped
    assert events[0].end == 5.0


def test_no_words_no_events() -> None:
    assert build_events([], "#FFD400", 5.0) == []


# --- the file ---------------------------------------------------------------------------------


def test_build_ass_layout() -> None:
    events = build_events(group_words([cw("hi", 0.0, 0.5)], 3), "#FFD400", 2.0)
    text = build_ass(events, font="Montserrat", font_size=80, margin_v=350, width=1080, height=1920)
    lines = text.splitlines()
    assert "PlayResX: 1080" in lines and "PlayResY: 1920" in lines
    (style,) = [line for line in lines if line.startswith("Style:")]
    fields = style.removeprefix("Style: ").split(",")
    assert fields[1] == "Montserrat" and fields[2] == "80"
    assert fields[3] == "&H00FFFFFF"  # white
    assert fields[7] == "-1"  # bold
    assert fields[15] == "1" and fields[16] == "4"  # outline, no box
    assert fields[18] == "2"  # bottom centre
    assert fields[21] == "350"
    (dialogue,) = [line for line in lines if line.startswith("Dialogue:")]
    assert dialogue == "Dialogue: 0,0:00:00.00,0:00:00.50,Clip,,0,0,0,,{\\c&H00D4FF&}hi{\\r}"
    assert text.endswith("\n") and "\r" not in text


def test_font_name_cannot_break_the_style_line() -> None:
    text = build_ass([], font="Bad,\nName", font_size=80, margin_v=350, width=1080, height=1920)
    (style,) = [line for line in text.splitlines() if line.startswith("Style:")]
    assert len(style.split(",")) == 23


# --- offset (Brief 6.1) -----------------------------------------------------------------------

HIGHLIGHT_A = r"{\c&H00D4FF&}a{\r}"


def offset_events(offset: float) -> list[tuple[float, float]]:
    # gaps of 0.4 s are filled, so each event runs to the next word's start
    words = [cw("a", 1.0, 1.4), cw("b", 1.8, 2.2), cw("c", 2.6, 3.0)]
    events = build_events(group_words(words, 3), "#FFD400", 4.0, offset)
    return [(e.start, e.end) for e in events]


def test_offset_zero_is_identical_to_no_offset() -> None:
    words = [cw("a", 1.0, 1.4), cw("b", 1.8, 2.2), cw("c", 2.6, 3.0)]
    groups = group_words(words, 3)
    assert build_events(groups, "#FFD400", 4.0, 0.0) == build_events(groups, "#FFD400", 4.0)
    assert offset_events(0.0) == [(1.0, 1.8), (1.8, 2.6), (2.6, 3.0)]


def test_positive_offset_moves_starts_and_ends_later() -> None:
    assert offset_events(0.15) == [(1.15, 1.95), (1.95, 2.75), (2.75, 3.15)]


def test_negative_offset_moves_starts_and_ends_earlier() -> None:
    assert offset_events(-0.25) == [(0.75, 1.55), (1.55, 2.35), (2.35, 2.75)]


def test_offset_clamps_at_zero_and_drops_events_left_empty() -> None:
    words = [cw("a", 0.1, 0.3), cw("b", 0.3, 0.6), cw("c", 1.0, 1.4)]
    events = build_events(group_words(words, 3), "#FFD400", 4.0, -0.5)
    # a: 0.1-0.3 -> -0.4..-0.2 -> empty, dropped; b: 0.3-1.0 -> 0.0 (clamped)..0.5; c: 0.5-0.9
    assert [(e.start, e.end) for e in events] == [(0.0, 0.5), (0.5, 0.9)]
    assert all(HIGHLIGHT_A not in e.text for e in events)


def test_offset_clamps_at_the_clip_duration() -> None:
    words = [cw("a", 1.0, 1.4), cw("b", 3.5, 3.9)]
    events = build_events(group_words(words, 3), "#FFD400", 4.0, 0.3)
    assert [(e.start, e.end) for e in events] == [(1.3, 1.7), (3.8, 4.0)]


def test_offset_drops_events_pushed_past_the_end() -> None:
    words = [cw("a", 1.0, 1.4), cw("b", 3.5, 3.9)]
    events = build_events(group_words(words, 3), "#FFD400", 4.0, 0.6)
    assert [(e.start, e.end) for e in events] == [(1.6, 2.0)]  # b would start at 4.1 > 4.0


def test_offset_keeps_millisecond_rounding() -> None:
    words = [cw("a", 0.1, 0.2)]
    (event,) = build_events(group_words(words, 3), "#FFD400", 4.0, 0.1)
    assert (event.start, event.end) == (0.2, 0.3)  # not 0.30000000000000004


# --- box_karaoke (Brief R1); synthetic words, real Montserrat metrics ---------------------------

FONT_PATH = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "Montserrat-Bold.ttf"
SAFE = safe_rect(1080, 1920, 290, 672, 120, 192)
SETTINGS = KaraokeSettings(
    font_size=64,
    max_words=4,
    max_width_px=760,
    word_gap_em=0.45,
    letter_spacing_px=1.0,
    spoken_color="#FFD400",
    upcoming_color="#FFFFFF",
    box_opacity=0.75,
    box_padding_px=18,
)


@pytest.fixture(scope="module")
def metrics() -> FontMetrics:
    return load_font(FONT_PATH)


def karaoke(
    metrics: FontMetrics,
    words: list[CaptionWord],
    *,
    clip: float = 30.0,
    offset: float = 0.0,
    settings: KaraokeSettings = SETTINGS,
) -> KaraokeResult:
    return build_karaoke(words, settings, metrics, SAFE, clip_seconds=clip, offset_seconds=offset)


def lines_of(
    metrics: FontMetrics,
    words: list[CaptionWord],
    *,
    clip: float = 30.0,
    offset: float = 0.0,
    settings: KaraokeSettings = SETTINGS,
) -> KaraokeLines:
    return karaoke_lines(words, settings, metrics, clip_seconds=clip, offset_seconds=offset)


def cap_events(result: KaraokeResult) -> list[AssEvent]:
    return [e for e in result.events if e.style == "Cap"]


def k_values(text: str) -> list[int]:
    return [int(v) for v in re.findall(r"\\k(\d+)", text)]


def dialogue_texts(ass: str) -> list[str]:
    return [line.split(",", 9)[9] for line in ass.split("\n") if line.startswith("Dialogue:")]


@pytest.mark.parametrize("offset", [0.0, 0.15, -0.002, -0.1])
def test_karaoke_each_word_switches_at_its_own_start(metrics: FontMetrics, offset: float) -> None:
    starts = [0.004, 0.337, 0.999, 1.5049]
    words = [cw(f"w{i}", s, s + 0.2) for i, s in enumerate(starts)]
    shifted = lines_of(metrics, words, offset=offset).lines[0].words
    (cap,) = cap_events(karaoke(metrics, words, offset=offset))
    values = k_values(cap.text)
    assert len(values) == len(shifted) == 4
    first_cs = round(cap.start * 100)
    for index, spoken in enumerate(shifted):
        assert first_cs + sum(values[:index]) == max(round(spoken.start * 100), 0)


def test_karaoke_timing_holds_across_many_lines(metrics: FontMetrics) -> None:
    words = [cw(f"w{i}", 0.1237 * i, 0.1237 * i + 0.09) for i in range(30)]
    result = karaoke(metrics, words)
    built = lines_of(metrics, words)
    assert len(built.lines) == len(cap_events(result)) > 5
    for line, cap in zip(built.lines, cap_events(result), strict=True):
        values = k_values(cap.text)
        for index, spoken in enumerate(line.words):
            assert round(cap.start * 100) + sum(values[:index]) == round(spoken.start * 100)
        # the last value reaches the end of the line, never past it
        assert round(cap.start * 100) + sum(values) == round(cap.end * 100)


def test_the_text_event_and_box_event_share_their_times(metrics: FontMetrics) -> None:
    result = karaoke(metrics, seq(["alpha", "beta"]))
    box, text = result.events
    assert (box.layer, box.style, text.layer, text.style) == (0, "Box", 1, "Cap")
    assert (box.start, box.end) == (text.start, text.end)


# --- line ends ----------------------------------------------------------------------------------

TWO_WORDS = replace(SETTINGS, max_words=2)


def test_a_gap_under_half_a_second_holds_the_line_to_the_next_one(metrics: FontMetrics) -> None:
    words = [cw("a", 0.0, 0.2), cw("b", 0.3, 0.5), cw("c", 0.99, 1.2), cw("d", 1.3, 1.5)]
    first, second = lines_of(metrics, words, settings=TWO_WORDS).lines
    assert (first.start, first.end) == (0.0, 0.99)  # gap 0.49
    assert (second.start, second.end) == (0.99, 1.5)


def test_a_gap_of_half_a_second_ends_the_line_at_its_last_word(metrics: FontMetrics) -> None:
    words = [cw("a", 0.0, 0.2), cw("b", 0.3, 0.5), cw("c", 1.0, 1.2), cw("d", 1.3, 1.5)]
    first, _ = lines_of(metrics, words, settings=TWO_WORDS).lines
    assert first.end == 0.5  # gap is exactly 0.5


def test_the_last_line_lasts_a_minimum_time_and_stays_in_the_clip(metrics: FontMetrics) -> None:
    words = [cw("a", 1.0, 1.02)]
    (line,) = lines_of(metrics, words, clip=5.0).lines
    assert (line.start, line.end) == (1.0, 1.1)
    words = [cw("a", 4.98, 4.99)]
    (line,) = lines_of(metrics, words, clip=5.0).lines
    assert line.end == 5.0


def test_no_words_no_lines(metrics: FontMetrics) -> None:
    result = karaoke(metrics, [])
    assert (result.events, result.lines, result.max_words, result.boxes) == ([], 0, 0, [])
    assert lines_of(metrics, []).lines == []


def test_offset_moves_words_clamps_and_drops_the_ones_left_empty(metrics: FontMetrics) -> None:
    words = [cw("a", 0.1, 0.3), cw("b", 0.3, 0.6), cw("c", 4.9, 5.0)]
    lines = lines_of(metrics, words, clip=5.0, offset=-0.5).lines
    assert [w.text for line in lines for w in line.words] == ["b", "c"]  # a is left empty
    assert (lines[0].start, lines[0].end) == (0.0, 0.1)
    late = lines_of(metrics, [cw("a", 1.0, 1.4), cw("b", 4.5, 4.9)], clip=5.0, offset=0.3).lines
    assert [w.text for line in late for w in line.words] == ["a", "b"]
    assert (late[-1].start, late[-1].end) == (4.8, 5.0)  # b's end is clamped to the clip
    pushed = lines_of(metrics, [cw("a", 1.0, 1.4), cw("b", 4.8, 4.9)], clip=5.0, offset=0.3).lines
    assert [w.text for line in pushed for w in line.words] == ["a"]  # b would start past the end


def test_a_zero_length_word_is_kept_and_coloured_at_its_start(metrics: FontMetrics) -> None:
    words = [cw("alpha", 1.0, 1.3), cw("beta", 1.5, 1.5), cw("gamma", 1.8, 2.1)]
    (line,) = lines_of(metrics, words).lines
    assert [w.text for w in line.words] == ["alpha", "beta", "gamma"]
    (cap,) = cap_events(karaoke(metrics, words))
    values = k_values(cap.text)
    assert len(values) == 3
    for index, spoken in enumerate(line.words):
        assert round(cap.start * 100) + sum(values[:index]) == round(spoken.start * 100)


def test_a_zero_length_word_alone_takes_its_end_from_the_rules(metrics: FontMetrics) -> None:
    # last line: the minimum length; earlier line: the next line's start
    (last,) = lines_of(metrics, [cw("a", 2.0, 2.0)], clip=5.0).lines
    assert (last.start, last.end) == (2.0, 2.1)
    first, second = lines_of(
        metrics, [cw("a", 1.0, 1.0), cw("b", 1.3, 1.6)], settings=replace(SETTINGS, max_words=1)
    ).lines
    assert (first.end, second.start) == (1.3, 1.3)
    # a line that would start and end at the same time is still dropped
    lines = lines_of(
        metrics, [cw("a", 1.0, 1.0), cw("b", 1.0, 1.4)], settings=replace(SETTINGS, max_words=1)
    ).lines
    assert [w.text for line in lines for w in line.words] == ["b"]


def test_a_word_pushed_wholly_before_zero_is_dropped_but_a_zero_length_one_at_zero_is_not(
    metrics: FontMetrics,
) -> None:
    words = [cw("a", 0.1, 0.1), cw("b", 0.2, 0.4), cw("c", 0.6, 0.9)]
    kept = [w.text for line in lines_of(metrics, words, offset=-0.3).lines for w in line.words]
    assert kept == ["b", "c"]  # a: -0.2..-0.2 is before the clip
    kept = [w.text for line in lines_of(metrics, [cw("a", 0.0, 0.0)]).lines for w in line.words]
    assert kept == ["a"]


def test_a_word_pushed_to_or_past_the_clip_end_is_dropped(metrics: FontMetrics) -> None:
    for start in (4.9, 4.95):  # offset 0.1 / 0.05: lands exactly on 5.0
        words = [cw("a", 1.0, 1.4), cw("b", start, start)]
        offset = round(5.0 - start, 2)
        kept = [
            w.text
            for line in lines_of(metrics, words, clip=5.0, offset=offset).lines
            for w in line.words
        ]
        assert kept == ["a"]
    words = [cw("a", 1.0, 1.4), cw("b", 4.8, 4.8)]
    kept = [
        w.text for line in lines_of(metrics, words, clip=5.0, offset=0.1).lines for w in line.words
    ]
    assert kept == ["a", "b"]  # 4.9: still inside


# --- grouping -----------------------------------------------------------------------------------


def test_a_line_never_has_more_than_max_words(metrics: FontMetrics) -> None:
    for per_line in (1, 2, 3, 4):
        settings = replace(SETTINGS, max_words=per_line)
        built = lines_of(metrics, seq(["ab"] * 11, 0.3), settings=settings)
        assert max(len(line.words) for line in built.lines) <= per_line
        assert sum(len(line.words) for line in built.lines) == 11


def test_a_sentence_end_breaks_the_line(metrics: FontMetrics) -> None:
    built = lines_of(metrics, seq(["Hi.", "there", "you", "all"], 0.3))
    assert [[w.text for w in line.words] for line in built.lines] == [
        ["Hi."],
        ["there", "you", "all"],
    ]


def test_a_width_limit_forces_a_split(metrics: FontMetrics) -> None:
    settings = replace(SETTINGS, max_width_px=520)  # 484 px of text
    built = lines_of(metrics, seq(["alphabet"] * 4, 0.3), settings=settings)
    assert max(len(line.words) for line in built.lines) < 4  # max_words alone would allow 4
    assert all(line.width <= settings.text_limit_px for line in built.lines)
    assert sum(len(line.words) for line in built.lines) == 4


def test_a_pause_inside_a_group_splits_it(metrics: FontMetrics) -> None:
    words = [cw("alpha", 0.0, 0.3), cw("beta", 0.8, 1.1)]  # 0.5 s of silence
    assert len(lines_of(metrics, words).lines) == 2
    words = [cw("alpha", 0.0, 0.3), cw("beta", 0.79, 1.1)]  # 0.49 s
    assert len(lines_of(metrics, words).lines) == 1


def test_an_overlong_word_is_shrunk_and_its_box_still_fits(metrics: FontMetrics) -> None:
    result = karaoke(metrics, [cw("m" * 25, 0.0, 1.0)])
    built = lines_of(metrics, [cw("m" * 25, 0.0, 1.0)])
    (line,) = built.lines
    assert 32 <= line.font_size < 64
    assert (built.words_shrunk, built.words_truncated) == (1, 0)
    assert result.notes == ["caption_word_shrunk"]
    assert line.width <= SETTINGS.text_limit_px
    assert outside_count(result.boxes, SAFE) == 0
    assert result.boxes[0].x2 - result.boxes[0].x1 <= SETTINGS.max_width_px + 1
    assert f"\\fs{line.font_size}\\k" in cap_events(result)[0].text


def test_a_normal_line_has_no_font_size_override(metrics: FontMetrics) -> None:
    (cap,) = cap_events(karaoke(metrics, seq(["alpha", "beta"])))
    assert r"\fs" not in cap.text.replace(r"\fsp", "")


def test_a_400_character_word_is_truncated_and_fits(metrics: FontMetrics) -> None:
    result = karaoke(metrics, [cw("w" * 400, 0.0, 1.0)])
    built = lines_of(metrics, [cw("w" * 400, 0.0, 1.0)])
    (line,) = built.lines
    (shown,) = line.words
    assert shown.text.endswith("\u2026") and len(shown.text) < 400
    assert line.font_size == 32
    assert metrics.text_width(shown.text, line.font_size, 1.0) <= SETTINGS.text_limit_px
    assert (built.words_shrunk, built.words_truncated) == (0, 1)
    assert result.notes == ["caption_word_truncated"]
    assert outside_count(result.boxes, SAFE) == 0


# --- spacing ------------------------------------------------------------------------------------

FSP_BLOCK = re.compile(r"\{\\fsp([0-9.]+)\}")


def test_the_space_gets_the_extra_spacing_that_makes_the_configured_gap(
    metrics: FontMetrics,
) -> None:
    (cap,) = cap_events(karaoke(metrics, seq(["alpha", "beta", "gamma"], 0.3)))
    values = [float(v) for v in FSP_BLOCK.findall(cap.text)]
    expected = round(0.45 * 64 - metrics.space_px(64) - 1.0, 2)
    assert values == [expected, expected]
    assert expected == pytest.approx(0.45 * 64 - metrics.space_px(64) - 1.0, abs=0.005)
    assert space_spacing_px(metrics, 64, 0.45, 1.0) == pytest.approx(expected, abs=0.005)
    assert r"{\fsp1\k" in cap.text  # the letter spacing comes back after the space


def test_a_wider_word_gap_changes_the_tag_and_the_width(metrics: FontMetrics) -> None:
    words = seq(["alpha", "beta"], 0.3)
    narrow = cap_events(karaoke(metrics, words))[0].text
    wide_settings = replace(SETTINGS, word_gap_em=0.9)
    wide = cap_events(karaoke(metrics, words, settings=wide_settings))[0].text
    assert FSP_BLOCK.findall(narrow) != FSP_BLOCK.findall(wide)
    assert lines_of(metrics, words, settings=wide_settings).lines[0].width > (
        lines_of(metrics, words).lines[0].width
    )


def test_a_gap_smaller_than_the_space_adds_nothing(metrics: FontMetrics) -> None:
    assert space_spacing_px(metrics, 64, 0.1, 1.0) == 0.0


def test_width_of_spaced_words_is_the_glyph_sum_plus_the_gaps(metrics: FontMetrics) -> None:
    texts = ["alpha", "beta", "gamma"]
    width = spaced_width(metrics, texts, 64, 0.45, 1.0)
    glyphs = sum(metrics.text_width(t, 64, 1.0) for t in texts)
    assert width == pytest.approx(glyphs + 2 * (0.45 * 64 - 1.0))
    assert spaced_width(metrics, [], 64, 0.45, 1.0) == 0.0


@pytest.mark.parametrize(
    ("value", "text"), [(1.0, "1"), (16.2, "16.2"), (0.0, "0"), (12.345, "12.35"), (-0.001, "0")]
)
def test_numbers_have_at_most_two_decimals(value: float, text: str) -> None:
    assert fmt_num(value) == text


# --- escaping -----------------------------------------------------------------------------------

OUR_TAGS = [
    re.compile(r"\{\\pos\(0,0\)\\p1\}[mlb0-9 ]+\{\\p0\}"),
    re.compile(r"\{\\pos\(\d+,\d+\)(?:\\fs\d+)?\\k\d+\}"),
    re.compile(r"\{\\fsp\d+(?:\.\d{1,2})?\}"),
    re.compile(r"\{\\fsp\d+(?:\.\d{1,2})?\\k\d+\}"),
]


def test_hostile_words_cannot_add_tags_or_lines_to_the_file(metrics: FontMetrics) -> None:
    raw = ["{\\fs500}big", "a\\Nb", "line1\nline2", "}{", "x\r\ny", "\\", "ok{\\p1}", "end"]
    words = clip_words(
        [segment(*[word(t, i * 0.3, i * 0.3 + 0.2) for i, t in enumerate(raw)])], 0, 9
    )
    result = karaoke(metrics, words, clip=9.0)
    ass = build_ass_v2(result.events, font="Montserrat", settings=SETTINGS, width=1080, height=1920)
    assert "\r" not in ass
    texts = dialogue_texts(ass)
    assert len(texts) == len(result.events) > 0
    for text in texts:
        for pattern in OUR_TAGS:
            text = pattern.sub("", text)
        assert not any(c in text for c in "{}\\\n\r")


# --- the box ------------------------------------------------------------------------------------


def test_the_box_is_the_text_plus_padding(metrics: FontMetrics) -> None:
    words = seq(["alpha", "beta", "gamma"], 0.3)
    result = karaoke(metrics, words)
    (line,) = lines_of(metrics, words).lines
    (box,) = result.boxes
    assert box.x2 - box.x1 == pytest.approx(line.width + 36)
    assert (box.x1 + box.x2) / 2 == 504
    assert abs(box.y2 - 1236) <= 1  # the requested bottom edge
    assert box.y2 - box.y1 > 36 + metrics.cap_px(64)


def test_the_box_drawing_has_integer_coordinates_only(metrics: FontMetrics) -> None:
    box_event = karaoke(metrics, seq(["alpha", "beta"])).events[0]
    match = re.fullmatch(r"\{\\pos\(0,0\)\\p1\}(.*)\{\\p0\}", box_event.text)
    assert match is not None
    drawing = match.group(1)
    assert re.fullmatch(r"(?:[mlb]|-?\d+)(?: (?:[mlb]|-?\d+))*", drawing)
    assert drawing.split()[0] == "m" and drawing.count(" b ") + drawing.startswith("b ") == 4


def test_rounded_box_radius_is_capped_by_the_height() -> None:
    flat = rounded_box(Rect(0, 0, 200, 20))
    assert flat.startswith("m 10 0 l 190 0")  # radius 10, not 20
    assert rounded_box(Rect(0, 0, 200, 100)).startswith("m 20 0 l 180 0")


def test_the_box_style_alpha_follows_the_opacity() -> None:
    def box_style(opacity: float) -> list[str]:
        settings = replace(SETTINGS, box_opacity=opacity)
        ass = build_ass_v2([], font="Montserrat", settings=settings, width=1080, height=1920)
        (style,) = [x for x in ass.split("\n") if x.startswith("Style: Box")]
        return style.removeprefix("Style: ").split(",")

    assert box_style(0.75)[3] == "&H40000000"
    assert box_style(1.0)[3] == "&H00000000"
    assert box_style(0.0)[3] == "&HFF000000"
    assert box_style(0.75)[18] == "7"  # top left, so the drawing is positioned absolutely


# --- the safe zone ------------------------------------------------------------------------------


def test_captions_for_varied_words_stay_inside_the_safe_area(metrics: FontMetrics) -> None:
    texts = ["I", "WWWW", "illicit", "microphone", "to", "Extraordinarily", "go.", "mmmmmm"] * 6
    words = [cw(t, i * 0.31, i * 0.31 + 0.25) for i, t in enumerate(texts)]
    result = karaoke(metrics, words, clip=40.0)
    assert result.lines > 10 and result.max_words <= 4
    assert outside_count(result.boxes, SAFE) == 0
    for box in result.boxes:
        assert (box.x1 + box.x2) / 2 == 504
        baseline = box.y2 - SETTINGS.box_padding_px - metrics.descent_px(64)
        assert 1150 <= baseline <= 1230
        assert box.x2 - box.x1 <= SETTINGS.max_width_px + 1


def test_a_box_that_misses_the_safe_area_is_counted() -> None:
    assert outside_count([Rect(100, 1100, 700, 1236)], SAFE) == 1


# --- the file -----------------------------------------------------------------------------------


def test_build_ass_v2_layout(metrics: FontMetrics) -> None:
    result = karaoke(metrics, seq(["alpha", "beta"]))
    ass = build_ass_v2(result.events, font="Montserrat", settings=SETTINGS, width=1080, height=1920)
    lines = ass.split("\n")
    assert "PlayResX: 1080" in lines and "PlayResY: 1920" in lines
    assert "WrapStyle: 2" in lines
    styles = [
        line.removeprefix("Style: ").split(",") for line in lines if line.startswith("Style:")
    ]
    assert [s[0] for s in styles] == ["Cap", "Hook", "Box"]
    assert all(len(s) == 23 for s in styles)
    cap, hook, box = styles
    assert (cap[1], cap[2]) == ("Montserrat", "64")
    assert (cap[3], cap[4]) == ("&H0000D4FF", "&H00FFFFFF")  # spoken yellow, upcoming white
    assert (cap[7], cap[13], cap[15], cap[16], cap[17], cap[18]) == ("-1", "1", "1", "0", "0", "5")
    assert cap[19:22] == ["0", "0", "0"]
    assert (hook[3], hook[4]) == ("&H00FFFFFF", "&H00FFFFFF")
    assert (box[15], box[16], box[17], box[18]) == ("1", "0", "0", "7")  # no outline or shadow
    assert len([x for x in lines if x.startswith("Dialogue:")]) == 2
    assert ass.endswith("\n") and "\r" not in ass


def test_extra_events_follow_the_caption_events() -> None:
    extra = AssEvent(2, 0.0, 2.0, "Hook", "hello")
    first = AssEvent(1, 0.0, 1.0, "Cap", "x")
    ass = build_ass_v2(
        [first], [extra], font="Montserrat", settings=SETTINGS, width=1080, height=1920
    )
    dialogues = [x for x in ass.split("\n") if x.startswith("Dialogue:")]
    assert dialogues == [
        "Dialogue: 1,0:00:00.00,0:00:01.00,Cap,,0,0,0,,x",
        "Dialogue: 2,0:00:00.00,0:00:02.00,Hook,,0,0,0,,hello",
    ]


def test_v2_font_name_cannot_break_a_style_line() -> None:
    ass = build_ass_v2([], font="Bad,\nName", settings=SETTINGS, width=1080, height=1920)
    styles = [x for x in ass.split("\n") if x.startswith("Style:")]
    assert len(styles) == 3
    assert all(len(s.split(",")) == 23 for s in styles)
    assert ass.endswith("\n") and "\r" not in ass


def test_settings_come_from_the_captions_config() -> None:
    config = CaptionsConfig(words_per_line=3, font="Montserrat", highlight_color="#FFD400")
    settings = KaraokeSettings.from_config(config)
    assert (settings.font_size, settings.max_words, settings.max_width_px) == (80, 4, 760)
    assert settings.text_limit_px == 724.0


# --- hook overlay (Brief R1); synthetic hook text -----------------------------------------------

HOOK_LIMIT = 768 - 2 * 18  # the safe width minus the padding on both sides


def hook(
    metrics: FontMetrics,
    text: str,
    faces: list[Rect] | None = None,
    *,
    seconds: float = 2.0,
    clip: float = 30.0,
    fade_ms: int = 150,
    font_size: int | None = None,
) -> HookResult:
    return build_hook(
        text,
        SETTINGS,
        metrics,
        SAFE,
        faces,
        seconds=seconds,
        fade_ms=fade_ms,
        line_gap_em=0.35,
        clip_seconds=clip,
        font_size=font_size,
    )


def hook_text_events(result: HookResult) -> list[AssEvent]:
    return [e for e in result.events if e.style == "Hook"]


def hook_size(event: AssEvent) -> int:
    match = re.search(r"\\fs(\d+)\}", event.text)
    return int(match.group(1)) if match else SETTINGS.font_size


def test_the_hook_is_shown_from_zero_to_the_configured_seconds_with_a_fade(
    metrics: FontMetrics,
) -> None:
    result = hook(metrics, "synthetic hook line", [])
    assert result.shown and not result.moved and result.band is not None
    box, text = result.events
    assert (box.layer, box.style, text.layer, text.style) == (2, "Box", 3, "Hook")
    assert [(e.start, e.end) for e in result.events] == [(0.0, 2.0), (0.0, 2.0)]
    assert box.text.startswith("{\\pos(0,0)\\fad(0,150)\\p1}")
    assert re.match(r"\{\\pos\(504,\d+\)\\fad\(0,150\)\}synthetic", text.text)
    assert result.notes == []


def test_a_clip_shorter_than_the_hook_time_ends_the_hook_with_the_clip(
    metrics: FontMetrics,
) -> None:
    result = hook(metrics, "synthetic hook", [], clip=1.2)
    assert {e.end for e in result.events} == {1.2}
    tiny = hook(metrics, "synthetic hook", [], clip=0.1)
    assert {e.end for e in tiny.events} == {0.1}
    assert "\\fad(0,100)" in tiny.events[1].text  # the fade cannot be longer than the hook
    assert "\\fad" not in hook(metrics, "synthetic hook", [], fade_ms=0).events[1].text


@pytest.mark.parametrize("text", ["", "   ", "{}", "\\", "\n\t"])
def test_a_blank_hook_is_skipped_with_a_note(metrics: FontMetrics, text: str) -> None:
    result = hook(metrics, text, [])
    assert (result.events, result.shown, result.moved, result.band) == ([], False, False, None)
    assert result.notes == ["hook_missing"]


HOOK_TAGS = [
    re.compile(r"\{\\pos\(0,0\)(?:\\fad\(0,\d+\))?\\p1\}[mlb0-9 ]+\{\\p0\}"),
    re.compile(r"\{\\pos\(\d+,\d+\)(?:\\fad\(0,\d+\))?(?:\\fs\d+)?\}"),
    re.compile(r"\{\\fsp\d+(?:\.\d{1,2})?\}"),
    re.compile(r"\{\\fsp\d+(?:\.\d{1,2})?\\k\d+\}"),
]


def test_a_hostile_hook_cannot_add_tags_or_lines(metrics: FontMetrics) -> None:
    text = "{\\an8}big\nsecond\\Nline {\\p1}x}{ y\r\nz \\"
    result = hook(metrics, text, [])
    assert result.shown
    ass = build_ass_v2(
        [], result.events, font="Montserrat", settings=SETTINGS, width=1080, height=1920
    )
    assert "\r" not in ass
    texts = dialogue_texts(ass)
    assert len(texts) == len(result.events)
    for body in texts:
        for pattern in HOOK_TAGS:
            body = pattern.sub("", body)
        assert not any(c in body for c in "{}\\\n\r")
    shown = "".join(texts[1:])  # the words survive, minus the characters ASS would read as tags
    assert all(part in shown for part in ("an8big", "secondNline", "p1x"))


def test_a_short_hook_is_one_line_and_a_long_one_two_with_the_pitch_between(
    metrics: FontMetrics,
) -> None:
    one = hook(metrics, "alpha beta", [])
    assert len(hook_text_events(one)) == 1
    two = hook(metrics, "alpha beta gamma delta omega sigma", [])
    first, second = hook_text_events(two)
    assert two.notes == []  # fits at full size
    pitch = metrics.cap_px(64) + metrics.descent_px(64) + 0.35 * 64
    gap = int(re.search(r"pos\(504,(\d+)\)", second.text).group(1)) - int(  # type: ignore[union-attr]
        re.search(r"pos\(504,(\d+)\)", first.text).group(1)  # type: ignore[union-attr]
    )
    assert abs(gap - pitch) <= 1  # integer anchors
    band = two.band
    assert band is not None
    height = 18 + metrics.cap_px(64) + pitch + metrics.descent_px(64) + 18
    assert band.y2 - band.y1 == pytest.approx(height)
    # the visible gap between the lines (descent to the next cap top) is the configured 0.35 em
    assert pitch - metrics.cap_px(64) - metrics.descent_px(64) == pytest.approx(0.35 * 64)
    assert (band.x1 + band.x2) / 2 == 504


def test_a_hook_too_wide_for_one_line_at_full_size_is_shrunk(metrics: FontMetrics) -> None:
    count = next(n for n in range(1, 80) if metrics.text_width("m" * n, 64, 1.0) > HOOK_LIMIT)
    result = hook(metrics, "m" * count, [])
    assert result.notes == ["hook_shrunk", "hook_faces_unknown"][:1]
    (event,) = hook_text_events(result)
    assert hook_size(event) == 57 and "\\fs57}" in event.text
    assert result.band is not None and result.band.inside(SAFE)


# --- the hook's own base size (Brief 11); the caption size in these tests is 64 -------------------


def test_no_hook_font_size_means_the_caption_size(metrics: FontMetrics) -> None:
    default = hook(metrics, "synthetic hook line", [])
    explicit = hook(metrics, "synthetic hook line", [], font_size=SETTINGS.font_size)
    assert default.events == explicit.events and default.notes == explicit.notes
    (event,) = hook_text_events(default)
    assert not re.search(r"\\fs\d", event.text)  # the Hook style already has the caption size


@pytest.mark.parametrize("base", [40, 100])
def test_a_hook_font_size_other_than_the_caption_size_gets_a_size_tag(
    metrics: FontMetrics, base: int
) -> None:
    result = hook(metrics, "synthetic hook line", [], font_size=base)
    (event,) = hook_text_events(result)
    assert hook_size(event) == base and f"\\fs{base}}}" in event.text
    assert result.notes == []  # smaller than the caption size is not "shrunk"


def test_the_hook_shrinks_in_fractions_of_its_own_base_size(metrics: FontMetrics) -> None:
    for base in (40, 100):
        count = next(
            n for n in range(1, 200) if metrics.text_width("m" * n, base, 1.0) > HOOK_LIMIT
        )
        result = hook(metrics, "m" * count, [], font_size=base)
        (event,) = hook_text_events(result)
        assert result.notes == ["hook_shrunk"]
        assert hook_size(event) in {math.floor(base * f) for f in HOOK_SIZE_FRACTIONS[1:]}
        assert result.band is not None and result.band.inside(SAFE)


def test_a_truncated_hook_is_shrunk_to_seventy_percent_of_the_base_size(
    metrics: FontMetrics,
) -> None:
    result = hook(metrics, "w" * 400, [], font_size=100)
    (event,) = hook_text_events(result)
    assert {"hook_truncated", "hook_shrunk"} <= set(result.notes)
    assert hook_size(event) == 70


def test_a_hook_at_its_base_size_is_not_shrunk_even_below_the_caption_size(
    metrics: FontMetrics,
) -> None:
    result = hook(metrics, "alpha beta", [], font_size=20)
    (event,) = hook_text_events(result)
    assert hook_size(event) == 20 and result.notes == []


def test_a_60_word_hook_is_truncated_to_two_lines_inside_the_safe_area(
    metrics: FontMetrics,
) -> None:
    text = " ".join(f"word{i}" for i in range(60))
    result = hook(metrics, text, [])
    events = hook_text_events(result)
    assert 1 <= len(events) <= 2
    assert "hook_truncated" in result.notes
    assert events[-1].text.endswith("\u2026")
    assert result.band is not None and result.band.inside(SAFE)
    assert result.band.x2 - result.band.x1 <= 768


def test_a_400_character_word_is_cut_to_fit(metrics: FontMetrics) -> None:
    result = hook(metrics, "w" * 400, [])
    (event,) = hook_text_events(result)
    assert "hook_truncated" in result.notes
    assert event.text.endswith("\u2026") and len(event.text) < 100
    assert hook_size(event) == 44  # floor(64 * 0.7)
    assert result.band is not None and result.band.inside(SAFE)
    assert result.band.x2 - result.band.x1 <= 768 + 1e-6


def test_the_hook_moves_below_a_face_that_overlaps_it(metrics: FontMetrics) -> None:
    result = hook(metrics, "synthetic hook line", [Rect(400, 250, 600, 500.4)])
    assert result.moved and result.band is not None
    assert result.band.y1 == 513  # ceil(500.4 + 12)
    assert result.notes == ["hook_moved_for_faces"]
    assert result.band.inside(SAFE) and result.band.y2 <= caption_top(SETTINGS, metrics, SAFE) - 12
    assert "pos(504," in hook_text_events(result)[0].text


@pytest.mark.parametrize("face", [Rect(0, 300, 100, 600), Rect(400, 800, 600, 1000)])
def test_a_face_beside_or_below_the_hook_does_not_move_it(metrics: FontMetrics, face: Rect) -> None:
    result = hook(metrics, "synthetic hook line", [face])
    assert not result.moved and result.band is not None and result.band.y1 == 290
    assert result.notes == []


def test_a_face_that_leaves_no_room_keeps_the_hook_at_the_top(metrics: FontMetrics) -> None:
    result = hook(metrics, "synthetic hook line", [Rect(0, 250, 1080, 1180)])
    assert not result.moved and result.band is not None and result.band.y1 == 290
    assert result.notes == ["hook_over_face"]


def test_without_face_data_the_hook_is_at_the_top_and_says_so(metrics: FontMetrics) -> None:
    result = hook(metrics, "synthetic hook line", None)
    assert result.band is not None and result.band.y1 == 290 and not result.moved
    assert result.notes == ["hook_faces_unknown"]


def test_the_hook_band_is_always_inside_the_safe_area_and_above_the_caption(
    metrics: FontMetrics,
) -> None:
    top_of_caption = caption_top(SETTINGS, metrics, SAFE)
    (normal,) = [
        r for r in karaoke(metrics, seq(["alpha", "beta"])).boxes
    ]  # a real caption box at the same size
    assert normal.y1 == pytest.approx(top_of_caption)
    texts = ["alpha", "alpha beta gamma delta omega sigma", "m" * 30, "word " * 70, "x" * 500]
    for text in texts:
        for faces in (None, [], [Rect(300, 200, 700, 700)], [Rect(0, 0, 1080, 1900)]):
            result = hook(metrics, text, faces)
            assert result.band is not None
            assert result.band.inside(SAFE)
            assert result.band.y2 <= top_of_caption - 12
            assert not result.band.intersects(normal)
            assert len(hook_text_events(result)) <= 2


# --- hook that avoids a rectangle (Brief 11: the campaign watermark) ------------------------------

MARK = Rect(0, 280, 1080, 400)  # across the top of the safe area


def band_top(result: HookResult) -> float:
    assert result.band is not None
    return result.band.y1


def text_tops(result: HookResult) -> list[int]:
    return [int(re.search(r"pos\(504,(\d+)\)", e.text).group(1)) for e in hook_text_events(result)]  # type: ignore[union-attr]


def test_the_hook_goes_below_an_avoided_rectangle(metrics: FontMetrics) -> None:
    result = build_hook(
        "alpha beta gamma delta omega sigma",
        SETTINGS,
        metrics,
        SAFE,
        [],
        seconds=2.0,
        fade_ms=150,
        line_gap_em=0.35,
        clip_seconds=30.0,
        avoid=[MARK],
    )
    assert band_top(result) == 412.0 and not result.moved
    assert result.notes == ["hook_moved_for_overlay"]
    assert all(top >= 412 for top in text_tops(result)) and len(text_tops(result)) == 2
    assert result.band is not None and result.band.inside(SAFE)


def test_an_avoided_rectangle_also_works_when_faces_are_unknown(metrics: FontMetrics) -> None:
    result = build_hook(
        "synthetic hook line",
        SETTINGS,
        metrics,
        SAFE,
        None,
        seconds=2.0,
        fade_ms=150,
        line_gap_em=0.35,
        clip_seconds=30.0,
        avoid=[MARK],
    )
    assert band_top(result) == 412.0
    assert result.notes == ["hook_faces_unknown", "hook_moved_for_overlay"]
    assert text_tops(result)[0] >= 412


def test_faces_below_the_avoided_rectangle_still_move_the_hook(metrics: FontMetrics) -> None:
    result = build_hook(
        "synthetic hook line",
        SETTINGS,
        metrics,
        SAFE,
        [Rect(400, 420, 600, 520)],
        seconds=2.0,
        fade_ms=150,
        line_gap_em=0.35,
        clip_seconds=30.0,
        avoid=[MARK],
    )
    assert result.moved and band_top(result) == 532.0
    assert result.notes == ["hook_moved_for_faces", "hook_moved_for_overlay"]


def test_without_an_avoided_rectangle_the_events_are_unchanged(metrics: FontMetrics) -> None:
    plain = hook(metrics, "synthetic hook line", [])
    elsewhere = build_hook(
        "synthetic hook line",
        SETTINGS,
        metrics,
        SAFE,
        [],
        seconds=2.0,
        fade_ms=150,
        line_gap_em=0.35,
        clip_seconds=30.0,
        avoid=[Rect(0, 1500, 100, 1600)],
    )
    assert elsewhere.events == plain.events and elsewhere.notes == plain.notes == []
    assert band_top(plain) == 290.0
