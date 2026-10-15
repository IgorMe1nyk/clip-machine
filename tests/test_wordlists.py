"""Word lists (Brief 11.1): parsing, masking and flagging, on invented words only."""

from pathlib import Path

import pytest

from clipmachine.utils.captions import CaptionWord
from clipmachine.utils.wordlists import (
    WordListError,
    WordLists,
    inflections,
    load_wordlists,
    mask_text,
    mask_words,
    parse_list,
    sensitive_hits,
    split_word,
)
from conftest import REPO_ROOT

MASK = """
# invented profanity
blorp
glim   # a comment after the entry
quux faddle
context: zib
slur: vexor
"""
FLAG = """
# invented sensitive words
wump
tral
spoon dome
"""
NEVER = """
glimmer
blorpsy
"""


def write_lists(root: Path, *, mask: str = MASK, flag: str = FLAG, never: str = NEVER) -> Path:
    folder = root / "config" / "wordlists"
    folder.mkdir(parents=True)
    (folder / "mask.txt").write_text(mask, encoding="utf-8")
    (folder / "flag.txt").write_text(flag, encoding="utf-8")
    (folder / "never.txt").write_text(never, encoding="utf-8")
    return root


@pytest.fixture
def lists(tmp_path: Path) -> WordLists:
    return load_wordlists(write_lists(tmp_path))


def words(*texts: str) -> list[CaptionWord]:
    return [CaptionWord(t, i * 0.5, i * 0.5 + 0.4) for i, t in enumerate(texts)]


# --- files and parsing ---------------------------------------------------------------------


def test_the_files_parse_into_the_five_sets(lists: WordLists) -> None:
    assert lists.mask == {("blorp",), ("glim",), ("quux", "faddle")}
    assert lists.context == {("zib",)}
    assert lists.slurs == {("vexor",)}
    assert lists.flag == {("wump",), ("tral",), ("spoon", "dome")}
    assert lists.never == {"glimmer", "blorpsy"}


def test_parse_list_handles_comments_blank_lines_prefixes_and_case() -> None:
    assert parse_list("# only a comment\n\n  Foo Bar  \ncontext: Baz # note\n") == [
        ("", ("foo", "bar")),
        ("context", ("baz",)),
    ]


def test_a_missing_file_and_a_bad_prefix_are_errors(tmp_path: Path) -> None:
    with pytest.raises(WordListError, match="word list not found: config/wordlists/mask.txt"):
        load_wordlists(tmp_path)
    write_lists(tmp_path, mask="other: thing\n")
    with pytest.raises(WordListError, match="unknown prefix 'other'"):
        load_wordlists(tmp_path)


def test_flag_refuses_a_prefix_and_never_wants_single_words(tmp_path: Path) -> None:
    write_lists(tmp_path, flag="slur: nope\n")
    with pytest.raises(WordListError, match="flag.txt: a prefix"):
        load_wordlists(tmp_path)
    write_lists(tmp_path / "two", never="two words\n")
    with pytest.raises(WordListError, match="never.txt: one plain word"):
        load_wordlists(tmp_path / "two")


def test_inflections_cover_the_common_endings() -> None:
    forms = inflections("blorp")
    assert {"blorp", "blorps", "blorped", "blorping", "blorper", "blorpers", "blorpin"} <= forms
    assert {"blorpped", "blorpping", "blorpper"} <= forms  # doubled final consonant
    assert "blorpies" not in forms
    assert {"flossy", "flossies", "flossied"} <= inflections("flossy")
    assert "flossyy" not in inflections("flossy")
    assert "zoo" in inflections("zoo") and "zooing" in inflections("zoo")
    assert "zoooing" not in inflections("zoo")  # a vowel is not doubled


def test_split_word_keeps_punctuation_and_a_possessive_apart() -> None:
    assert split_word("\"Blorp's,") == ('"', "Blorp", "'s,")
    assert split_word("...") == ("...", "", "")
    assert split_word("x") == ("", "x", "")


# --- masking -------------------------------------------------------------------------------


def test_a_hit_keeps_its_first_letter_its_case_and_its_punctuation(lists: WordLists) -> None:
    out, hits = mask_words(words("Oh,", '"Blorp!"', "glim.", "fine"), lists)
    assert [w.text for w in out] == ["Oh,", '"B***!"', "g***.", "fine"]
    assert [(w.start, w.end) for w in out] == [(0.0, 0.4), (0.5, 0.9), (1.0, 1.4), (1.5, 1.9)]
    assert [(h.word, h.kind) for h in hits] == [("Blorp", "mask"), ("glim", "mask")]


def test_inflections_are_masked_but_not_a_longer_word(lists: WordLists) -> None:
    out, _ = mask_words(words("blorping", "BLORPS", "blorped", "blorpish", "unblorp"), lists)
    assert [w.text for w in out] == ["b***", "B***", "b***", "blorpish", "unblorp"]


def test_a_never_word_is_never_masked_even_as_an_inflection(lists: WordLists) -> None:
    # "glimmer" would be glim + "mer"? no: glim + m + er is a doubled-consonant form of glim
    assert "glimmer" in inflections("glim")
    out, hits = mask_words(words("glimmer", "blorpsy", "glim"), lists)
    assert [w.text for w in out] == ["glimmer", "blorpsy", "g***"]
    assert len(hits) == 1


