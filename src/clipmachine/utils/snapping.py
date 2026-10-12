"""Move a model's rough start and end onto sentence boundaries, using word timestamps.

Pure functions, no I/O. The transcript text is untrusted data: nothing here puts it into a note
or a log line; notes carry seconds and counts only.

A sentence ends after a word that ends in . ? ! (or an ellipsis), optionally followed by closing
quotes or brackets, and at the last word of the transcript. A stretch between two such ends that
is longer than fallback_max_words words is split further, with no regard for Whisper segments:
first at every gap between words of at least min_pause seconds (end kind "pause"), then any piece
still too long at its largest gap, ties to the middle of the piece (end kind "edge"). The first
word of the transcript always starts a sentence.

Every sentence carries how it ends and the gap after it. A punctuated end with a gap under
min_cut_gap seconds is an "edge" too: a cut there would land inside continuous speech. The last
sentence of the transcript is never an edge. Boundaries that are edges are flagged.

Cut times: a clip is cut in the middle of the pause before its first word and after its last word,
at most start_pad seconds before the first word and end_pad seconds after the last word. Where the
words touch or overlap there is no pause and no padding.

Units: SentenceIndex.units() merges the sentences between cuttable boundaries into stretches that
can be cut before and after. snap() over an index of units can only start and end on a clean cut.
"""

import math
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

from clipmachine.models import TranscriptSegment

SENTENCE_ENDERS = (".", "?", "!", "…")
CLOSERS = "\"')]}”’»"
EPSILON = 1e-9
ROUND = 3
TIE_DIGITS = 6  # overlaps closer than this are a tie (float noise is not a preference)
DEFAULT_FALLBACK_WORDS = 40
END_PAUSE = "pause"  # a sentence that ends at a pause with no punctuation
END_EDGE = "edge"  # a weak end: a forced split, or punctuation inside continuous speech
DEFAULT_MIN_PAUSE = 0.35
DEFAULT_MIN_CUT_GAP = 0.15
# The notes snap() writes for a boundary that is a fallback segment edge. Named so the score stage
# can read the flags back from candidates.json without matching on a copy of the text.
EDGE_START_NOTE = "start is a segment edge: no sentence punctuation before it"
EDGE_END_NOTE = "end is a segment edge: no sentence punctuation after it"


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str  # stripped of the leading space Whisper puts on words
    segment: int  # index of the segment in the transcript


@dataclass(frozen=True)
class Sentence:
    first: int  # index of the first word
    last: int  # index of the last word (inclusive)
    end: str = "."  # how it ends: ".", "?", "!", END_PAUSE or END_EDGE
    gap: float = 0.0  # seconds from its last word to the next word's start; inf after the last


@dataclass(frozen=True)
class SentenceIndex:
    words: tuple[Word, ...]
    sentences: tuple[Sentence, ...]
    firsts: tuple[int, ...]  # first word index of every sentence, for bisecting

    def sentence_of(self, word_index: int) -> int:
        return bisect_right(self.firsts, word_index) - 1

    def cut_after(self, n: int) -> bool:
        """Whether a clip may end after sentence n (its end is not weak)."""
        return self.sentences[n].end != END_EDGE

    def cut_before(self, n: int) -> bool:
        """Whether a clip may start at sentence n (the transcript start, or a real end before)."""
        return n == 0 or self.cut_after(n - 1)

    def units(self) -> "SentenceIndex":
        """An index over the same words whose sentences are the stretches between cuttable
        boundaries: a unit runs from a sentence a clip may start at to the next sentence a clip
        may end after. Its end kind and gap are those of its last sentence."""
        units: list[Sentence] = []
        first: int | None = None  # index of the sentence that opened the current unit
        for n, sentence in enumerate(self.sentences):
            if first is None:
                if not self.cut_before(n):
                    continue
                first = n
            if self.cut_after(n):
                units.append(
                    Sentence(self.sentences[first].first, sentence.last, sentence.end, sentence.gap)
                )
                first = None
        return SentenceIndex(
            words=self.words,
            sentences=tuple(units),
            firsts=tuple(u.first for u in units),
        )


