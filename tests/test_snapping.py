"""Sentence snapping and de-duplication, on synthetic word lists (no real transcript text)."""

import math
import random

import pytest

from clipmachine.models import TranscriptSegment, TranscriptWord
from clipmachine.utils.snapping import (
    EDGE_END_NOTE,
    EDGE_START_NOTE,
    END_EDGE,
    END_KEPT_NOTE,
    END_PAUSE,
    START_KEPT_NOTE,
    AutoEnd,
    Dropped,
    NearSnap,
    Sentence,
    SentenceIndex,
    Snapped,
    auto_end,
    build_index,
    dedupe,
    ends_sentence,
    snap,
    snap_near,
    words_text,
)

WORD = 0.5  # seconds per word
GAP = 0.3  # seconds between words: the cut sits 0.15 s from each neighbouring word by default
STEP = WORD + GAP


def ws(k: int, step: float = STEP, origin: float = 0.0) -> float:
    return origin + k * step


def we(k: int, step: float = STEP, origin: float = 0.0) -> float:
    return origin + k * step + WORD


def build(
    spec: list[str], *, gap: float = GAP, origin: float = 0.0, leading_space: bool = True
) -> list[TranscriptSegment]:
    """One segment per string; every token is a word WORD long, `gap` apart."""
    segments = []
    t = origin
    for number, text in enumerate(spec):
        words = []
        for token in text.split():
            words.append(
                TranscriptWord(
                    start=t,
                    end=t + WORD,
                    word=(" " if leading_space else "") + token,
                    probability=0.9,
                )
            )
            t += WORD + gap
        segments.append(
            TranscriptSegment(
                id=number,
                start=words[0].start,
                end=words[-1].end,
                text=text,
                avg_logprob=0.0,
                no_speech_prob=0.0,
                words=words,
            )
        )
    return segments


def run_snap(
    spec_or_segments: list[str] | list[TranscriptSegment],
    model_start: float,
    model_end: float,
    *,
    start_pad: float = 0.25,
    end_pad: float = 0.5,
    duration: float = 1000.0,
    min_length: float = 1.0,
    max_length: float = 500.0,
    preferred_min: float = 0.0,
    fallback_words: int = 40,
    gap: float = GAP,
    clean: bool = False,  # snap over index.units(): only clean cut points
) -> Snapped | Dropped:
    segments = (
        build(spec_or_segments, gap=gap)  # type: ignore[arg-type]
        if spec_or_segments and isinstance(spec_or_segments[0], str)
        else spec_or_segments
    )
    index = build_index(segments, fallback_words)  # type: ignore[arg-type]
    return snap(
        index.units() if clean else index,
        model_start,
        model_end,
        start_pad=start_pad,
        end_pad=end_pad,
        duration=duration,
        min_length=min_length,
        max_length=max_length,
        preferred_min=preferred_min,
    )


def snapped(result: Snapped | Dropped) -> Snapped:
    assert isinstance(result, Snapped), result
    return result


FOUR = ["One two three. Four five six seven. Eight nine ten. Eleven twelve."]
# words: 0 One, 1 two, 2 three. | 3 Four, 4 five, 5 six, 6 seven. | 7 Eight, 8 nine, 9 ten. |
#        10 Eleven, 11 twelve.


# --- index ---------------------------------------------------------------------------------------


def test_words_lose_their_leading_space_and_empty_words_are_skipped() -> None:
    segments = build(["Hello there."])
    segments[0].words.append(TranscriptWord(start=2.0, end=2.1, word="  ", probability=0.5))
    index = build_index(segments)
    assert [w.text for w in index.words] == ["Hello", "there."]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("done.", True),
        ("really?", True),
        ("no!", True),
        ('stop."', True),
        ("(maybe.)", True),
        ("wait...", True),
        ("wait…", True),
        ("comma,", False),
        ("plain", False),
        ("3.5", False),
        ("", False),
    ],
)
def test_sentence_end_detection(text: str, expected: bool) -> None:
    assert ends_sentence(text) is expected


def test_a_mid_sentence_segment_edge_is_not_a_boundary() -> None:
    index = build_index(build(["Alpha beta. Gamma delta", "epsilon zeta."]))
    assert [(s.first, s.last) for s in index.sentences] == [(0, 1), (2, 5)]


def test_a_long_unpunctuated_run_is_split_at_its_middle_when_all_gaps_are_equal() -> None:
    spec = [" ".join(f"s{n}w{k}" for k in range(10)) for n in range(6)]  # 60 unpunctuated words
    index = build_index(build(spec), fallback_max_words=40)
    # segment edges play no part; every gap is 0.3 s (under min_pause), so the split is balanced
    assert [(s.first, s.last) for s in index.sentences] == [(0, 29), (30, 59)]


def test_exactly_the_fallback_number_of_words_is_not_enough() -> None:
    spec = [" ".join(f"s{n}w{k}" for k in range(10)) for n in range(4)]  # 40 words in all
    spec[-1] += "."
    index = build_index(build(spec), fallback_max_words=40)
    assert [(s.first, s.last) for s in index.sentences] == [(0, 39)]
    shorter = build_index(build(spec), fallback_max_words=39)
    assert [(s.first, s.last) for s in shorter.sentences] == [(0, 19), (20, 39)]


def test_the_first_word_of_the_transcript_starts_a_sentence() -> None:
    index = build_index(build(["starts mid thought and goes on", "until it ends."]))
    assert index.sentences[0].first == 0
    result = snapped(run_snap(["starts mid thought and goes on", "until it ends."], -1.0, 1.0))
    assert result.first_word == 0
    assert result.edge_start is False


def test_segment_without_words_becomes_one_pseudo_word() -> None:
    segments = build(["One two."])
    segments.append(
        TranscriptSegment(
            id=1,
            start=5.0,
            end=9.0,
            text=" a segment with no word times. ",
            avg_logprob=0,
            no_speech_prob=0,
            words=[],
        )
    )
    index = build_index(segments)
    assert (index.words[-1].start, index.words[-1].end) == (5.0, 9.0)
    assert words_text(index, 2, 2) == "a segment with no word times."


# --- end kinds, pauses and gaps (Brief 10) ------------------------------------------------------

# Times here are multiples of 0.125 so gaps compare exactly: words are 0.5 s long, a "quarter" gap
# is 0.25 s. The thresholds the tests pass are min_pause 0.5 and min_cut_gap 0.25.
QUARTER = 0.25
PAUSE = 0.5
MIN_PAUSE = 0.5
MIN_CUT = 0.25


