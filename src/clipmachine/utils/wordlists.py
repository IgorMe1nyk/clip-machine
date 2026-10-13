"""Word lists for captions, hooks and post captions (Brief 11.1): masking and flagging.

Risk reduction, not a guarantee: the platforms publish no banned-word lists. The lists live in
`config/wordlists/` as plain text and are edited by hand. Everything here is deterministic and
local. Pure functions apart from `load_wordlists`; nothing is logged, and a hit's word is returned
to the caller, who decides where it may be shown (slurs: a count only, everywhere).

File format: one entry per line, `#` starts a comment, blank lines are ignored, an entry may have
spaces (a phrase matches consecutive words). In mask.txt two prefixes: `context: <word>` is masked
only next to another masked word (coke, crack, lean ...) and flagged otherwise; `slur: <word>` is
always masked and always flagged.

Matching is whole-word on the core of a word (leading and trailing punctuation kept), case
insensitive. A single-word entry also matches its common inflections (-s -es -ed -ing -er -ers
-in, -ies/-ied for a final y, and a doubled final consonant before -ed/-ing/-er). A word in
never.txt is never a hit, whatever the other lists say. A phrase matches exactly.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from clipmachine.utils.captions import CaptionWord

WORDLISTS_DIR = Path("config") / "wordlists"
FILES = ("mask.txt", "flag.txt", "never.txt")
MASK_SUFFIX = "***"
CONTEXT_WINDOW = 2  # words on either side that make a context word count as masked
_SUFFIXES = ("s", "es", "ed", "ing", "er", "ers", "in")
_DOUBLED_SUFFIXES = ("ed", "ing", "er")
_VOWELS = frozenset("aeiou")
_POSSESSIVES = ("'s", "’s")

Kind = Literal["mask", "slur", "context", "flag"]
Entry = tuple[str, ...]  # the lower-case words of an entry; a single word is a 1-tuple


class WordListError(Exception):
    """A word-list file that is missing or cannot be read."""


@dataclass(frozen=True)
class Hit:
    """One matched entry: the words as they were written (for a report) and the list kind."""

    word: str
    kind: Kind


@dataclass(frozen=True)
class WordLists:
    mask: frozenset[Entry]
    context: frozenset[Entry]
    slurs: frozenset[Entry]
    flag: frozenset[Entry]
    never: frozenset[str]

    def without(self, allowed: Iterable[str]) -> "WordLists":
        """The lists with `allowed` entries removed from mask, context and flag (never from
        slurs)."""
        gone = {_entry(a) for a in allowed}
        return WordLists(
            frozenset(e for e in self.mask if e not in gone),
            frozenset(e for e in self.context if e not in gone),
            self.slurs,
            frozenset(e for e in self.flag if e not in gone),
            self.never,
        )

    def with_masked(self, extra: Iterable[str]) -> "WordLists":
        """The lists with `extra` entries added to mask."""
        more = {_entry(e) for e in extra if _entry(e)}
        return WordLists(self.mask | more, self.context, self.slurs, self.flag, self.never)


def _entry(text: str) -> Entry:
    return tuple(text.lower().split())


def split_word(text: str) -> tuple[str, str, str]:
    """(leading punctuation, core, trailing punctuation). Punctuation is whatever is not a letter
    or a digit at either end; a trailing 's belongs to the trailing part. A word with no letter or
    digit has an empty core."""
    first = next((i for i, c in enumerate(text) if c.isalnum()), None)
    if first is None:
        return text, "", ""
    last = max(i for i, c in enumerate(text) if c.isalnum())
    leading, core, trailing = text[:first], text[first : last + 1], text[last + 1 :]
    if len(core) > 2 and core[-2:].lower() in _POSSESSIVES and core[-3].isalnum():
        trailing = core[-2:] + trailing
        core = core[:-2]
    return leading, core, trailing


def inflections(base: str) -> frozenset[str]:
    """The base word and its common inflected forms, lower case."""
    base = base.lower()
    forms = {base}
    forms.update(base + suffix for suffix in _SUFFIXES)
    if base and base[-1].isalpha() and base[-1] not in _VOWELS and base[-1] != "y":
        forms.update(base + base[-1] + suffix for suffix in _DOUBLED_SUFFIXES)
    if base.endswith("y") and len(base) > 2:
        forms.update((base[:-1] + "ies", base[:-1] + "ied"))
    return frozenset(forms)


def parse_list(text: str) -> list[tuple[str, Entry]]:
    """(prefix, entry) per line; the prefix is "" or the word before a colon (context, slur)."""
    out: list[tuple[str, Entry]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        prefix = ""
        if ":" in line:
            head, _, rest = line.partition(":")
            prefix, line = head.strip().lower(), rest.strip()
        entry = _entry(line)
        if entry:
            out.append((prefix, entry))
    return out


def load_wordlists(root: Path) -> WordLists:
    """The three files in `<root>/config/wordlists/`. WordListError when one is missing."""
    texts: dict[str, str] = {}
    for name in FILES:
        path = root / WORDLISTS_DIR / name
        try:
            texts[name] = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WordListError(
                f"word list not found: {WORDLISTS_DIR.as_posix()}/{name} ({exc.strerror or exc})"
            ) from exc
    mask: set[Entry] = set()
    context: set[Entry] = set()
    slurs: set[Entry] = set()
    for prefix, entry in parse_list(texts["mask.txt"]):
        if prefix == "context":
            context.add(entry)
        elif prefix == "slur":
            slurs.add(entry)
        elif prefix:
            raise WordListError(f"mask.txt: unknown prefix {prefix!r} (use context: or slur:)")
        else:
            mask.add(entry)
    flag: set[Entry] = set()
    for prefix, entry in parse_list(texts["flag.txt"]):
        if prefix:
            raise WordListError(f"flag.txt: a prefix ({prefix!r}) is not allowed here")
        flag.add(entry)
    never: set[str] = set()
    for prefix, entry in parse_list(texts["never.txt"]):
        if prefix or len(entry) != 1:
            raise WordListError("never.txt: one plain word per line")
        never.add(entry[0])
    return WordLists(
        frozenset(mask), frozenset(context), frozenset(slurs), frozenset(flag), frozenset(never)
    )


@dataclass(frozen=True)
class _Match:
    start: int  # index of the first word
    count: int
    kind: Kind


def _forms(entries: Iterable[Entry], never: frozenset[str]) -> dict[str, Entry]:
    """Every inflected form of the single-word entries, mapped to its entry; never-words are left
    out."""
    forms: dict[str, Entry] = {}
    for entry in entries:
        if len(entry) == 1:
            for form in inflections(entry[0]):
                if form not in never:
                    forms[form] = entry
    return forms


def _find(
    cores: Sequence[str], entries: frozenset[Entry], never: frozenset[str], kind: Kind
) -> list[_Match]:
    """Where the entries occur in the lower-case cores: phrases exactly (whatever `never` says:
    "dead body" is a phrase even though "dead" alone is normal), single words with their
    inflections, where a word in `never` is never a hit."""
    forms = _forms(entries, never)
    phrases = sorted((e for e in entries if len(e) > 1), key=len, reverse=True)
    found: list[_Match] = []
    for i, core in enumerate(cores):
        if not core:
            continue
        for phrase in phrases:
            n = len(phrase)
            if tuple(cores[i : i + n]) == phrase:
                found.append(_Match(i, n, kind))
                break
        if core in forms and core not in never:
            found.append(_Match(i, 1, kind))
    return found


def _masked(core: str) -> str:
    return core[0] + MASK_SUFFIX if core else core


def _plan(cores: Sequence[str], lists: WordLists) -> tuple[list[_Match], list[_Match]]:
    """(masked matches, flagged-only matches) over the cores. A slur is in both. A context word
    is masked when a mask or slur match lies within CONTEXT_WINDOW words, else flagged only."""
    masked = _find(cores, lists.mask, lists.never, "mask")
    slurs = _find(cores, lists.slurs, lists.never, "slur")
    anchors = {i for m in [*masked, *slurs] for i in range(m.start, m.start + m.count)}
    context_masked: list[_Match] = []
    flagged: list[_Match] = list(slurs)
    for m in _find(cores, lists.context, lists.never, "context"):
        near = range(max(0, m.start - CONTEXT_WINDOW), m.start + m.count + CONTEXT_WINDOW)
        if any(i in anchors and not (m.start <= i < m.start + m.count) for i in near):
            context_masked.append(m)
        else:
            flagged.append(m)
    flagged.extend(_find(cores, lists.flag, lists.never, "flag"))
    return sorted([*masked, *slurs, *context_masked], key=_place), sorted(flagged, key=_place)


def _place(match: _Match) -> int:
    return match.start


def _hit(parts: Sequence[tuple[str, str, str]], match: _Match) -> Hit:
    return Hit(
        " ".join(parts[i][1] for i in range(match.start, match.start + match.count)), match.kind
    )


def mask_texts(texts: Sequence[str], lists: WordLists) -> tuple[list[str], list[Hit]]:
    """The words with masked ones as first letter + ***, and the hits (one per match)."""
    parts = [split_word(t) for t in texts]
    cores = [core.lower() for _, core, _ in parts]
    to_mask, _ = _plan(cores, lists)
    out = list(texts)
    for m in to_mask:
        for i in range(m.start, m.start + m.count):
            leading, core, trailing = parts[i]
            out[i] = leading + _masked(core) + trailing
    return out, [_hit(parts, m) for m in to_mask]


def mask_words(
    words: Sequence[CaptionWord], lists: WordLists
) -> tuple[list[CaptionWord], list[Hit]]:
    """`mask_texts` on caption words; times are kept."""
    texts, hits = mask_texts([w.text for w in words], lists)
    return [CaptionWord(t, w.start, w.end) for t, w in zip(texts, words, strict=True)], hits


def mask_text(text: str, lists: WordLists) -> tuple[str, list[Hit]]:
    """`mask_texts` on a line of text split at spaces (the hook, a post caption line)."""
    if not text:
        return text, []
    texts, hits = mask_texts(text.split(" "), lists)
    return " ".join(texts), hits


def sensitive_hits(text: str, lists: WordLists) -> list[Hit]:
    """Flag words, slurs and context words that are not masked, in a line of text."""
    if not text:
        return []
    parts = [split_word(t) for t in text.split(" ")]
    cores = [core.lower() for _, core, _ in parts]
    _, flagged = _plan(cores, lists)
    return [_hit(parts, m) for m in flagged]
