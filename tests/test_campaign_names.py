"""Name variants from the transcript (Brief 11.1), on invented words."""

import pytest

from clipmachine.campaign.models import SpellingRule
from clipmachine.campaign.names import (
    Variant,
    auto_rules,
    classify,
    find_variants,
    is_inflection,
    normalise,
    record,
    soundex,
)
from clipmachine.models import TranscriptSegment, TranscriptWord
from clipmachine.utils.snapping import SentenceIndex, build_index


def rule(source: str, to: str) -> SpellingRule:
    return SpellingRule.model_validate({"from": source, "to": to})


def index_of(*sentences: str) -> SentenceIndex:
    """A sentence index of the given sentences (words split at spaces), 0.5 s per word, with a
    pause of 1 s between sentences."""
    segments = []
    t = 0.0
    for k, sentence in enumerate(sentences):
        words = []
        for text in sentence.split():
            words.append(TranscriptWord(start=t, end=t + 0.4, word=f" {text}", probability=0.9))
            t += 0.5
        t += 1.0
        segments.append(
            TranscriptSegment(
                id=k,
                start=words[0].start,
                end=words[-1].end,
                text="",
                avg_logprob=0.0,
                no_speech_prob=0.0,
                words=words,
            )
        )
    return build_index(segments)


def variants(index: SentenceIndex, *names: str) -> list[Variant]:
    return find_variants(index, names, apply_threshold=0.8, suggest_threshold=0.7)


# --- the pieces ----------------------------------------------------------------------------


def test_normalise_lowers_maps_digit_lookalikes_and_drops_punctuation() -> None:
    assert normalise("N3on!") == "neon"
    assert normalise("M.P.J") == "mpj"
    assert normalise("Z0rb1e's") == "zorbies"
    assert normalise("...") == ""


def test_soundex_is_the_basic_american_one() -> None:
    assert soundex("Robert") == soundex("Rupert") == "R163"
    assert soundex("Tymczak") == "T522"
    assert soundex("Pfister") == "P236"
    assert soundex("Ashcraft") == "A261"  # h does not separate s and c
    assert soundex("Lee") == "L000"
    assert soundex("123") == "" and soundex("") == ""


@pytest.mark.parametrize(
    ("span", "name", "status"),
    [
        ("zorble", "Zorb1e", "apply"),  # 0.83: a longer name needs no sound match
        ("zorbie", "Zorb1e", "apply"),  # the same letters, another spelling of the digit
        ("z0rble", "Zorb1e", "apply"),
        ("frab", "Frob", "suggest"),  # 0.75 with the same sound: listed, not applied
        ("frobs", "Frob", None),  # 0.89 but another sound: a short name needs both
        ("from", "Frob", None),  # an ordinary word: 0.75, another sound
        ("on", "Frob", None),
        ("gorbal twee", "Gorbal Twee", "apply"),
        ("nin", "Nion", "suggest"),  # 0.86 and the same sound, but 3 letters: a suggestion at most
        ("q x v", "QXV", "suggest"),  # exact letters, 3 of them: a suggestion at most
        ("nib", "Nion", None),  # 3 letters and another sound: dropped as before
    ],
)
def test_classify(span: str, name: str, status: str | None) -> None:
    assert classify(span, name, apply_threshold=0.8, suggest_threshold=0.7)[2] == status


def test_the_verb_in_a_show_name_is_never_spelled_as_the_name() -> None:
    # found in Batch A: names [..., Solved] turned "solve" and "solves" into "Solved" (S42)
    for span in ("solve", "solves", "solving", "Solve"):
        assert classify(span, "Solved", apply_threshold=0.8, suggest_threshold=0.7)[2] is None
    index = index_of("we solve it.", "she solves them.", "solving is hard.", "the Solved show.")
    assert variants(index, "Solved") == []
    assert classify("zorbi", "Zorbie", apply_threshold=0.8, suggest_threshold=0.7)[2] == "apply"


@pytest.mark.parametrize(
    ("span", "name"),
    [
        ("davod", "Davos"),
        ("holms", "Holmes"),
        ("hughs", "Hughes"),
        ("anthony david", "Anthony Davis"),
    ],
)
def test_a_name_not_shaped_like_a_verb_keeps_its_variants(span: str, name: str) -> None:
    # review of the fix above: ending-only differences must not hide real misspellings of names
    assert classify(span, name, apply_threshold=0.8, suggest_threshold=0.7)[2] == "apply"
    assert not is_inflection(span, name)
    assert is_inflection("solving", "Solved") and not is_inflection("so lived", "Solved")


def test_a_span_under_four_letters_is_never_applied() -> None:
    index = index_of("nin s1 nin s3.", "the nion show.")
    found = variants(index, "Nion")
    assert [(v.text, v.count, v.status) for v in found] == [("nin", 2, "suggest")]
    rules, covered = auto_rules(found, [])
    assert rules == [] and covered == []