def test_a_phrase_masks_every_word_of_it_and_nothing_else(lists: WordLists) -> None:
    out, hits = mask_words(words("a", "quux", "faddle", "quux", "b"), lists)
    assert [w.text for w in out] == ["a", "q***", "f***", "quux", "b"]
    assert [(h.word, h.kind) for h in hits] == [("quux faddle", "mask")]


def test_a_context_word_is_masked_only_near_another_masked_word(lists: WordLists) -> None:
    alone, hits = mask_words(words("a", "zib", "and", "soda"), lists)
    assert [w.text for w in alone] == ["a", "zib", "and", "soda"] and hits == []
    near, hits = mask_words(words("blorp", "of", "zib"), lists)
    assert [w.text for w in near] == ["b***", "of", "z***"]
    assert [(h.word, h.kind) for h in hits] == [("blorp", "mask"), ("zib", "context")]
    far, _ = mask_words(words("blorp", "a", "b", "c", "zib"), lists)
    assert [w.text for w in far] == ["b***", "a", "b", "c", "zib"]
    two_context, hits = mask_words(words("zib", "zib"), lists)
    assert [w.text for w in two_context] == ["zib", "zib"] and hits == []


def test_a_slur_is_masked_and_is_a_slur_hit(lists: WordLists) -> None:
    out, hits = mask_words(words("Vexor", "vexors"), lists)
    assert [w.text for w in out] == ["V***", "v***"]
    assert [h.kind for h in hits] == ["slur", "slur"]


def test_mask_text_works_on_a_line_of_text(lists: WordLists) -> None:
    masked, hits = mask_text("HE SAID BLORP, then glim", lists)
    assert masked == "HE SAID B***, then g***"
    assert [h.word for h in hits] == ["BLORP", "glim"]
    assert mask_text("", lists) == ("", [])


def test_allowed_and_extra_masked(lists: WordLists) -> None:
    changed = lists.without(["Blorp", "zib", "WUMP"]).with_masked(["Nork", "two part"])
    out, _ = mask_words(words("blorp", "zib", "nork", "two", "part"), changed)
    assert [w.text for w in out] == ["blorp", "zib", "n***", "t***", "p***"]
    assert sensitive_hits("wump tral", changed) == [sensitive_hits("tral", lists)[0]]
    assert lists.without(["vexor"]).slurs == lists.slurs  # a slur cannot be allowed


# --- flagging ------------------------------------------------------------------------------


def test_sensitive_hits_are_flag_words_slurs_and_unmasked_context_words(lists: WordLists) -> None:
    hits = sensitive_hits("Vexor and Wump! trals, spoon dome; and then zib", lists)
    assert [(h.word, h.kind) for h in hits] == [
        ("Vexor", "slur"),
        ("Wump", "flag"),
        ("trals", "flag"),
        ("spoon dome", "flag"),
        ("zib", "context"),
    ]
    assert sensitive_hits("blorp zib", lists) == []  # masked next to blorp, so not flagged
    assert sensitive_hits("", lists) == []


# --- the committed lists -------------------------------------------------------------------


def test_the_committed_lists_load_and_do_not_contradict_each_other() -> None:
    lists = load_wordlists(REPO_ROOT)
    assert lists.mask and lists.flag and lists.never and lists.slurs and lists.context
    masked_words = {w for entry in lists.mask | lists.context | lists.slurs for w in entry}
    assert not masked_words & lists.never
    assert not {entry[0] for entry in lists.flag if len(entry) == 1} & lists.never
    assert mask_text("a dead body", lists) == ("a dead body", [])
    assert [h.word for h in sensitive_hits("a dead body", lists)] == ["dead body"]
    assert not lists.mask & lists.flag and not lists.context & lists.mask
    # the words sports talk needs are flagged only and the ordinary words are left alone
    for text in ("he killed that shot", "the stakes are high", "kick ass, that is hell"):
        assert mask_text(text, lists) == (text, [])
    masked, _ = mask_text("line of coke", lists)
    assert masked == "line of coke"  # alone: flagged, not masked
    assert [h.kind for h in sensitive_hits("line of coke", lists)] == ["context"]
    masked, _ = mask_text("cocaine and coke", lists)
    assert masked == "c*** and c***"


def test_self_harm_is_flagged_not_masked() -> None:
    # Masked, "self -harm" showed as "s*** -h***", which reads like a swear word in a caption.
    lists = load_wordlists(REPO_ROOT)
    words = [
        CaptionWord("of", 0.0, 0.2),
        CaptionWord("self", 0.2, 0.5),
        CaptionWord("-harm", 0.5, 0.9),
    ]
    masked, hits = mask_words(words, lists)
    assert [w.text for w in masked] == ["of", "self", "-harm"]
    assert hits == []
    assert mask_text("an act of self harm", lists) == ("an act of self harm", [])
    assert [(h.word, h.kind) for h in sensitive_hits("an act of self harm", lists)] == [
        ("self harm", "flag")
    ]
    assert [h.word for h in sensitive_hits("no self-harming", lists)] == ["self-harming"]
    # flag-only, so it no longer makes a context word next to it masked
    assert mask_text("self harm and cutting", lists)[0] == "self harm and cutting"