def timed(
    items: list[tuple[str, float]], *, breaks: frozenset[int] = frozenset()
) -> list[TranscriptSegment]:
    """Words with explicit gaps: (token, seconds from its end to the next word's start; negative
    = overlap). `breaks` are word indices that begin a new segment."""
    groups: list[list[TranscriptWord]] = [[]]
    t = 0.0
    for i, (token, gap_after) in enumerate(items):
        if i in breaks and groups[-1]:
            groups.append([])
        groups[-1].append(TranscriptWord(start=t, end=t + WORD, word=" " + token, probability=0.9))
        t += WORD + gap_after
    return [
        TranscriptSegment(
            id=n,
            start=g[0].start,
            end=g[-1].end,
            text="x",
            avg_logprob=0.0,
            no_speech_prob=0.0,
            words=g,
        )
        for n, g in enumerate(groups)
    ]


def make_index(
    items: list[tuple[str, float]],
    fallback: int = 40,
    *,
    breaks: frozenset[int] = frozenset(),
) -> SentenceIndex:
    return build_index(
        timed(items, breaks=breaks), fallback, min_pause=MIN_PAUSE, min_cut_gap=MIN_CUT
    )


def plain(count: int, gap: float = QUARTER, final: str = "") -> list[tuple[str, float]]:
    """`count` unpunctuated words with equal gaps; the last carries `final`."""
    items = [(f"w{k}", gap) for k in range(count)]
    items[-1] = (items[-1][0] + final, gap)
    return items


def spans(index: SentenceIndex) -> list[tuple[int, int]]:
    return [(s.first, s.last) for s in index.sentences]


@pytest.mark.parametrize(
    ("token", "gap_after", "expected"),
    [
        ("end.", 0.5, "."),
        ("end?", 0.5, "?"),
        ("end!", 0.5, "!"),
        ("end...", 0.5, "."),
        ("end…", 0.5, "."),
        ('end?"', 0.5, "?"),
        ('end."', 0.5, "."),
        ("(end!)", 0.5, "!"),
        ("end.", MIN_CUT, "."),  # exactly min_cut_gap is enough
        ("end?", 0.125, END_EDGE),
        ("end.", 0.0, END_EDGE),
        ("end!", -0.25, END_EDGE),
        ('end."', 0.125, END_EDGE),
    ],
)
def test_a_punctuated_end_is_its_mark_or_an_edge_when_the_gap_is_too_small(
    token: str, gap_after: float, expected: str
) -> None:
    index = make_index([("a", QUARTER), (token, gap_after), ("b", QUARTER), ("c.", 0.0)])
    assert spans(index) == [(0, 1), (2, 3)]
    assert index.sentences[0].end == expected
    assert index.sentences[1].end == "."


def test_the_last_sentence_is_never_an_edge() -> None:
    punctuated = make_index([("a", QUARTER), ("b.", 0.0)])
    assert (punctuated.sentences[-1].end, punctuated.sentences[-1].gap) == (".", math.inf)
    unpunctuated = make_index([("a", QUARTER), ("b", 0.0)])
    assert unpunctuated.sentences[-1].end == END_PAUSE
    assert unpunctuated.sentences[-1].gap == math.inf
    questioning = make_index([("a?", 0.0)])
    assert questioning.sentences[-1].end == "?"


def test_a_long_unpunctuated_run_splits_at_every_gap_of_min_pause_or_more() -> None:
    items = plain(12, final=".")
    items[3] = (items[3][0], PAUSE)
    items[7] = (items[7][0], PAUSE + 0.25)
    index = make_index(items, fallback=4)
    assert spans(index) == [(0, 3), (4, 7), (8, 11)]
    assert [s.end for s in index.sentences] == [END_PAUSE, END_PAUSE, "."]
    assert [s.gap for s in index.sentences] == [PAUSE, PAUSE + 0.25, math.inf]


def test_a_gap_just_under_min_pause_is_not_a_pause_boundary() -> None:
    items = plain(8)
    items[3] = (items[3][0], PAUSE - 0.125)
    index = make_index(items, fallback=4)
    # no pause: the 8 words are split at their largest gap, which is that one (an edge)
    assert spans(index) == [(0, 3), (4, 7)]
    assert [s.end for s in index.sentences] == [END_EDGE, END_PAUSE]


def test_a_piece_still_too_long_splits_at_its_largest_gap_then_at_its_middle() -> None:
    items = plain(8)
    items[2] = (items[2][0], 0.375)  # the largest gap, under min_pause
    index = make_index(items, fallback=4)
    # 0-2 | 3-7; the 5-word piece has equal gaps: 2/3 and 3/2 are equally central, the earlier wins
    assert spans(index) == [(0, 2), (3, 4), (5, 7)]
    assert [s.end for s in index.sentences] == [END_EDGE, END_EDGE, END_PAUSE]


def test_equal_gaps_give_balanced_pieces_not_slivers() -> None:
    index = make_index(plain(100), fallback=40)
    assert spans(index) == [(0, 24), (25, 49), (50, 74), (75, 99)]
    assert [s.end for s in index.sentences] == [END_EDGE, END_EDGE, END_EDGE, END_PAUSE]


def test_pieces_between_pauses_are_split_further_when_they_are_still_too_long() -> None:
    items = plain(20)
    items[9] = (items[9][0], PAUSE)  # two pieces of 10 words
    index = make_index(items, fallback=6)
    assert spans(index) == [(0, 4), (5, 9), (10, 14), (15, 19)]
    assert [s.end for s in index.sentences] == [END_EDGE, END_PAUSE, END_EDGE, END_PAUSE]
    assert all(s.last - s.first + 1 <= 6 for s in index.sentences)


def test_gaps_are_compared_to_three_decimals() -> None:
    items = plain(6)
    items[1] = (items[1][0], 0.3754)  # both round to 0.375: a tie, so the middle wins
    items[2] = (items[2][0], 0.3746)
    index = make_index(items, fallback=4)
    assert spans(index) == [(0, 2), (3, 5)]


def test_exactly_fallback_max_words_is_not_split_even_across_a_pause() -> None:
    items = plain(4)
    items[1] = (items[1][0], 2.0)
    assert spans(make_index(items, fallback=4)) == [(0, 3)]
    assert spans(make_index(items, fallback=3)) == [(0, 1), (2, 3)]


def test_segment_edges_alone_create_no_boundary() -> None:
    items = plain(12, final=".")
    index = make_index(items, fallback=8, breaks=frozenset({3, 7}))
    assert spans(index) == [(0, 5), (6, 11)]  # 12 words > 8: split at the middle, not at 3 or 7
    assert [s.end for s in index.sentences] == [END_EDGE, "."]
    # with a pause at word 5 the split is the pause; the segment edges at 3 and 7 still play no part
    items[5] = (items[5][0], PAUSE)
    index = make_index(items, fallback=8, breaks=frozenset({3, 7}))
    assert spans(index) == [(0, 5), (6, 11)]
    assert [s.end for s in index.sentences] == [END_PAUSE, "."]
    # a run that is short enough stays whole whatever the segments do
    assert spans(make_index(plain(6, final="."), fallback=8, breaks=frozenset({2, 4}))) == [(0, 5)]