@dataclass(frozen=True)
class Snapped:
    start: float
    end: float
    first_word: int
    last_word: int
    edge_start: bool  # starts at a fallback segment edge, not after sentence punctuation
    edge_end: bool
    notes: tuple[str, ...]
    grew_backward: int = 0  # sentences added at the start to reach the minimum length


@dataclass(frozen=True)
class Dropped:
    reason: str


def ends_sentence(text: str) -> bool:
    return text.rstrip(CLOSERS).endswith(SENTENCE_ENDERS)


def _gap_after(words: Sequence[Word], i: int) -> float:
    if i + 1 >= len(words):
        return math.inf
    return max(0.0, words[i + 1].start - words[i].end)


def _punctuation_mark(text: str) -> str:
    stripped = text.rstrip(CLOSERS)
    return stripped[-1] if stripped.endswith(("?", "!")) else "."


def _split_point(words: Sequence[Word], first: int, last: int) -> int:
    """The word after which to split first..last: the largest gap (to 3 decimals), ties to the
    split nearest the middle, then to the earlier word."""
    half = (last - first + 1) / 2
    return min(
        range(first, last),
        key=lambda i: (-round(_gap_after(words, i), ROUND), abs(i - first + 1 - half), i),
    )


def build_index(
    segments: Sequence[TranscriptSegment],
    fallback_max_words: int = DEFAULT_FALLBACK_WORDS,
    *,
    min_pause: float = DEFAULT_MIN_PAUSE,
    min_cut_gap: float = DEFAULT_MIN_CUT_GAP,
) -> SentenceIndex:
    """Words and sentences of a whole transcript.

    A segment without word timestamps contributes one pseudo-word spanning the segment, so its
    text is not lost. Whisper segment edges are not boundaries.
    """
    words: list[Word] = []
    for number, segment in enumerate(segments):
        added = False
        for item in segment.words:
            text = item.word.strip()
            if text:
                words.append(Word(item.start, item.end, text, number))
                added = True
        if not added and segment.text.strip():
            words.append(Word(segment.start, segment.end, segment.text.strip(), number))

    sentences: list[Sentence] = []
    run_first = 0
    for i, word in enumerate(words):
        is_last = i == len(words) - 1
        punctuated = ends_sentence(word.text)
        if not (is_last or punctuated):
            continue
        # the run run_first..i ends here; its own end kind comes from the punctuation and the gap
        gap = _gap_after(words, i)
        if punctuated:
            run_end = _punctuation_mark(word.text) if gap >= min_cut_gap else END_EDGE
        else:
            run_end = END_PAUSE
        if is_last and run_end == END_EDGE:
            run_end = _punctuation_mark(word.text)
        if i + 1 - run_first <= fallback_max_words:
            sentences.append(Sentence(run_first, i, run_end, gap))
            run_first = i + 1
            continue

        # a long run: split at pauses, then pieces still too long at their largest gap
        pieces: list[tuple[int, int, str]] = []
        start = run_first
        for k in range(run_first, i):
            if _gap_after(words, k) >= min_pause:
                pieces.append((start, k, END_PAUSE))
                start = k + 1
        pieces.append((start, i, run_end))
        work = list(reversed(pieces))
        while work:
            first, last, kind = work.pop()
            if last + 1 - first <= fallback_max_words:
                sentences.append(Sentence(first, last, kind, _gap_after(words, last)))
                continue
            cut = _split_point(words, first, last)
            work.append((cut + 1, last, kind))
            work.append((first, cut, END_EDGE))
        run_first = i + 1
    return SentenceIndex(
        words=tuple(words),
        sentences=tuple(sentences),
        firsts=tuple(s.first for s in sentences),
    )


def edge_flags(notes: Sequence[str]) -> tuple[bool, bool]:
    """(start_at_segment_edge, end_at_segment_edge) from a candidate's snap notes."""
    return EDGE_START_NOTE in notes, EDGE_END_NOTE in notes


def words_text(index: SentenceIndex, first: int, last: int) -> str:
    return " ".join(w.text for w in index.words[first : last + 1])


def _overlap(low_a: float, high_a: float, low_b: float, high_b: float) -> float:
    return max(0.0, min(high_a, high_b) - max(low_a, low_b))


def _sentence_overlap(index: SentenceIndex, number: int, low: float, high: float) -> float:
    sentence = index.sentences[number]
    return _overlap(index.words[sentence.first].start, index.words[sentence.last].end, low, high)


