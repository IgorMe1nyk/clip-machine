"""Caption word fixes and names to check (Brief 11), on invented words."""

from pathlib import Path

import pytest

from clipmachine.campaign.models import SpellingRule
from clipmachine.campaign.spelling import apply_spelling, find_names
from clipmachine.stages.candidates import _load_transcript
from clipmachine.utils.captions import CaptionWord, clip_words
from test_render import write_inputs


def rule(source: str, to: str) -> SpellingRule:
    return SpellingRule.model_validate({"from": source, "to": to})


def cw(text: str, start: float = 0.0, end: float | None = None) -> CaptionWord:
    return CaptionWord(text, start, start + 0.4 if end is None else end)


def seq(*texts: str) -> list[CaptionWord]:
    return [cw(t, start=i * 0.5) for i, t in enumerate(texts)]


def texts(words: list[CaptionWord]) -> list[str]:
    return [w.text for w in words]


ZORBLE = rule("zorble", "Zorb1e")
QXV = rule("q x v", "QXV")


def test_matching_is_case_insensitive_and_the_replacement_is_the_rules() -> None:
    out, count = apply_spelling(seq("a", "ZORBLE", "b", "Zorble", "zorble"), [ZORBLE])
    assert texts(out) == ["a", "Zorb1e", "b", "Zorb1e", "Zorb1e"]
    assert count == 3


def test_punctuation_is_kept_on_both_sides() -> None:
    out, count = apply_spelling(seq('"zorble,', "(Zorble)", "zorble?!", "“ZORBLE”."), [ZORBLE])
    assert texts(out) == ['"Zorb1e,', "(Zorb1e)", "Zorb1e?!", "“Zorb1e”."]
    assert count == 4


@pytest.mark.parametrize("mark", ["'s", "’s", "'S"])
def test_a_trailing_possessive_is_kept(mark: str) -> None:
    out, count = apply_spelling(seq(f"Zorble{mark},"), [ZORBLE])
    assert texts(out) == [f"Zorb1e{mark},"] and count == 1


def test_only_whole_words_match() -> None:
    words = seq("zorbles", "azorble", "zorble-x", "zorbled", "un-zorble")
    out, count = apply_spelling(words, [ZORBLE])
    assert out == words and count == 0


def test_an_all_punctuation_word_never_matches() -> None:
    rule_dash = rule("a", "B")
    words = seq("--", "...", "—")
    assert apply_spelling(words, [rule_dash]) == (words, 0)


def test_a_multi_word_rule_merges_into_one_word_with_exact_times() -> None:
    words = [
        cw("so", 0.0, 0.3),
        cw("q", 1.0, 1.2),
        cw("X", 1.3, 1.5),
        cw("v", 1.6, 2.1),
        cw("ok", 2.5, 2.8),
    ]
    out, count = apply_spelling(words, [QXV])
    assert count == 1
    assert out == [cw("so", 0.0, 0.3), CaptionWord("QXV", 1.0, 2.1), cw("ok", 2.5, 2.8)]


def test_a_multi_word_match_keeps_the_first_leading_and_last_trailing_punctuation() -> None:
    out, _ = apply_spelling(seq('"q', "x", "v’s,"), [QXV])
    assert texts(out) == ['"QXV’s,']


def test_dots_and_commas_after_the_inner_letters_are_fine() -> None:
    out, count = apply_spelling(seq("Q.", "X,", "V."), [QXV])
    assert texts(out) == ["QXV."] and count == 1


@pytest.mark.parametrize(
    "bad", [("q?", "x", "v"), ("q", "x!", "v"), ("q", "x-", "v"), ("q", "x’s", "v")]
)
def test_sentence_ending_or_other_marks_inside_a_multi_word_match_block_it(
    bad: tuple[str, ...],
) -> None:
    words = seq(*bad)
    assert apply_spelling(words, [QXV]) == (words, 0)


def test_a_multi_word_match_cannot_run_past_the_end() -> None:
    words = seq("q", "x")
    assert apply_spelling(words, [QXV]) == (words, 0)


def test_a_longer_rule_wins_over_a_shorter_one_that_overlaps() -> None:
    long_rule, short_rule = rule("b c d", "LONG"), rule("a b", "SHORT")
    out, count = apply_spelling(seq("a", "b", "c", "d"), [short_rule, long_rule])
    assert texts(out) == ["a", "LONG"] and count == 1
    out, count = apply_spelling(seq("b", "c", "d"), [rule("b", "ONE"), long_rule])
    assert texts(out) == ["LONG"] and count == 1


def test_a_replacement_is_not_matched_again() -> None:
    chain = [rule("alpha", "beta"), rule("beta", "gamma")]
    out, count = apply_spelling(seq("alpha", "beta"), chain)
    assert texts(out) == ["beta", "gamma"] and count == 2


def test_the_count_is_the_number_of_replacements_not_of_words() -> None:
    out, count = apply_spelling(seq("q", "x", "v", "zorble", "q", "x", "v"), [ZORBLE, QXV])
    assert texts(out) == ["QXV", "Zorb1e", "QXV"] and count == 3


def test_no_rules_returns_equal_words_and_zero() -> None:
    words = seq("zorble", "q", "x", "v")
    out, count = apply_spelling(words, [])
    assert out == words and count == 0


def test_the_input_list_is_not_changed() -> None:
    words = seq("zorble", "q", "x", "v")
    before = list(words)
    apply_spelling(words, [ZORBLE, QXV])
    assert words == before


def test_find_names_gives_rounded_clip_relative_start_times() -> None:
    words = [cw("go"), cw("Zorb1e,", 1.2349), cw("and"), cw("zorb1e's", 7.0), cw("x", 9.0)]
    got = find_names(words, ["Zorb1e", "Other"])
    assert got == {"Zorb1e": [1.23, 7.0], "Other": []}
    assert list(got) == ["Zorb1e", "Other"]


def test_find_names_with_spaces_matches_consecutive_words() -> None:
    words = [cw("go"), cw("Mia", 2.0), cw("Vale.", 2.5), cw("then", 3.0), cw("mia", 4.0)]
    assert find_names(words, ["Mia Vale", "mia"]) == {"Mia Vale": [2.0], "mia": [2.0, 4.0]}


def test_find_names_does_not_match_part_of_a_word() -> None:
    assert find_names(seq("qxvs", "aqxv"), ["QXV"]) == {"QXV": []}


def test_find_names_runs_after_the_spelling_fix() -> None:
    words, _ = apply_spelling(seq("so", "q", "x", "v", "zorble"), [ZORBLE, QXV])
    assert find_names(words, ["QXV", "Zorb1e"]) == {"QXV": [0.5], "Zorb1e": [2.0]}


def test_the_transcript_file_is_never_changed(project: Path) -> None:
    run_dir = project / "runs" / "run"
    run_dir.mkdir()
    write_inputs(run_dir)
    path = run_dir / "transcript.json"
    before = path.read_bytes()

    transcript = _load_transcript(run_dir)
    words = clip_words(transcript.segments, 10.0, 30.0)
    assert words
    fixes = [rule("w12", "Zorb1e"), rule("w14 w16", "MERGED")]
    out, count = apply_spelling(words, fixes)

    assert count == 2 and len(out) == len(words) - 1
    assert path.read_bytes() == before
    assert "Zorb1e" not in path.read_text(encoding="utf-8")
