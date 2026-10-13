"""Name variants (Brief 11.1): how Whisper misspells a campaign's names, found in the transcript.

Deterministic, stdlib only, no model. The whole transcript is scanned once per run for spans of
one to three adjacent words (never across a sentence end) that sound or look like one of the
campaign's `names`. A span is normalised (lower case, digit look-alikes 3 1 0 -> e i o, no
punctuation) and compared with the normalised name by `difflib.SequenceMatcher` ratio and a
basic Soundex key:

- ratio >= apply_threshold (and, for a name shorter than 5 letters, the same Soundex key) makes a
  spelling rule that is applied to the captions;
- suggest_threshold <= ratio < apply_threshold with the same Soundex key is a suggestion: listed
  in the report and in auto_spelling.json, never applied (Igor's decision, 2026-10-12);
- a span of fewer than 4 normalised letters is a suggestion at most, whatever its ratio and
  sound: too often it is an ordinary word (Igor, after the real run);
- an explicit rule in `spelling` with the same `from` always wins, so `{from: x, to: x}` vetoes.

Pure functions; the transcript files are never written. The variants hold transcript words, so
they are never printed to the console or a log.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Literal

from clipmachine.campaign.models import AutoSeen, AutoSpelling, AutoVariant, SpellingRule
from clipmachine.utils.snapping import SentenceIndex

AUTO_SPELLING_FILE = "auto_spelling.json"
MAX_SEEN = 3  # timestamps kept per variant
MAX_SPAN = 3  # adjacent words joined and compared with a name
SHORT_NAME = 5  # a normalised name shorter than this must also match in Soundex to be applied
MIN_APPLY_LETTERS = 4  # a normalised span shorter than this is a suggestion at most (Igor)
_LOOKALIKES = str.maketrans({"3": "e", "1": "i", "0": "o"})
_SOUNDEX = {
    **dict.fromkeys("bfpv", "1"),
    **dict.fromkeys("cgjkqsxz", "2"),
    **dict.fromkeys("dt", "3"),
    "l": "4",
    **dict.fromkeys("mn", "5"),
    "r": "6",
}

Status = Literal["apply", "suggest"]


@dataclass(frozen=True)
class Variant:
    """One way a name is written in the transcript: the span as spoken (lower case, words joined
    with one space, punctuation stripped), the name it stands for, how often, and how sure."""

    text: str
    name: str
    count: int
    ratio: float
    soundex_match: bool
    status: Status


def normalise(text: str) -> str:
    """Lower case, digit look-alikes mapped (3->e, 1->i, 0->o), everything but letters and digits
    removed."""
    return "".join(c for c in text.lower().translate(_LOOKALIKES) if c.isalnum())


def soundex(text: str) -> str:
    """A basic 4-character Soundex of the letters in `text` ("" when there is no letter)."""
    letters = [c for c in text.lower() if c.isascii() and c.isalpha()]
    if not letters:
        return ""
    code = letters[0].upper()
    last = _SOUNDEX.get(letters[0], "")
    for c in letters[1:]:
        digit = _SOUNDEX.get(c, "")
        if digit and digit != last:
            code += digit
        if c not in "hw":  # h and w do not separate two letters with the same code
            last = digit
        if len(code) == 4:
            break
    return (code + "000")[:4]


def _core(text: str) -> str:
    """A transcript word without its leading and trailing punctuation, lower case."""
    first = next((i for i, c in enumerate(text) if c.isalnum()), None)
    if first is None:
        return ""
    last = max(i for i, c in enumerate(text) if c.isalnum())
    return text[first : last + 1].lower()


def classify(
    span: str, name: str, *, apply_threshold: float, suggest_threshold: float
) -> tuple[float, bool, Status | None]:
    """(ratio, same Soundex, status) of a span (words joined with spaces) against a name; the
    status is None when the span is neither applied nor suggested."""
    a, b = normalise(span), normalise(name)
    if not a or not b:
        return 0.0, False, None
    ratio = SequenceMatcher(None, a, b).ratio()
    same_sound = soundex(a) == soundex(b)
    if ratio >= apply_threshold and (len(b) >= SHORT_NAME or same_sound):
        # a span of fewer than MIN_APPLY_LETTERS letters is too often an ordinary word ("non" for
        # a name sounding like "neon"): listed, never applied, whatever its ratio and sound
        return ratio, same_sound, "apply" if len(a) >= MIN_APPLY_LETTERS else "suggest"
    if ratio >= suggest_threshold and same_sound:
        return ratio, same_sound, "suggest"
    return ratio, same_sound, None


def find_variants(
    index: SentenceIndex,
    names: Sequence[str],
    *,
    apply_threshold: float,
    suggest_threshold: float,
) -> list[Variant]:
    """Every span of 1..MAX_SPAN adjacent words inside one sentence that is a variant of a name.
    A span written exactly like the name (apart from case) is not a variant, and a longer span is
    not one when a shorter span inside it (sharing its first or last word) is the name or an
    applied variant of it ("neon sign" when "neon" already counts; "i'm curious like" when
    "curious like" does). Each distinct span text is one Variant with its count; a span is
    counted for the name it fits best. Applied variants come first, then suggestions, each by
    count (most first) then text."""
    if not names:
        return []
    words = index.words
    cores = [_core(w.text) for w in words]
    lower_names = {name.lower() for name in names}

    def applies(span: str) -> bool:
        """Whether this span is a name or an applied variant of one by itself."""
        if span in lower_names:
            return True
        return any(
            classify(span, n, apply_threshold=apply_threshold, suggest_threshold=suggest_threshold)[
                2
            ]
            == "apply"
            for n in names
        )

    def holds_a_variant(i: int, j: int) -> bool:
        """Whether a shorter span inside words i..j (sharing its first or last word) already
        counts: then the longer span is that variant plus a neighbour, not a variant of its own."""
        for n in range(1, j - i + 1):
            if applies(" ".join(cores[i : i + n])) or applies(" ".join(cores[j - n + 1 : j + 1])):
                return True
        return False

    found: dict[str, tuple[str, int, float, bool, Status]] = {}
    for i in range(len(words)):
        sentence = index.sentence_of(i)
        for n in range(1, MAX_SPAN + 1):
            j = i + n - 1
            if j >= len(words) or index.sentence_of(j) != sentence or not cores[j]:
                break
            if n > 1 and holds_a_variant(i, j):
                break
            span = " ".join(cores[i : j + 1])
            if span in lower_names:
                continue
            best: tuple[float, bool, Status, str] | None = None
            for name in names:
                ratio, same_sound, status = classify(
                    span, name, apply_threshold=apply_threshold, suggest_threshold=suggest_threshold
                )
                if status is not None and (best is None or ratio > best[0]):
                    best = (ratio, same_sound, status, name)
            if best is None:
                continue
            ratio, same_sound, status, name = best
            if span in found:
                prev = found[span]
                found[span] = (prev[0], prev[1] + 1, prev[2], prev[3], prev[4])
            else:
                found[span] = (name, 1, ratio, same_sound, status)
    variants = [
        Variant(text, name, count, round(ratio, 3), same_sound, status)
        for text, (name, count, ratio, same_sound, status) in found.items()
    ]
    order = {"apply": 0, "suggest": 1}
    return sorted(variants, key=lambda v: (order[v.status], -v.count, v.text))


def auto_rules(
    variants: Sequence[Variant], explicit: Sequence[SpellingRule]
) -> tuple[list[SpellingRule], list[Variant]]:
    """The spelling rules the applied variants make, and the variants an explicit rule vetoes
    (same `from`; an identity rule is the usual veto). Suggestions make no rule."""
    taken = {rule.source for rule in explicit}
    rules: list[SpellingRule] = []
    vetoed: list[Variant] = []
    for variant in variants:
        if variant.status != "apply":
            continue
        if variant.text in taken:
            vetoed.append(variant)
            continue
        rules.append(SpellingRule.model_validate({"from": variant.text, "to": variant.name}))
        taken.add(variant.text)
    return rules, vetoed


def record(
    variants: Sequence[Variant],
    covered: Sequence[Variant],
    heard: Mapping[str, Sequence[tuple[str, float]]],
    *,
    names: Sequence[str],
    apply_threshold: float,
    suggest_threshold: float,
    source_sha256: str,
    written_at: str | None = None,
) -> AutoSpelling:
    """The auto_spelling.json contents: every variant with its status (`covered` ones as
    "explicit"), its rule and up to MAX_SEEN places it was heard in the rendered moments."""
    explicit = {v.text for v in covered}
    rows = []
    for v in variants:
        status: Literal["apply", "suggest", "explicit"] = (
            "explicit" if v.text in explicit else v.status
        )
        seen = [AutoSeen(clip=clip, at=at) for clip, at in heard.get(v.text, ())][:MAX_SEEN]
        rows.append(
            AutoVariant(
                text=v.text,
                name=v.name,
                count=v.count,
                ratio=v.ratio,
                soundex_match=v.soundex_match,
                status=status,
                rule_to=v.name if status == "apply" else None,
                seen=seen,
            )
        )
    return AutoSpelling(
        names=list(names),
        apply_threshold=apply_threshold,
        suggest_threshold=suggest_threshold,
        source_sha256=source_sha256,
        written_at=written_at or datetime.now(UTC).isoformat(),
        variants=rows,
    )