def test_gap_is_the_time_to_the_next_word_never_negative() -> None:
    index = make_index([("a", QUARTER), ("b.", 0.75), ("c", QUARTER), ("d.", -0.25), ("e.", 0.0)])
    assert [s.gap for s in index.sentences] == [0.75, 0.0, math.inf]
    assert [s.end for s in index.sentences] == [".", END_EDGE, "."]


def test_sentence_defaults_keep_old_constructions_valid() -> None:
    sentence = Sentence(0, 3)
    assert (sentence.end, sentence.gap) == (".", 0.0)


def test_cut_after_and_cut_before() -> None:
    items = [
        ("a.", PAUSE),  # 0: a real end
        ("b.", 0.0),  # 1: punctuation in continuous speech: an edge
        ("c", QUARTER),
        ("d", PAUSE),  # 2: a pause end
        ("e", QUARTER),
        ("f.", 0.0),  # 3: the last sentence
    ]
    index = make_index(items, fallback=2)
    assert [s.end for s in index.sentences] == [".", END_EDGE, END_PAUSE, "."]
    assert [index.cut_after(n) for n in range(4)] == [True, False, True, True]
    assert [index.cut_before(n) for n in range(4)] == [True, True, False, True]


def test_snap_flags_follow_the_end_kinds() -> None:
    index = make_index(
        [("a", QUARTER), ("b.", PAUSE), ("c", QUARTER), ("d.", 0.0), ("e", QUARTER), ("f.", 0.0)]
    )
    assert [s.end for s in index.sentences] == [".", END_EDGE, "."]
    # the middle sentence starts after a real end and ends at an edge
    middle = snapped(
        snap(
            index,
            1.5,
            2.5,
            start_pad=0.0,
            end_pad=0.0,
            duration=10.0,
            min_length=0.1,
            max_length=500.0,
        )
    )
    assert (middle.first_word, middle.last_word) == (2, 3)
    assert (middle.edge_start, middle.edge_end) == (False, True)
    # the last sentence starts after an edge
    last = snapped(
        snap(
            index,
            3.5,
            4.5,
            start_pad=0.0,
            end_pad=0.0,
            duration=10.0,
            min_length=0.1,
            max_length=500.0,
        )
    )
    assert (last.first_word, last.last_word) == (4, 5)
    assert (last.edge_start, last.edge_end) == (True, False)


# --- boundaries ----------------------------------------------------------------------------------


def test_mid_sentence_start_and_end_snap_to_sentence_boundaries() -> None:
    result = snapped(run_snap(FOUR, ws(4) + 0.1, ws(8) + 0.1, gap=GAP))
    assert (result.first_word, result.last_word) == (3, 9)
    assert result.start == pytest.approx(ws(3) - 0.15)
    assert result.end == pytest.approx(we(9) + 0.15)
    assert (result.edge_start, result.edge_end) == (False, False)
    assert any(n.startswith("start moved -") for n in result.notes)
    assert any(n.startswith("end moved +") for n in result.notes)


def test_start_and_end_already_on_boundaries_are_only_cut_mid_gap() -> None:
    result = snapped(run_snap(FOUR, ws(3), we(9)))
    assert (result.first_word, result.last_word) == (3, 9)
    assert result.start == pytest.approx(ws(3) - 0.15)
    assert result.end == pytest.approx(we(9) + 0.15)
    assert result.notes == ()


def test_single_sentence_moment() -> None:
    result = snapped(run_snap(FOUR, ws(7) + 0.05, ws(9)))
    assert (result.first_word, result.last_word) == (7, 9)


def test_no_punctuation_splits_at_equal_gaps_after_the_word_limit() -> None:
    spec = ["alpha beta gamma delta", "epsilon zeta eta theta", "iota kappa lambda mu"]
    # 12 words, limit 5: split 6/6, then 3/3 each; the middle sentences end at an edge
    result = snapped(run_snap(spec, ws(4), ws(4) + 0.1, fallback_words=5))
    assert (result.first_word, result.last_word) == (3, 5)
    assert (result.edge_start, result.edge_end) == (True, True)
    assert "start is a segment edge: no sentence punctuation before it" in result.notes
    assert "end is a segment edge: no sentence punctuation after it" in result.notes


def test_no_punctuation_under_the_word_limit_is_one_sentence() -> None:
    spec = ["alpha beta gamma delta", "epsilon zeta eta theta", "iota kappa lambda mu"]
    result = snapped(run_snap(spec, ws(5), ws(6)))
    assert (result.first_word, result.last_word) == (0, 11)
    # the recording ends here: always a legal cut, never an edge
    assert (result.edge_start, result.edge_end) == (False, False)


def test_audio_start_is_not_a_segment_edge() -> None:
    spec = ["alpha beta gamma delta", "epsilon zeta eta theta"]
    result = snapped(run_snap(spec, 0.0, ws(2)))
    assert result.first_word == 0
    assert result.edge_start is False  # the audio begins here
    assert result.edge_end is False  # and ends here: the end of the recording is no edge


def test_a_time_in_a_gap_belongs_to_the_next_sentence_for_a_start() -> None:
    gap_time = we(2) + 0.1  # between "three." and "Four"
    result = snapped(run_snap(FOUR, gap_time, we(6)))
    assert result.first_word == 3


def test_a_time_in_a_gap_belongs_to_the_previous_sentence_for_an_end() -> None:
    gap_time = we(6) + 0.1  # between "seven." and "Eight"
    result = snapped(run_snap(FOUR, ws(3), gap_time))
    assert result.last_word == 6


def test_times_outside_the_transcript_use_the_first_and_last_sentences() -> None:
    result = snapped(run_snap(FOUR, -50.0, 9999.0))
    assert (result.first_word, result.last_word) == (0, 11)


# --- padding -------------------------------------------------------------------------------------


def test_padding_never_runs_into_a_neighbouring_word() -> None:
    gap = 0.05
    spec = FOUR
    result = snapped(run_snap(spec, ws(3, 0.55), we(9, 0.55), gap=gap))
    # word 2 ends 0.05 s before word 3 starts, word 10 starts 0.05 s after word 9 ends
    # the cut is in the middle of each 0.05 s gap, so it never touches a neighbouring word
    assert result.start == pytest.approx(ws(3, 0.55) - 0.025)
    assert result.end == pytest.approx(we(9, 0.55) + 0.025)
    assert result.start > we(2, 0.55)
    assert result.end < ws(10, 0.55)


