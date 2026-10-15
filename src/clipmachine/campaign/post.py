"""The paste-ready post text of one clip (Brief 11.2), one shape per platform.

TikTok and Instagram:
    <that platform's prefix> <caption>      (masked)
    <hashtags>                              (only when the campaign has some)
    <closer>

YouTube:
    TITLE: <the hook in sentence case, at most TITLE_MAX characters>   (masked)
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
from clipmachine.utils.wordlists import Hit, WordLists, mask_text

TITLE_MAX = 100  # YouTube's title limit
TITLE_PREFIX = "TITLE: "
_ELLIPSIS = "..."
_SENTENCE_END = ".?!"
_CLOSING = "\"')]”’"


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
        title = youtube_title(moment.hook, keep)
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