# --- scanning the transcript ---------------------------------------------------------------


def test_variants_are_found_as_single_words_and_as_split_words() -> None:
    index = index_of("s0 z0rble s2 s3.", "zor ble s6 Z0RBLE.", "s8 Zorb1e s10.")
    found = variants(index, "Zorb1e")
    assert [(v.text, v.name, v.count, v.status) for v in found] == [
        ("z0rble", "Zorb1e", 2, "apply"),
        ("zor ble", "Zorb1e", 1, "apply"),
    ]
    assert found[0].ratio == 0.833 and found[0].soundex_match is False  # Z614 vs Z610


def test_the_exact_spelling_is_not_a_variant_and_a_sentence_end_is_not_crossed() -> None:
    index = index_of("s0 Zorb1e s2.", "zor.", "ble s5 s6.")
    assert variants(index, "Zorb1e") == []


def test_a_name_or_variant_with_its_neighbour_is_not_a_second_variant() -> None:
    # "s8 zorbie" is 0.86 close to Zorb1e only because it holds the name's own spelling
    index = index_of("s8 zorbie sign.", "the Zorb1e show.")
    assert [(v.text, v.count) for v in variants(index, "Zorb1e")] == [("zorbie", 1)]


def test_a_two_word_variant_with_its_neighbour_is_not_a_third_variant() -> None:
    # found in the real run: "i'm curious like" (0.83) is "curious like" (0.91) plus a word
    index = index_of("so i'm gorbal twinkel today.", "gorbal twinkel s2.")
    found = variants(index, "Gorbal Twinkle")
    assert [(v.text, v.count, v.status) for v in found] == [("gorbal twinkel", 2, "apply")]


def test_suggestions_come_after_the_applied_ones_and_a_short_name_needs_the_sound() -> None:
    index = index_of("frab s1 frobs.", "from s4 frab.", "frob s7.")
    found = variants(index, "Frob")
    assert [(v.text, v.count, v.status) for v in found] == [("frab", 2, "suggest")]


def test_a_span_is_counted_for_the_name_it_fits_best() -> None:
    index = index_of("zorble s1 frab.")
    found = variants(index, "Frob", "Zorb1e")
    assert [(v.text, v.name) for v in found] == [("zorble", "Zorb1e"), ("frab", "Frob")]


def test_no_names_means_no_variants() -> None:
    assert variants(index_of("zorble s1."), *()) == []


# --- rules and the record ------------------------------------------------------------------


def test_auto_rules_apply_variants_unless_an_explicit_rule_has_the_same_from() -> None:
    found = variants(index_of("z0rble s1 zor ble frab."), "Zorb1e", "Frob")
    rules, covered = auto_rules(found, [rule("z0rble", "z0rble")])  # an identity rule: a veto
    assert [(r.source, r.to) for r in rules] == [("zor ble", "Zorb1e")]
    assert [v.text for v in covered] == ["z0rble"]
    rules, covered = auto_rules(found, [])
    assert [(r.source, r.to) for r in rules] == [("z0rble", "Zorb1e"), ("zor ble", "Zorb1e")]
    assert covered == []  # the suggestion "frab" makes no rule and is not vetoed


def test_the_record_holds_statuses_rules_and_up_to_three_places() -> None:
    found = variants(index_of("z0rble zor ble frab."), "Zorb1e", "Frob")
    _, covered = auto_rules(found, [rule("zor ble", "QXV")])
    heard = {
        "z0rble": [
            ("A01_tiktok", 1.0),
            ("A02_youtube", 2.0),
            ("A03_tiktok", 3.0),
            ("B01_tiktok", 4.0),
        ]
    }
    auto = record(
        found,
        covered,
        heard,
        names=["Zorb1e", "Frob"],
        apply_threshold=0.8,
        suggest_threshold=0.7,
        source_sha256="b" * 64,
        written_at="2026-10-12T00:00:00+00:00",
    )
    by_text = {v.text: v for v in auto.variants}
    assert by_text["z0rble"].status == "apply" and by_text["z0rble"].rule_to == "Zorb1e"
    assert [(s.clip, s.at) for s in by_text["z0rble"].seen] == [
        ("A01_tiktok", 1.0),
        ("A02_youtube", 2.0),
        ("A03_tiktok", 3.0),
    ]
    assert by_text["zor ble"].status == "explicit" and by_text["zor ble"].rule_to is None
    assert by_text["frab"].status == "suggest" and by_text["frab"].seen == []
    assert auto.names == ["Zorb1e", "Frob"] and auto.schema_version == 1
    assert auto.written_at == "2026-10-12T00:00:00+00:00"