def test_padding_is_clamped_to_the_start_and_end_of_the_audio() -> None:
    segments = build(["Hi there. Bye now."], origin=0.1)
    last_end = segments[0].words[-1].end
    result = snapped(run_snap(segments, 0.0, last_end, duration=last_end + 0.05))
    # the gap from 0.0 is 0.1 s and the gap to the duration is 0.05 s: half of each
    assert result.start == pytest.approx(0.05)
    assert result.end == pytest.approx(last_end + 0.025)


def test_end_never_passes_the_audio_duration() -> None:
    segments = build(["Hi there. Bye now."])
    last_end = segments[0].words[-1].end
    result = snapped(run_snap(segments, 0.0, last_end, duration=last_end))
    assert result.end == pytest.approx(last_end)
    assert result.start == 0.0


def test_zero_padding_gives_exact_word_times() -> None:
    result = snapped(run_snap(FOUR, ws(3), we(9), start_pad=0.0, end_pad=0.0))
    assert (result.start, result.end) == (pytest.approx(ws(3)), pytest.approx(we(9)))
    assert result.notes == ()


# --- length: grow, shrink, drop ---------------------------------------------------------

# Eight sentences of four words each: sentence n covers words 4n..4n+3 and is 3.2 - 0.3 s long.
EIGHT = [" ".join(f"w{n}{k}" + ("." if k == 3 else "") for k in range(4)) for n in range(8)]


def sentence_times(n: int) -> tuple[float, float]:
    return ws(4 * n), we(4 * n + 3)


def test_too_short_grows_by_whole_sentences_toward_the_end_first() -> None:
    start, end = sentence_times(2)
    result = snapped(run_snap(EIGHT, start, end, min_length=10.0, max_length=30.0))
    assert (result.first_word, result.last_word) == (8, 23)  # sentences 2 to 5
    assert result.end - result.start >= 10.0
    assert "grew by 3 sentence(s) at the end to reach the minimum length" in result.notes


def test_growth_goes_to_the_front_when_there_is_nothing_after() -> None:
    start, end = sentence_times(7)
    result = snapped(run_snap(EIGHT, start, end, min_length=10.0, max_length=30.0))
    assert (result.first_word, result.last_word) == (16, 31)
    assert any(n.startswith("grew backward by 3 sentence(s)") for n in result.notes)
    assert not any("at the end" in n for n in result.notes)
    assert result.grew_backward == 3


def test_a_short_moment_grows_toward_the_end_to_the_preferred_length() -> None:
    start, end = sentence_times(2)  # one sentence is about 3.3 s: above the 1 s minimum
    result = snapped(run_snap(EIGHT, start, end, min_length=1.0, preferred_min=10.0))
    assert (result.first_word, result.last_word) == (
        8,
        23,
    )  # sentences 2 to 5; three are only 9.7 s
    assert result.end - result.start >= 10.0 - 1e-6
    assert "grew by 3 sentence(s) at the end toward the preferred length" in result.notes
    assert result.grew_backward == 0


def test_preferred_growth_never_goes_backward() -> None:
    start, end = sentence_times(7)  # the last sentence: nothing after it
    result = snapped(run_snap(EIGHT, start, end, min_length=1.0, preferred_min=10.0))
    assert (result.first_word, result.last_word) == (28, 31)
    assert result.grew_backward == 0


def test_preferred_growth_stops_before_the_maximum() -> None:
    start, end = sentence_times(0)
    result = snapped(
        run_snap(EIGHT, start, end, min_length=1.0, max_length=7.0, preferred_min=30.0)
    )
    assert result.end - result.start <= 7.0
    assert (result.first_word, result.last_word) == (0, 7)  # two sentences; a third passes 7 s


def test_a_moment_already_at_the_preferred_length_is_not_grown() -> None:
    start, end = sentence_times(2)
    result = snapped(run_snap(EIGHT, start, ws(4 * 4 + 3), min_length=1.0, preferred_min=6.0))
    assert result.last_word == 19
    assert not any("preferred" in n for n in result.notes)


def test_growth_will_not_pass_the_maximum() -> None:
    # sentences of 20 words are 16 s: one is under the minimum, two pass the maximum
    spec = [" ".join(["x"] * 19 + ["y."]) for _ in range(3)]
    result = run_snap(spec, ws(0), ws(5), min_length=20.0, max_length=30.0)
    assert isinstance(result, Dropped)
    assert "shorter than the 20 s minimum" in result.reason


def test_too_long_drops_the_edge_sentence_that_overlaps_the_models_range_less() -> None:
    first_start, _ = sentence_times(1)
    _, last_end = sentence_times(4)
    # the model's range only touches the last word of sentence 1 but covers sentence 4 fully
    result = snapped(run_snap(EIGHT, ws(7), last_end, min_length=5.0, max_length=9.0))
    assert result.first_word >= 8  # sentence 1 was dropped first
    assert result.end - result.start <= 9.0
    assert any("dropped" in n and "at the start" in n for n in result.notes)
    assert first_start < ws(8)


def test_too_long_with_a_tie_keeps_the_first_sentence() -> None:
    first_start, _ = sentence_times(1)
    _, last_end = sentence_times(4)
    result = snapped(run_snap(EIGHT, first_start, last_end, min_length=5.0, max_length=9.0))
    assert result.first_word == 4  # the hook sentence stays
    assert any("at the end to fit the maximum" in n for n in result.notes)
    assert result.end - result.start <= 9.0


def test_a_single_sentence_over_the_maximum_is_dropped() -> None:
    spec = [" ".join(["word"] * 99 + ["end."])]  # 100 words, 79.7 s
    result = run_snap(spec, ws(10), ws(20), min_length=20.0, max_length=60.0, fallback_words=200)
    assert isinstance(result, Dropped)
    assert "longer than the 60 s maximum" in result.reason


def test_a_transcript_shorter_than_the_minimum_is_dropped() -> None:
    result = run_snap(["Just a few words. Then done."], 0.0, 5.0, min_length=20.0, max_length=60.0)
    assert isinstance(result, Dropped)


def test_shrinking_never_re_adds_a_removed_sentence() -> None:
    long = " ".join(["x"] * 13 + ["y."])  # 14 words = 11.1 s padded
    short = " ".join(["s"] * 3 + ["t."])  # 4 words = 3.3 s padded
    # all three are 25.7 s: over 14 s, so the last long one goes, then the short one; the lone
    # long sentence is under the 12 s minimum and the short one must not come back to fix that
    result = run_snap([long, short, long], ws(0), we(31), min_length=12.0, max_length=14.0)
    assert isinstance(result, Dropped)
    assert "shorter than the 12 s minimum" in result.reason


