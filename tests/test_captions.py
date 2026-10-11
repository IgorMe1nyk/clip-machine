import pytest

from clipmachine.models import TranscriptSegment, TranscriptWord
from clipmachine.utils.captions import (
    CaptionWord,
    ass_color,
    ass_time,
    build_ass,
    build_events,
    clean_word,
    clip_words,
    group_words,
)


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
