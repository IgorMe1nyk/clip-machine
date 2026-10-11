"""Move a model's rough start and end onto sentence boundaries, using word timestamps.

Pure functions, no I/O. The transcript text is untrusted data: nothing here puts it into a note
or a log line; notes carry seconds and counts only.

A sentence ends after a word that ends in . ? ! (or an ellipsis), optionally followed by closing
quotes or brackets. Whisper segments often break mid-sentence, so a segment edge is a boundary
only as a fallback: when more than fallback_max_words words have passed since the last sentence
end. The first word of the transcript always starts a sentence. Boundaries that came from the
fallback instead of punctuation are flagged.
"""

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


@dataclass(frozen=True)
class SentenceIndex:
    words: tuple[Word, ...]
    sentences: tuple[Sentence, ...]
    firsts: tuple[int, ...]  # first word index of every sentence, for bisecting

    def sentence_of(self, word_index: int) -> int:
        return bisect_right(self.firsts, word_index) - 1


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


def build_index(
    segments: Sequence[TranscriptSegment], fallback_max_words: int = DEFAULT_FALLBACK_WORDS
) -> SentenceIndex:
    """Words and sentences of a whole transcript.

    A segment without word timestamps contributes one pseudo-word spanning the segment, so its
    text is not lost.
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
    first = 0
    for i, word in enumerate(words):
        last_word = i == len(words) - 1
        segment_edge = not last_word and words[i + 1].segment != word.segment
        too_long = segment_edge and i + 1 - first > fallback_max_words
        if last_word or too_long or ends_sentence(word.text):
            sentences.append(Sentence(first, i))
            first = i + 1
    return SentenceIndex(
        words=tuple(words),
        sentences=tuple(sentences),
        firsts=tuple(s.first for s in sentences),
    )


def words_text(index: SentenceIndex, first: int, last: int) -> str:
    return " ".join(w.text for w in index.words[first : last + 1])


def _overlap(low_a: float, high_a: float, low_b: float, high_b: float) -> float:
    return max(0.0, min(high_a, high_b) - max(low_a, low_b))


def _sentence_overlap(index: SentenceIndex, number: int, low: float, high: float) -> float:
    sentence = index.sentences[number]
    return _overlap(index.words[sentence.first].start, index.words[sentence.last].end, low, high)


def _padded_bounds(
    index: SentenceIndex, a: int, b: int, padding: float, duration: float
) -> tuple[float, float]:
    """Start and end of sentences a..b with padding, never into a neighbouring word, the
    sentence's own words, or outside 0..duration."""
    first_index = index.sentences[a].first
    last_index = index.sentences[b].last
    first = index.words[first_index]
    last = index.words[last_index]
    previous_end = index.words[first_index - 1].end if first_index > 0 else 0.0
    next_start = (
        index.words[last_index + 1].start if last_index + 1 < len(index.words) else duration
    )
    start = min(max(first.start - padding, previous_end, 0.0), first.start)
    end = max(min(last.end + padding, next_start), last.end)
    # round first, then clamp, so rounding can never push a time outside the audio
    return max(round(start, ROUND), 0.0), min(round(end, ROUND), duration)


def span_times(
    index: SentenceIndex, a: int, b: int, *, padding: float, duration: float
) -> tuple[float, float]:
    """Start and end of sentences a..b (indices into index.sentences) with padding: the same
    rule snap() applies, so a sub-range of a snapped span is cut the same way."""
    return _padded_bounds(index, a, b, padding, duration)


def locate(
    index: SentenceIndex, start: float, end: float, *, padding: float, duration: float
) -> tuple[int, int] | None:
    """The sentences a..b whose padded span is exactly (start, end), or None.

    Inverse of span_times for spans that snap() produced: used to recover which sentences of the
    transcript a stored candidate covers. Times compare equal within a millisecond.
    """
    tolerance = 0.5 * 10**-ROUND
    count = len(index.sentences)
    starts = [
        a
        for a in range(count)
        if abs(span_times(index, a, a, padding=padding, duration=duration)[0] - start) <= tolerance
    ]
    for a in starts:
        for b in range(a, count):
            high = span_times(index, a, b, padding=padding, duration=duration)[1]
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
    padding: float,
    duration: float,
    max_length: float,
) -> bool:
    """Whether sentence `number` (b + 1 or a - 1) exists, was not removed, and fits the maximum."""
    if not 0 <= number < len(index.sentences) or number in removed:
        return False
    low, high = (a, number) if number > b else (number, b)
    start, end = _padded_bounds(index, low, high, padding, duration)
    return end - start <= max_length + EPSILON


def snap(
    index: SentenceIndex,
    model_start: float,
    model_end: float,
    *,
    padding: float,
    duration: float,
    min_length: float,
    max_length: float,
    preferred_min: float = 0.0,
) -> Snapped | Dropped:
    """Snap one moment. Returns the snapped span, or why it had to be dropped.

    Start: the first word of the sentence containing model_start. End: the last word of the
    sentence containing model_end. A time between two words belongs to the sentence of the
    word after it (start) or before it (end). Then padding is added. If the padded length is
    over max_length, whole sentences are removed, preferring to keep the sentence that overlaps
    the model's own range more (ties keep the first sentence). If it is under min_length,
    whole sentences are added at the end (the payoff side), and at the start only when the end
    is blocked. If it is under preferred_min, sentences are added at the end only, as long as
    it stays within max_length.
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
        start, end = _padded_bounds(index, a, b, padding, duration)
        length = end - start
        if min_length - EPSILON <= length <= max_length + EPSILON:
            break

        if length > max_length:
            if a == b:
                return Dropped(
                    f"one sentence is {length:.1f} s, longer than the {max_length:g} s maximum"
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
        if _can_add(index, a, b, b + 1, removed, padding, duration, max_length):
            b += 1
            grown_end += 1
        elif _can_add(index, a, b, a - 1, removed, padding, duration, max_length):
            a -= 1
            grown_backward += 1
        else:
            return Dropped(
                f"{length:.1f} s is shorter than the {min_length:g} s minimum and no whole "
                "sentence can be added without passing the maximum"
            )

    # Preferred length: toward the end only, never backward and never past the maximum.
    while True:
        start, end = _padded_bounds(index, a, b, padding, duration)
        if end - start >= preferred_min - EPSILON:
            break
        if not _can_add(index, a, b, b + 1, removed, padding, duration, max_length):
            break
        b += 1
        grown_preferred += 1

    first = words[index.sentences[a].first]
    last = words[index.sentences[b].last]
    first_index = index.sentences[a].first
    last_index = index.sentences[b].last
    edge_start = first_index != 0 and not ends_sentence(words[first_index - 1].text)
    edge_end = not ends_sentence(last.text)

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
        notes.append("start is a segment edge: no sentence punctuation before it")
    if edge_end:
        notes.append("end is a segment edge: no sentence punctuation after it")
    start_pad = first.start - start
    end_pad = end - last.end
    if start_pad < padding - 0.005:
        notes.append(f"start padding limited to {max(start_pad, 0.0):.2f} s of {padding:g} s")
    if end_pad < padding - 0.005:
        notes.append(f"end padding limited to {max(end_pad, 0.0):.2f} s of {padding:g} s")

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