def test_empty_transcript_is_dropped() -> None:
    result = snap(
        build_index([]),
        0.0,
        10.0,
        start_pad=0.25,
        end_pad=0.5,
        duration=10.0,
        min_length=1.0,
        max_length=60.0,
    )
    assert isinstance(result, Dropped)


def test_snapped_spans_always_hold_the_invariants() -> None:
    rng = random.Random(7)
    spec = [
        " ".join(f"w{n}_{k}" + ("." if k == size - 1 else "") for k in range(size))
        for n, size in enumerate(rng.randint(2, 15) for _ in range(40))
    ]
    segments = build(spec, gap=0.2)  # at least min_cut_gap, so a full stop is a real end
    index = build_index(segments)
    duration = segments[-1].words[-1].end + 0.1
    for _ in range(400):
        a = rng.uniform(-5.0, duration)
        b = a + rng.uniform(0.1, 80.0)
        result = snap(
            index,
            a,
            b,
            start_pad=0.25,
            end_pad=0.5,
            duration=duration,
            min_length=8.0,
            max_length=25.0,
        )
        if isinstance(result, Dropped):
            continue
        words = index.words
        assert 0.0 <= result.start < result.end <= duration
        assert 8.0 - 1e-6 <= result.end - result.start <= 25.0 + 1e-6
        assert result.start <= words[result.first_word].start
        assert result.end >= words[result.last_word].end
        if result.first_word > 0:
            assert result.start >= words[result.first_word - 1].end - 1e-9
        if result.last_word + 1 < len(words):
            assert result.end <= words[result.last_word + 1].start + 1e-9
        assert ends_sentence(words[result.last_word].text) or result.edge_end


# --- text rebuild --------------------------------------------------------------------------------


def test_words_text_is_rebuilt_from_the_snapped_words() -> None:
    result = snapped(run_snap(FOUR, ws(4), ws(8)))
    index = build_index(build(FOUR))
    assert words_text(index, result.first_word, result.last_word) == (
        "Four five six seven. Eight nine ten."
    )


# --- dedupe --------------------------------------------------------------------------------------


def test_overlap_over_the_threshold_drops_the_later_one() -> None:
    spans = [(10.0, 40.0), (15.0, 45.0), (100.0, 130.0)]  # 25 of 30 s overlap
    assert dedupe(spans, 0.5) == [0, 2]


def test_overlap_at_or_under_the_threshold_is_kept() -> None:
    assert dedupe([(0.0, 20.0), (10.0, 30.0)], 0.5) == [0, 1]  # exactly 0.5
    assert dedupe([(0.0, 20.0), (15.0, 35.0)], 0.5) == [0, 1]  # 0.25


def test_overlap_is_measured_against_the_shorter_span() -> None:
    # the short one sits inside the long one: 100 % of the shorter
    assert dedupe([(0.0, 60.0), (10.0, 30.0)], 0.5) == [0]
    assert dedupe([(10.0, 30.0), (0.0, 60.0)], 0.5) == [0]  # earlier-listed wins, not longer


def test_earlier_listed_is_kept_and_a_dropped_span_does_not_block_others() -> None:
    spans = [(0.0, 30.0), (5.0, 35.0), (32.0, 62.0)]
    # span 1 is dropped (25/30); span 2 overlaps only span 1 (3 s of 30) and is kept
    assert dedupe(spans, 0.5) == [0, 2]


def test_touching_spans_do_not_overlap() -> None:
    assert dedupe([(0.0, 30.0), (30.0, 60.0)], 0.0) == [0, 1]


def test_dedupe_of_nothing() -> None:
    assert dedupe([], 0.5) == []


# --- span_times and locate (used by the score stage) ---------------------------------------------


PADS = {"start_pad": 0.25, "end_pad": 0.5}


def _index_of_three_sentences() -> tuple[object, float]:
    spec = ["one two three four.", "five six seven eight.", "nine ten eleven twelve."]
    segments = build(spec)
    return build_index(segments), segments[-1].end + 1.0


def test_span_times_matches_what_snap_produced() -> None:
    from clipmachine.utils.snapping import span_times

    index, duration = _index_of_three_sentences()
    snapped = snap(
        index,  # type: ignore[arg-type]
        0.0,
        100.0,
        start_pad=0.25,
        end_pad=0.5,
        duration=duration,  # type: ignore[arg-type]
        min_length=1.0,
        max_length=500.0,
    )
    assert isinstance(snapped, Snapped)
    start, end = span_times(
        index,  # type: ignore[arg-type]
        0,
        2,
        start_pad=0.25,
        end_pad=0.5,
        duration=duration,  # type: ignore[arg-type]
    )
    assert (start, end) == (snapped.start, snapped.end)


def test_locate_recovers_the_sentences_of_every_span() -> None:
    from clipmachine.utils.snapping import locate, span_times

    index, duration = _index_of_three_sentences()
    for a in range(3):
        for b in range(a, 3):
            start, end = span_times(index, a, b, **PADS, duration=duration)  # type: ignore[arg-type]
            found = locate(index, start, end, **PADS, duration=duration)  # type: ignore[arg-type]
            assert found == (a, b)


def test_locate_returns_none_for_times_that_are_not_a_snapped_span() -> None:
    from clipmachine.utils.snapping import locate

    index, duration = _index_of_three_sentences()
    assert locate(index, 0.123, 4.567, **PADS, duration=duration) is None  # type: ignore[arg-type]


def test_edge_flags_read_back_what_snap_wrote() -> None:
    from clipmachine.utils.snapping import EDGE_END_NOTE, EDGE_START_NOTE, edge_flags

    # three unpunctuated 45-word segments: with the 40-word fallback a segment edge is a boundary
    words = " ".join(f"w{k}" for k in range(45))
    result = run_snap([words, words, words], 40.0, 60.0, min_length=1.0, fallback_words=40)
    assert isinstance(result, Snapped)
    assert edge_flags(result.notes) == (result.edge_start, result.edge_end)
    assert edge_flags(["something else"]) == (False, False)
    assert edge_flags([EDGE_START_NOTE]) == (True, False)
    assert edge_flags([EDGE_END_NOTE]) == (False, True)


# --- mid-gap cut times (Brief 10) ----------------------------------------------------------------

# Words are 0.5 s long. Times here are multiples of 0.125 so the arithmetic is exact. The caps are
# the defaults: 0.25 s before the first word, 0.5 s after the last.
START_PAD = 0.25
END_PAD = 0.5


def times_of(
    index: SentenceIndex,
    a: int,
    b: int,
    *,
    start_pad: float = START_PAD,
    end_pad: float = END_PAD,
    duration: float = 100.0,
) -> tuple[float, float]:
    from clipmachine.utils.snapping import span_times

    return span_times(index, a, b, start_pad=start_pad, end_pad=end_pad, duration=duration)