def _padded_bounds(
    index: SentenceIndex, a: int, b: int, start_pad: float, end_pad: float, duration: float
) -> tuple[float, float]:
    """Start and end of sentences a..b: the middle of the pause before the first word and after
    the last word, at most start_pad before and end_pad after. No pause (touching or overlapping
    words) means no padding; never outside 0..duration."""
    first_index = index.sentences[a].first
    last_index = index.sentences[b].last
    first = index.words[first_index]
    last = index.words[last_index]
    previous_end = index.words[first_index - 1].end if first_index > 0 else 0.0
    next_start = (
        index.words[last_index + 1].start if last_index + 1 < len(index.words) else duration
    )
    gap_before = max(0.0, first.start - previous_end)
    gap_after = max(0.0, next_start - last.end)
    start = first.start - min(gap_before / 2, start_pad)
    end = last.end + min(gap_after / 2, end_pad)
    # round first, then clamp, so rounding can never push a time outside the audio
    return max(round(start, ROUND), 0.0), min(round(end, ROUND), duration)


def span_times(
    index: SentenceIndex,
    a: int,
    b: int,
    *,
    start_pad: float,
    end_pad: float,
    duration: float,
) -> tuple[float, float]:
    """Start and end of sentences a..b (indices into index.sentences) with mid-gap cut times: the
    same rule snap() applies, so a sub-range of a snapped span is cut the same way."""
    return _padded_bounds(index, a, b, start_pad, end_pad, duration)


def locate(
    index: SentenceIndex,
    start: float,
    end: float,
    *,
    start_pad: float,
    end_pad: float,
    duration: float,
) -> tuple[int, int] | None:
    """The sentences a..b whose cut times are exactly (start, end), or None.

    Inverse of span_times for spans that snap() produced: used to recover which sentences of the
    transcript a stored candidate covers. Times compare equal within a millisecond.
    """
    tolerance = 0.5 * 10**-ROUND
    count = len(index.sentences)

    def times(a: int, b: int) -> tuple[float, float]:
        return span_times(index, a, b, start_pad=start_pad, end_pad=end_pad, duration=duration)

    starts = [a for a in range(count) if abs(times(a, a)[0] - start) <= tolerance]
    for a in starts:
        for b in range(a, count):
            high = times(a, b)[1]
            if abs(high - end) <= tolerance:
                return a, b
            if high > end:
                break
    return None


def _can_add(
    index: SentenceIndex,
    a: int,
    b: int,
    number: int,
    removed: set[int],
    start_pad: float,
    end_pad: float,
    duration: float,
    max_length: float,
) -> bool:
    """Whether sentence `number` (b + 1 or a - 1) exists, was not removed, and fits the maximum."""
    if not 0 <= number < len(index.sentences) or number in removed:
        return False
    low, high = (a, number) if number > b else (number, b)
    start, end = _padded_bounds(index, low, high, start_pad, end_pad, duration)
    return end - start <= max_length + EPSILON


