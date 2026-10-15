"""The paste-ready post text of one clip (Brief 11.2), one shape per platform.

TikTok and Instagram:
    <that platform's prefix> <caption>      (masked)
    <hashtags>                              (only when the campaign has some)
    <closer>

YouTube:
    TITLE: <the hook in sentence case without its masked words, at most TITLE_MAX characters>
    <blank line>
    <youtube prefix> <caption>              (masked)
    <hashtags>                              (only when the campaign has some)
    <closer>

The closer is the last line on every platform, so the `closer_present` check reads the same way
everywhere. Sentence case keeps the spelling of the campaign's names, spelling targets and
allowed words and of the app config's `campaign.title_keep_case` (acronyms such as NBA), so an
all-capitals hook does not lose them. Pure functions; nothing here prints.
"""

from collections.abc import Iterable, Sequence

from clipmachine.campaign.models import CampaignConfig, Moment
from clipmachine.utils.wordlists import Hit, WordLists, mask_text, mask_texts

TITLE_MAX = 100  # YouTube's title limit
TITLE_PREFIX = "TITLE: "
TITLE_NOTE = "title_without_masked_words"  # the report note when a title leaves words out
_ELLIPSIS = "..."
_SENTENCE_END = ".?!"
_CLOSING = "\"')]”’"
_DETERMINERS = frozenset(
    ("a", "an", "the", "my", "your", "his", "her", "its", "our", "their", "this", "that")
)
MIN_TITLE_WORDS = 3  # a title left without its masked words needs at least this many
# a title left without its masked words must not end on one of these ("Kevin is.", "Shaq on")
_DANGLING = _DETERMINERS | frozenset(
    (
        *("is", "are", "was", "were", "be", "been", "am", "and", "or", "but", "so", "just"),
        *("such", "very", "really", "than", "as", "on", "of", "to", "for", "with", "at", "in"),
        *("about", "like", "from", "by", "into"),
    )
)
_KEPT_MARKS = _SENTENCE_END + ",;:" + _CLOSING


def _core(word: str) -> tuple[str, str, str]:
    """(leading punctuation, the word itself, trailing punctuation)."""
    first = next((i for i, c in enumerate(word) if c.isalnum()), None)
    if first is None:
        return word, "", ""
    last = max(i for i, c in enumerate(word) if c.isalnum())
    return word[:first], word[first : last + 1], word[last + 1 :]


def keep_words(cfg: CampaignConfig, extra: Iterable[str]) -> list[str]:
    """The words and phrases whose spelling a title keeps, as given: the campaign's names,
    names_to_check, spelling targets and allowed words, then `extra` (the app config's list).
    The first spelling of a phrase wins."""
    seen: set[str] = set()
    kept: list[str] = []
    for phrase in (
        *cfg.names,
        *cfg.names_to_check,
        *(rule.to for rule in cfg.spelling),
        *cfg.allowed,
        *extra,
    ):
        key = " ".join(phrase.lower().split())
        if key and key not in seen:
            seen.add(key)
            kept.append(" ".join(phrase.split()))
    return kept


def sentence_case(text: str, keep: Sequence[str] = ()) -> str:
    """Lower case with the first letter of each sentence capitalised (after . ? or !, see
    _ends_sentence), and every phrase in `keep` (matched whole
    word, any case, punctuation around the words kept) written as `keep` has it."""
    words = text.split()
    if not words:
        return ""
    lowered = [w.lower() for w in words]
    parts = [_core(w) for w in lowered]
    cores = [core for _, core, _ in parts]
    for phrase in keep:
        wanted = phrase.split()
        n = len(wanted)
        target = [w.lower() for w in wanted]
        for i in range(len(cores) - n + 1):
            if cores[i : i + n] == target:
                for j, spelling in enumerate(wanted):
                    lead, _, trail = parts[i + j]
                    parts[i + j] = (lead, spelling, trail)
                    cores[i + j] = spelling.lower()
    out = ["".join(part) for part in parts]
    for i, word in enumerate(out):
        if (i == 0 or _ends_sentence(parts[i - 1])) and not _is_kept(i, keep, cores):
            out[i] = _capitalised(word)
    return " ".join(out)


def _ends_sentence(part: tuple[str, str, str]) -> bool:
    """Whether a word ends a sentence: its trailing punctuation (closing quotes and brackets
    aside) ends in . ? or !, but not an ellipsis, and the word is not an abbreviation (u.s.)."""
    _, core, trailing = part
    end = trailing.rstrip(_CLOSING)
    return bool(end) and end[-1] in _SENTENCE_END and not end.endswith("..") and "." not in core


def _capitalised(word: str) -> str:
    index = next((i for i, c in enumerate(word) if c.isalpha()), None)
    if index is None:
        return word
    return word[:index] + word[index].upper() + word[index + 1 :]