def test_the_cut_is_in_the_middle_of_a_gap_smaller_than_twice_the_cap() -> None:
    # a. 0-0.5 | 0.25 gap | b. 0.75-1.25 | 0.5 gap | c. 1.75-2.25
    index = make_index([("a.", 0.25), ("b.", 0.5), ("c.", 0.0)])
    start, end = times_of(index, 1, 1)
    assert start == pytest.approx(0.75 - 0.125)  # half of the 0.25 s gap before
    assert end == pytest.approx(1.25 + 0.25)  # half of the 0.5 s gap after


@pytest.mark.parametrize(
    ("start_pad", "end_pad"), [(0.25, 0.5), (0.1, 0.3), (0.5, 0.1), (0.0, 0.0)]
)
def test_a_long_silence_gives_exactly_the_caps(start_pad: float, end_pad: float) -> None:
    # b. is 4.5-5.0 with 4 s of silence on each side
    index = make_index([("a.", 4.0), ("b.", 4.0), ("c.", 0.0)])
    start, end = times_of(index, 1, 1, start_pad=start_pad, end_pad=end_pad)
    assert start == pytest.approx(4.5 - start_pad)
    assert end == pytest.approx(5.0 + end_pad)


def test_the_start_of_the_transcript_uses_the_gap_from_zero() -> None:
    index = build_index(build(["Hi there."], origin=0.25))  # words 0.25-0.75 and 1.05-1.55
    assert times_of(index, 0, 0)[0] == pytest.approx(0.25 - 0.125)  # half of the 0.25 s before
    far = build_index(build(["Hi there."], origin=3.0))
    assert times_of(far, 0, 0)[0] == pytest.approx(3.0 - START_PAD)  # capped
    assert times_of(build_index(build(["Hi there."])), 0, 0)[0] == 0.0  # no gap at all


def test_the_end_of_the_transcript_uses_the_gap_to_the_duration() -> None:
    index = build_index(build(["Hi there."]))
    last_end = index.words[-1].end
    assert times_of(index, 0, 0, duration=last_end + 0.25)[1] == pytest.approx(last_end + 0.125)
    assert times_of(index, 0, 0, duration=last_end + 9.0)[1] == pytest.approx(last_end + END_PAD)
    assert times_of(index, 0, 0, duration=last_end)[1] == pytest.approx(last_end)


def test_overlapping_words_give_no_padding() -> None:
    # a. 0-0.5 | -0.125 | b. 0.375-0.875 | 0.5 | c. 1.375-1.875
    index = make_index([("a.", -0.125), ("b.", 0.5), ("c.", 0.0)])
    assert times_of(index, 0, 0)[1] == pytest.approx(0.5)  # the next word starts before it ends
    assert times_of(index, 1, 1)[0] == pytest.approx(0.375)  # the previous one ends after it starts
    # and the cut never crosses the words themselves
    for a in range(3):
        start, end = times_of(index, a, a)
        word = index.words[index.sentences[a].first]
        assert start <= word.start and end >= word.end


def test_locate_inverts_span_times_under_mid_gap_cuts() -> None:
    from clipmachine.utils.snapping import locate

    gaps = [0.5, 4.0, 0.25, -0.125, 1.0, 0.125, 0.75]
    items = [(f"w{k}.", gap) for k, gap in enumerate(gaps)] + [("end.", 0.0)]
    index = make_index(items)
    count = len(index.sentences)
    assert count == len(items)
    duration = 40.0
    for start_pad, end_pad in ((0.25, 0.5), (0.1, 0.3), (0.0, 0.0)):
        for a in range(count):
            for b in range(a, count):
                start, end = times_of(
                    index, a, b, start_pad=start_pad, end_pad=end_pad, duration=duration
                )
                found = locate(
                    index, start, end, start_pad=start_pad, end_pad=end_pad, duration=duration
                )
                assert found == (a, b)


# --- units (Brief 10) ----------------------------------------------------------------------------

UNIT_ITEMS = [
    ("a.", PAUSE),  # 0: a real end
    ("b.", 0.0),  # 1: an edge
    ("c.", 0.0),  # 2: an edge
    ("d.", PAUSE),  # 3: a real end
    ("e", QUARTER),
    ("f", PAUSE),  # 4: e f, ending at a pause
    ("g", QUARTER),
    ("h.", 0.0),  # 5: g h, the last sentence
]


def test_units_merge_the_sentences_between_cuttable_boundaries() -> None:
    index = make_index(UNIT_ITEMS, fallback=2)
    assert [s.end for s in index.sentences] == [".", END_EDGE, END_EDGE, ".", END_PAUSE, "."]
    units = index.units()
    assert spans(units) == [(0, 0), (1, 3), (4, 5), (6, 7)]
    # the kind and the gap of a unit are those of its last sentence
    assert [s.end for s in units.sentences] == [".", ".", END_PAUSE, "."]
    assert [s.gap for s in units.sentences] == [PAUSE, PAUSE, PAUSE, math.inf]
    assert units.firsts == (0, 1, 4, 6)
    assert units.words is index.words  # the same word indices
    assert all(units.cut_before(n) and units.cut_after(n) for n in range(len(units.sentences)))


def test_units_of_a_transcript_of_edges_is_one_unit_closed_by_the_last_sentence() -> None:
    index = make_index([("a.", 0.0), ("b.", 0.0), ("c.", 0.0)])
    assert [s.end for s in index.sentences] == [END_EDGE, END_EDGE, "."]
    assert spans(index.units()) == [(0, 2)]


def test_units_of_real_ends_are_the_sentences_themselves() -> None:
    index = make_index([("a.", PAUSE), ("b.", PAUSE), ("c.", 0.0)])
    assert spans(index.units()) == spans(index)


def test_units_of_an_empty_index_is_empty() -> None:
    units = build_index([]).units()
    assert units.sentences == () and units.firsts == () and units.words == ()


def random_items(rng: random.Random, count: int) -> list[tuple[str, float]]:
    """Words with random end punctuation and gaps from tiny to long, so some ends are edges."""
    items = []
    for k in range(count):
        token = f"w{k}" + ("." if rng.random() < 0.3 else "")
        items.append((token, rng.choice([0.0, 0.125, 0.25, 0.5, 1.0, -0.125])))
    return items