def snap(
    index: SentenceIndex,
    model_start: float,
    model_end: float,
    *,
    start_pad: float,
    end_pad: float,
    duration: float,
    min_length: float,
    max_length: float,
    preferred_min: float = 0.0,
) -> Snapped | Dropped:
    """Snap one moment. Returns the snapped span, or why it had to be dropped.

    It snaps onto the sentences of whatever index it is given. Pass index.units() to restrict it
    to clean cuts: every unit starts and ends at a cuttable boundary, so the result never does
    anything else.

    Start: the first word of the sentence containing model_start. End: the last word of the
    sentence containing model_end. A time between two words belongs to the sentence of the
    word after it (start) or before it (end). Then the cut times are set (mid-gap, capped). If
    that length is over max_length, whole sentences are removed, preferring to keep the sentence
    that overlaps the model's own range more (ties keep the first sentence). If it is under
    min_length, whole sentences are added at the end (the payoff side), and at the start only
    when the end is blocked. If it is under preferred_min, sentences are added at the end only,
    as long as it stays within max_length.
    """
    words = index.words
    if not words:
        return Dropped("the transcript has no words")
    i = next((k for k, w in enumerate(words) if w.end > model_start), len(words) - 1)
    j = next((k for k in range(len(words) - 1, -1, -1) if words[k].start < model_end), 0)
    a = index.sentence_of(i)
    b = max(a, index.sentence_of(j))
    first_chosen, last_chosen = a, b

    removed: set[int] = set()
    grown_end = grown_backward = grown_preferred = shrunk_front = shrunk_back = 0

    while True:
        start, end = _padded_bounds(index, a, b, start_pad, end_pad, duration)
        length = end - start
        if min_length - EPSILON <= length <= max_length + EPSILON:
            break

        if length > max_length:
            if a == b:
                return Dropped(
                    f"one stretch with no clean cut point inside it is {length:.1f} s, "
                    f"longer than the {max_length:g} s maximum"
                )
            front = _sentence_overlap(index, a, model_start, model_end)
            back = _sentence_overlap(index, b, model_start, model_end)
            if round(front, TIE_DIGITS) < round(back, TIE_DIGITS):
                removed.add(a)
                a += 1
                shrunk_front += 1
            else:  # also the tie: keep the first sentence (the hook)
                removed.add(b)
                b -= 1
                shrunk_back += 1
            continue

        # toward the end (the payoff side) first; backward only when the end is blocked
        if _can_add(index, a, b, b + 1, removed, start_pad, end_pad, duration, max_length):
            b += 1
            grown_end += 1
        elif _can_add(index, a, b, a - 1, removed, start_pad, end_pad, duration, max_length):
            a -= 1
            grown_backward += 1
        else:
            return Dropped(
                f"{length:.1f} s is shorter than the {min_length:g} s minimum and no whole "
                "sentence can be added without passing the maximum"
            )

    # Preferred length: toward the end only, never backward and never past the maximum.
    while True:
        start, end = _padded_bounds(index, a, b, start_pad, end_pad, duration)
        if end - start >= preferred_min - EPSILON:
            break
        if not _can_add(index, a, b, b + 1, removed, start_pad, end_pad, duration, max_length):
            break
        b += 1
        grown_preferred += 1

    first = words[index.sentences[a].first]
    last = words[index.sentences[b].last]
    first_index = index.sentences[a].first
    last_index = index.sentences[b].last
    edge_start = not index.cut_before(a)
    edge_end = not index.cut_after(b)

    notes: list[str] = []
    if a == first_chosen:
        moved = first.start - model_start
        if abs(moved) >= 0.005:
            notes.append(f"start moved {moved:+.2f} s to the start of its sentence")
    if b == last_chosen:
        moved = last.end - model_end
        if abs(moved) >= 0.005:
            notes.append(f"end moved {moved:+.2f} s to the end of its sentence")
    for amount, what in (
        (grown_end, "grew by {n} sentence(s) at the end to reach the minimum length"),
        (grown_backward, "grew backward by {n} sentence(s) at the start to reach the minimum"),
        (grown_preferred, "grew by {n} sentence(s) at the end toward the preferred length"),
        (shrunk_front, "dropped {n} sentence(s) at the start to fit the maximum length"),
        (shrunk_back, "dropped {n} sentence(s) at the end to fit the maximum length"),
    ):
        if amount:
            notes.append(what.format(n=amount))
    if edge_start:
        notes.append(EDGE_START_NOTE)
    if edge_end:
        notes.append(EDGE_END_NOTE)

    return Snapped(
        start=start,
        end=end,
        first_word=first_index,
        last_word=last_index,
        edge_start=edge_start,
        edge_end=edge_end,
        notes=tuple(notes),
        grew_backward=grown_backward,
    )


def dedupe(spans: Sequence[tuple[float, float]], max_overlap: float) -> list[int]:
    """Indices to keep. A span is dropped when it overlaps an already kept (earlier-listed) span
    by more than max_overlap of the shorter of the two."""
    kept: list[int] = []
    for i, (start, end) in enumerate(spans):
        duplicate = False
        for k in kept:
            other_start, other_end = spans[k]
            shorter = min(end - start, other_end - other_start)
            overlap = _overlap(start, end, other_start, other_end)
            if shorter > 0 and overlap / shorter > max_overlap + EPSILON:
                duplicate = True
                break
        if not duplicate:
            kept.append(i)
    return kept