def _is_kept(position: int, keep: Sequence[str], cores: Sequence[str]) -> bool:
    """Whether the word at `position` was written by a kept phrase (then its case stays)."""
    for phrase in keep:
        wanted = [w.lower() for w in phrase.split()]
        n = len(wanted)
        for start in range(max(0, position - n + 1), position + 1):
            if cores[start : start + n] == wanted:
                return True
    return False


def shorten(text: str, limit: int = TITLE_MAX) -> str:
    """`text` when it fits, else cut at a word boundary with `...`, never longer than `limit`."""
    if len(text) <= limit:
        return text
    room = limit - len(_ELLIPSIS)
    head = text[:room]
    cut = head.rfind(" ")
    if cut > 0:
        head = head[:cut]
    return head.rstrip() + _ELLIPSIS


def youtube_title(hook: str, keep: Sequence[str]) -> str:
    return shorten(sentence_case(hook, keep))


def without_masked(text: str, lists: WordLists) -> str:
    """`text` without the words the lists would mask (Igor, 2026-10-15: a title reads better
    without the swear than with "a***"), or "" when no such text reads as a title. Unchanged
    when nothing is masked (spaces between words become one).

    First the sentences that hold one are left out ("Too much honesty? You're just an asshole"
    -> "Too much honesty?"). When that leaves fewer than MIN_TITLE_WORDS words, the masked words
    are left out instead, their punctuation kept on the word before, and a determiner just
    before one that ends its phrase (the hook, or . ? ! , ; :) goes too ("Wow. He is a fucking
    genius" -> "Wow. He is a genius"; "A friend called out my bullshit" -> "A friend called
    out"). That must leave MIN_TITLE_WORDS words and not end on a dangling word ("Kevin Hart is
    a bitch." would end on "is"; nor on a contraction or a possessive), else ""."""
    words = text.split()
    masked, _ = mask_texts(words, lists)
    if masked == words:
        return " ".join(words)
    sentences: list[list[int]] = [[]]
    for i, word in enumerate(words):
        sentences[-1].append(i)
        if _ends_sentence(_core(word)) and i < len(words) - 1:
            sentences.append([])
    clean = [i for s in sentences if all(words[i] == masked[i] for i in s) for i in s]
    if len(clean) >= MIN_TITLE_WORDS:
        return " ".join(words[i] for i in clean)
    kept: list[str] = []
    for i, (word, after) in enumerate(zip(words, masked, strict=True)):
        if word == after:
            kept.append(word)
            continue
        ends = i == len(words) - 1 or any(c in ".?!,;:" for c in _core(word)[2])
        while (
            ends and kept and not _core(kept[-1])[2] and _core(kept[-1])[1].lower() in _DETERMINERS
        ):
            kept.pop()
        marks = "".join(c for c in _core(word)[2] if c in _KEPT_MARKS)
        if marks and kept and not _core(kept[-1])[2]:
            kept[-1] += marks
    last = _core(kept[-1])[1].lower() if kept else ""
    # a contraction or a possessive at the end dangles too ("Too much? You're", "called out Mark's")
    if len(kept) < MIN_TITLE_WORDS or last in _DANGLING or "'" in last or "’" in last:
        return ""
    return " ".join(kept)


def title_left_out_words(hook: str, lists: WordLists | None) -> bool:
    """Whether the YouTube title of this hook leaves masked words out (a report note)."""
    if lists is None:
        return False
    clean = without_masked(hook, lists)
    return bool(clean) and clean != " ".join(hook.split())


def post_text(
    cfg: CampaignConfig,
    moment: Moment,
    closer: str | None,
    lists: WordLists | None,
    *,
    keep: Sequence[str] = (),
) -> tuple[str, list[Hit]]:
    """The text of `<id>.txt` for the moment's platform and the words masked in it. Only the
    title, the prefix and the caption are masked: the hashtags and the closer are fixed text.
    `closer` None (a campaign without closers, Brief 12) ends the text with the hashtags, or the
    caption."""
    post = cfg.post
    hits: list[Hit] = []
    lines: list[str] = []
    if moment.platform == "youtube":
        clean = without_masked(moment.hook, lists) if lists is not None else ""
        # no clean text that reads as a title: the hook's own words, masked
        title = youtube_title(clean or moment.hook, keep)
        if lists is not None:
            title, title_hits = mask_text(title, lists)
            hits.extend(title_hits)
        lines.extend((TITLE_PREFIX + title, ""))
    first = " ".join(
        part for part in (post.prefix.get(moment.platform, ""), moment.caption) if part
    )
    if lists is not None:
        first, first_hits = mask_text(first, lists)
        hits.extend(first_hits)
    lines.append(first)
    if post.hashtags:
        lines.append(" ".join(post.hashtags))
    if closer is not None:
        lines.append(closer)
    return "\n".join(lines) + "\n", hits