def test_snapping_over_units_never_starts_or_ends_on_an_edge() -> None:
    rng = random.Random(11)
    index = make_index(random_items(rng, 400), fallback=12)
    assert any(not index.cut_after(n) for n in range(len(index.sentences)))  # edges exist
    units = index.units()
    duration = index.words[-1].end + 1.0
    produced = 0
    for _ in range(400):
        a = rng.uniform(-5.0, duration)
        b = a + rng.uniform(0.1, 60.0)
        result = snap(
            units,
            a,
            b,
            start_pad=START_PAD,
            end_pad=END_PAD,
            duration=duration,
            min_length=4.0,
            max_length=40.0,
        )
        if isinstance(result, Dropped):
            continue
        produced += 1
        first = index.sentence_of(result.first_word)
        last = index.sentence_of(result.last_word)
        assert index.sentences[first].first == result.first_word
        assert index.sentences[last].last == result.last_word
        assert index.cut_before(first)
        assert index.cut_after(last)
        assert (result.edge_start, result.edge_end) == (False, False)
        assert EDGE_START_NOTE not in result.notes and EDGE_END_NOTE not in result.notes
        assert 0.0 <= result.start < result.end <= duration
        assert 4.0 - 1e-6 <= result.end - result.start <= 40.0 + 1e-6
    assert produced > 20


# 0 a. (real) | 1..10 ten edge sentences, 0.5 s each with no gap | 11 z. (real) | 12 q. (last)
LONG_STRETCH = [("a.", PAUSE)] + [(f"b{k}.", 0.0) for k in range(10)] + [("z.", PAUSE), ("q.", 0.0)]


def test_a_moment_inside_an_uncuttable_stretch_extends_to_the_nearest_clean_cuts() -> None:
    index = make_index(LONG_STRETCH)
    assert spans(index.units()) == [(0, 0), (1, 11), (12, 12)]
    result = snapped(
        snap(
            index.units(),
            2.0,
            3.0,  # inside b2..b4
            start_pad=START_PAD,
            end_pad=END_PAD,
            duration=20.0,
            min_length=1.0,
            max_length=20.0,
        )
    )
    assert (result.first_word, result.last_word) == (1, 11)
    assert result.start == pytest.approx(1.0 - 0.25)  # b0 starts at 1.0, 0.5 s after a.
    assert result.end == pytest.approx(6.5 + 0.25)  # z. ends at 6.5, 0.5 s before q.
    assert (result.edge_start, result.edge_end) == (False, False)
    # over the sentence index the same moment would be cut at edges
    loose = snapped(
        snap(
            index,
            2.0,
            3.0,
            start_pad=START_PAD,
            end_pad=END_PAD,
            duration=20.0,
            min_length=0.1,
            max_length=20.0,
        )
    )
    assert (loose.edge_start, loose.edge_end) == (True, True)


def test_a_moment_inside_an_uncuttable_stretch_is_dropped_when_the_stretch_is_too_long() -> None:
    index = make_index(LONG_STRETCH)
    result = snap(
        index.units(),
        2.0,
        3.0,
        start_pad=START_PAD,
        end_pad=END_PAD,
        duration=20.0,
        min_length=1.0,
        max_length=5.0,  # the stretch needs 6.0 s
    )
    assert isinstance(result, Dropped)
    assert "no clean cut point" in result.reason
    assert "longer than the 5 s maximum" in result.reason


# --- snap_near and auto_end (Brief 11, campaign mode) ---------------------------------------------

NEAR_PADS = {"start_pad": 0.25, "end_pad": 0.5, "duration": 100.0}
# Three sentences, each a unit; the cut times (mid-gap, capped at the pads) worked out by hand:
#   a b.  0-1.25   | c d.  2.25-3.5   | e f.  4.5-5.75   (gap 1.0 after each full stop)
#   starts 0, 2.0, 4.25 and ends 1.75, 4.0, 6.25
THREE = [
    ("a", 0.25),
    ("b.", 1.0),
    ("c", 0.25),
    ("d.", 1.0),
    ("e", 0.25),
    ("f.", 1.0),
]
# A full stop inside continuous speech (gap 0.125 < min_cut_gap) is a weak end: b. is not a cut.
# Units are a..d. (cuts 0 and 3.125) and e..f. (cuts 3.375 and 5.375).
WEAK = [
    ("a", 0.25),
    ("b.", 0.125),
    ("c", 0.25),
    ("d.", 1.0),
    ("e", 0.25),
    ("f.", 1.0),
]


def near(
    items: list[tuple[str, float]], start: float, end: float | None, max_move: float
) -> NearSnap:
    return snap_near(make_index(items).units(), start, end, max_move=max_move, **NEAR_PADS)


def test_a_start_moves_to_the_nearest_clean_cut_in_both_directions() -> None:
    forward = near(THREE, 1.5, 4.0, 1.0)  # candidates 0 (1.5 away) and 2.0 (0.5 away)
    assert forward.start == 2.0 and forward.start_moved == pytest.approx(0.5)
    assert forward.notes[0] == "start moved +0.50 s to a clean sentence edge"
    backward = near(THREE, 2.4, 4.0, 1.0)
    assert backward.start == 2.0 and backward.start_moved == pytest.approx(-0.4)
    assert backward.notes[0] == "start moved -0.40 s to a clean sentence edge"


def test_a_start_exactly_at_the_limit_moves_and_just_beyond_it_is_kept() -> None:
    at_limit = near(THREE, 3.0, 6.0, 1.0)  # 2.0 is exactly 1.0 away, 4.25 is 1.25 away
    assert at_limit.start == 2.0 and at_limit.start_moved == pytest.approx(-1.0)
    beyond = near(THREE, 3.0, 6.0, 0.99)
    assert beyond.start == 3.0 and beyond.start_moved is None
    assert beyond.notes[0] == f"{START_KEPT_NOTE} 0.99 s"


def test_start_and_end_are_decided_on_their_own() -> None:
    result = near(THREE, 2.4, 5.0, 0.5)  # the start reaches 2.0; no end cut within 0.5 of 5.0
    assert (result.start, result.end) == (2.0, 5.0)
    assert result.start_moved == pytest.approx(-0.4) and result.end_moved is None
    assert result.notes == (
        "start moved -0.40 s to a clean sentence edge",
        f"{END_KEPT_NOTE} 0.5 s",
    )
    other = near(THREE, 1.0, 4.2, 0.5)  # no start cut within 0.5 of 1.0; the end reaches 4.0
    assert (other.start, other.end) == (1.0, 4.0)
    assert other.start_moved is None and other.end_moved == pytest.approx(-0.2)
    assert other.notes == (f"{START_KEPT_NOTE} 0.5 s", "end moved -0.20 s to a clean sentence edge")


def test_the_end_moves_to_the_nearest_end_cut_in_both_directions() -> None:
    assert near(THREE, 2.0, 4.4, 1.0).end == 4.0
    assert near(THREE, 2.0, 3.8, 1.0).end == 4.0
    assert near(THREE, 2.0, 6.0, 1.0).end == 6.25


