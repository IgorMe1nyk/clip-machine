"""Caption word fixes for campaign mode (Brief 11).

Pure functions, no I/O. They work on the clip's caption words only (`CaptionWord`, clip-relative
seconds); transcript files are never opened, let alone written. Matching is case-insensitive and
whole-word, on the core of a word: its leading and trailing punctuation are kept as they are.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from clipmachine.campaign.models import SpellingRule
from clipmachine.utils.captions import CaptionWord

_POSSESSIVES = ("'s", "’s")
_INNER_TRAILING = frozenset(".,")  # what may follow a word inside a multi-word match


@dataclass(frozen=True)
class _Parts:
    leading: str
    core: str
    trailing: str  # includes a possessive 's


def _split(text: str) -> _Parts:
    """Leading punctuation, core, trailing punctuation. Punctuation is whatever is not a letter
    or a digit at either end; a trailing 's belongs to the trailing part. A word with no letter
    or digit has an empty core."""
    first = next((i for i, c in enumerate(text) if c.isalnum()), None)
    if first is None:
        return _Parts(text, "", "")
    last = max(i for i, c in enumerate(text) if c.isalnum())
    leading, core, trailing = text[:first], text[first : last + 1], text[last + 1 :]
    if len(core) > 2 and core[-2:].lower() in _POSSESSIVES and core[-3].isalnum():
        trailing = core[-2:] + trailing
        core = core[:-2]
    return _Parts(leading, core, trailing)


def _matches(parts: Sequence[_Parts], i: int, source: Sequence[str]) -> bool:
    """Whether the words from `i` have the cores in `source` (lower case). All words but the last
    may only be followed by dots and commas."""
    if i + len(source) > len(parts):
        return False
    for k, wanted in enumerate(source):
        part = parts[i + k]
        if not part.core or part.core.lower() != wanted:
            return False
        if k < len(source) - 1 and not set(part.trailing) <= _INNER_TRAILING:
            return False
    return True


def apply_spelling(
    words: Sequence[CaptionWord], rules: Sequence[SpellingRule]
) -> tuple[list[CaptionWord], int]:
    """The words with the rules applied, and how many replacements were made.

    Rules with more source words go first (the given order among equals); each scans the words
    left to right. A match uses words no earlier rule has used, so matches never overlap and a
    replacement is never matched again. A match becomes one word: the first word's leading
    punctuation, the rule's `to`, the last word's trailing punctuation; it starts when the first
    word starts and ends when the last word ends."""
    parts = [_split(w.text) for w in words]
    taken = [False] * len(words)
    replaced: dict[int, tuple[int, CaptionWord]] = {}  # first index -> (word count, new word)
    for rule in sorted(rules, key=lambda r: -len(r.source.split())):
        source = rule.source.split()
        i = 0
        while i < len(words):
            n = len(source)
            if _matches(parts, i, source) and not any(taken[i : i + n]):
                text = parts[i].leading + rule.to + parts[i + n - 1].trailing
                replaced[i] = (n, CaptionWord(text, words[i].start, words[i + n - 1].end))
                for k in range(i, i + n):
                    taken[k] = True
                i += n
            else:
                i += 1
    out: list[CaptionWord] = []
    i = 0
    while i < len(words):
        if i in replaced:
            n, word = replaced[i]
            out.append(word)
            i += n
        else:
            out.append(words[i])
            i += 1
    return out, len(replaced)


def find_names(words: Sequence[CaptionWord], names: Sequence[str]) -> dict[str, list[float]]:
    """For each name, the clip-relative start times (2 decimals) of its occurrences, matched like
    the rules (case-insensitive, whole word; a name with spaces is consecutive words). Every name
    is a key, with an empty list when it is not found. Run it on the words after
    `apply_spelling`."""
    parts = [_split(w.text) for w in words]
    found: dict[str, list[float]] = {}
    for name in names:
        source = name.lower().split()
        times: list[float] = []
        i = 0
        while source and i < len(words):
            if _matches(parts, i, source):
                times.append(round(words[i].start, 2))
                i += len(source)
            else:
                i += 1
        found[name] = times
    return found