def test_a_tie_goes_to_the_earlier_cut() -> None:
    # 3.125 is 1.125 from both start cuts (2.0 and 4.25) and from both end cuts (4.0 and 2.25...)
    result = near(THREE, 3.125, 5.125, 1.125)  # end cuts 4.0 and 6.25 are both 1.125 away
    assert result.start == 2.0 and result.end == 4.0


def test_the_end_never_lands_at_or_before_the_start() -> None:
    # the start goes to 4.25; the end cut at 4.0 is before it, and 6.25 is too far
    kept = near(THREE, 4.1, 4.2, 0.5)
    assert kept.start == 4.25 and kept.end == 4.2 and kept.end_moved is None
    assert kept.notes[-1] == f"{END_KEPT_NOTE} 0.5 s"
    far = near(THREE, 4.1, 4.2, 3.0)
    assert far.start == 4.25 and far.end == 6.25


def test_a_weak_boundary_is_not_a_cut() -> None:
    index = make_index(WEAK).units()
    assert spans(index) == [(0, 3), (4, 5)]
    start = near(WEAK, 1.375, None, 0.5)  # c starts here, after the weak full stop
    assert start.start == 1.375 and start.start_moved is None
    end = near(WEAK, 0.0, 1.3, 0.5)  # b. ends at 1.25, but that is not a cut
    assert end.end == 1.3 and end.end_moved is None
    assert near(WEAK, 3.4, 5.4, 0.5).start == 3.375


def test_no_end_leaves_the_end_alone() -> None:
    result = near(THREE, 2.4, None, 1.0)
    assert result.end is None and result.end_moved is None
    assert result.start == 2.0
    assert result.notes == ("start moved -0.40 s to a clean sentence edge",)
    kept = near(THREE, 3.0, None, 0.1)
    assert kept.end is None and kept.notes == (f"{START_KEPT_NOTE} 0.1 s",)


def test_an_empty_transcript_keeps_both_times() -> None:
    result = snap_near(build_index([]).units(), 5.0, 9.0, max_move=2.0, **NEAR_PADS)
    assert (result.start, result.end) == (5.0, 9.0)
    assert result.start_moved is None and result.end_moved is None
    assert result.notes == (f"{START_KEPT_NOTE} 2 s", f"{END_KEPT_NOTE} 2 s")


def test_a_move_under_five_milliseconds_has_no_note_but_is_recorded() -> None:
    result = near(THREE, 2.002, 4.001, 0.5)
    assert result.start == 2.0 and result.start_moved == pytest.approx(-0.002)
    assert result.end == 4.0 and result.end_moved == pytest.approx(-0.001)
    assert result.notes == ()


def test_a_limit_of_zero_only_moves_onto_an_exact_edge() -> None:
    exact = near(THREE, 2.0, 4.0, 0)
    assert (exact.start, exact.end) == (2.0, 4.0)
    assert exact.start_moved == 0 and exact.end_moved == 0 and exact.notes == ()
    off = near(THREE, 2.1, 4.1, 0)
    assert (off.start, off.end) == (2.1, 4.1)
    assert off.start_moved is None and off.end_moved is None
    assert off.notes == (f"{START_KEPT_NOTE} 0 s", f"{END_KEPT_NOTE} 0 s")


def auto(
    items: list[tuple[str, float]],
    start: float,
    target: float,
    *,
    min_length: float = 1.0,
    max_length: float = 100.0,
    duration: float = 100.0,
) -> AutoEnd | None:
    return auto_end(
        make_index(items).units(),
        start,
        target=target,
        min_length=min_length,
        max_length=max_length,
        start_pad=0.25,
        end_pad=0.5,
        duration=duration,
    )


def test_auto_end_picks_the_end_nearest_to_the_target_before_or_after_it() -> None:
    before = auto(THREE, 2.0, 3.0)  # target end 5.0: 4.0 is 1.0 away, 6.25 is 1.25 away
    assert before == AutoEnd(end=4.0, target_end=5.0, length=2.0)
    after = auto(THREE, 2.0, 3.5)  # target end 5.5: 4.0 is 1.5 away, 6.25 is 0.75 away
    assert after == AutoEnd(end=6.25, target_end=5.5, length=4.25)


def test_auto_end_skips_an_end_below_the_minimum_even_when_it_is_nearer() -> None:
    result = auto(THREE, 2.0, 2.0, min_length=3.0)  # 4.0 would be the target, but is 2.0 long
    assert result is not None and result.end == 6.25


def test_auto_end_skips_an_end_above_the_maximum_even_when_it_is_nearer() -> None:
    result = auto(THREE, 2.0, 4.25, max_length=3.0)  # 6.25 would be the target; it is 4.25 long
    assert result is not None and result.end == 4.0


def test_auto_end_length_limits_are_inclusive() -> None:
    assert auto(THREE, 2.0, 2.0, min_length=2.0, max_length=2.0) == AutoEnd(4.0, 4.0, 2.0)


def test_auto_end_tie_goes_to_the_earlier_end() -> None:
    result = auto(THREE, 2.0, 3.125)  # target end 5.125 is 1.125 from 4.0 and from 6.25
    assert result is not None and result.end == 4.0


def test_auto_end_is_none_when_no_end_is_in_range() -> None:
    assert auto(THREE, 2.0, 3.0, min_length=5.0, max_length=6.0) is None
    assert auto(THREE, 2.0, 3.0, min_length=0.5, max_length=1.5) is None
    assert auto(THREE, 100.0, 3.0) is None  # nothing ends after this start
    assert (
        auto_end(
            build_index([]).units(),
            0.0,
            target=3.0,
            min_length=1.0,
            max_length=9.0,
            start_pad=0.25,
            end_pad=0.5,
            duration=10.0,
        )
        is None
    )


def test_auto_end_never_uses_a_weak_boundary() -> None:
    # b. ends at 1.25, exactly the target end, but it is not a cut: the nearest cut is 3.125
    result = auto(WEAK, 0.0, 1.25, min_length=0.5)
    assert result is not None and result.end == 3.125


def test_auto_end_can_pick_the_last_sentence_and_its_cut_stays_inside_the_audio() -> None:
    # f. ends at 5.75; with the audio ending at 5.8 the pause after it is 0.05 s, so the cut is
    # 0.025 s after the word (never past the audio), not the 0.5 s cap
    result = auto(THREE, 4.25, 2.0, min_length=1.0, max_length=3.0, duration=5.8)
    assert result is not None
    assert result.end == pytest.approx(5.775) and result.end <= 5.8
    assert result.target_end == 6.25 and result.length == pytest.approx(1.525)
