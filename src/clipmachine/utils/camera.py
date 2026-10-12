"""Speaker-following camera: face tracks, the active speaker, shots and the crop path.

Pure Python: no numpy, no mediapipe, no I/O, and deterministic (ties go to the lowest track id).
Positions and sizes are fractions of the frame (0..1); times are seconds from the clip start.

The pipeline, each step a function below:
  frames of FaceSample -> link_tracks -> Track
  transcript words     -> speech_windows -> one bool per window
  tracks + speech      -> choose_speakers -> one track id (or None) per window
  choices              -> build_shots -> Shot (one person each, none shorter than the minimum)
  shots + tracks       -> camera_path -> crop x per sample

Cuts: a camera shot (a "segment" here) is the stretch between two cuts in the source edit.
  picture signatures   -> find_cuts -> sample indices where a new camera shot starts
  cuts                 -> split_at_cuts -> sample ranges, one per segment
  everything above, run on each segment alone and joined again -> follow_segments
Nothing is carried across a cut: tracks, speaker choice, hysteresis and the crop position all
start fresh in each segment, and a cut is never counted as a speaker switch.
"""

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from clipmachine.models import SpeakerConfig

MAX_LINK_GAP = 3  # samples a face may vanish and still be re-linked to its track
MAX_LINK_GAP_SECONDS = 0.3  # the same gap in time: follow_segments scales it to the sample rate
SMALL_FACE_MAJORITY = 0.8  # share of face samples that must be small for "faces_too_small"
MIN_VARIANCE = 1e-9  # mouth-movement variance below this counts as no movement

FALLBACK_NO_FACE = "no_face"
FALLBACK_FACES_TOO_SMALL = "faces_too_small"
FALLBACK_SOURCE_NOT_WIDER = "source_not_wider_than_crop"


@dataclass(frozen=True)
class FaceSample:
    """One face in one sampled frame. `mouth` is the mouth openness divided by the face height, or
    None when the face was found but its mouth could not be read. `motion` is the pixel movement
    round the mouth since the previous sample, less the movement of the upper face (None: no
    previous frame, a cut, or it could not be measured)."""

    t: float
    cx: float
    cy: float
    w: float
    h: float
    mouth: float | None
    motion: float | None = None


@dataclass
class Track:
    """One person followed across samples: at most one face per sample index."""

    id: int
    by_frame: dict[int, FaceSample] = field(default_factory=dict)

    @property
    def points(self) -> list[tuple[int, FaceSample]]:
        return sorted(self.by_frame.items())

    @property
    def last_index(self) -> int:
        return max(self.by_frame)


@dataclass(frozen=True)
class Shot:
    """One tracked person (or nobody: track None) over [start, end) seconds."""

    start: float
    end: float
    track: int | None


@dataclass(frozen=True)
class CameraSummary:
    faces_seen: int  # tracks found
    switches: int  # shot changes
    face_time_pct: float  # % of samples with at least one face
    faces_per_sample: float = 0.0  # mean number of faces per sample
    mouth_read_pct: float = 0.0  # % of face samples whose mouth could be read


# --- tracks -------------------------------------------------------------------------------


def link_tracks(
    frames: Sequence[Sequence[FaceSample]], max_distance: float, max_gap: int = MAX_LINK_GAP
) -> list[Track]:
    """Link faces across consecutive samples by nearest centre.

    A face joins the track whose last position is nearest, if within max_distance and the track
    was seen at most max_gap samples ago (a face that vanishes and returns re-links). Candidate
    pairs are taken nearest first, so each track gets at most one face per sample and two faces of
    one sample never share a track. Two faces that pass each other are told apart by position
    alone (no velocity), so a crossing is only resolved when they are further apart than they
    move between samples.
    """
    tracks: list[Track] = []
    for index, faces in enumerate(frames):
        pairs: list[tuple[float, int, int]] = []
        for track in tracks:
            if index - track.last_index > max_gap + 1:
                continue
            last = track.by_frame[track.last_index]
            for face_index, face in enumerate(faces):
                distance = math.hypot(face.cx - last.cx, face.cy - last.cy)
                if distance <= max_distance:
                    pairs.append((distance, track.id, face_index))
        pairs.sort()
        used_tracks: set[int] = set()
        used_faces: set[int] = set()
        for _, track_id, face_index in pairs:
            if track_id in used_tracks or face_index in used_faces:
                continue
            used_tracks.add(track_id)
            used_faces.add(face_index)
            tracks[track_id].by_frame[index] = faces[face_index]
        for face_index, face in enumerate(faces):
            if face_index not in used_faces:
                tracks.append(Track(id=len(tracks), by_frame={index: face}))
    return tracks


# --- speech and speaker choice ------------------------------------------------------------


def window_count(duration: float, window: float) -> int:
    return max(0, math.ceil(round(duration / window, 9)))


def window_index(t: float, window: float, count: int) -> int:
    """The window holding time t, clamped to 0..count-1."""
    return min(max(int(math.floor(round(t / window, 9))), 0), count - 1)


def speech_windows(
    words: Sequence[tuple[float, float]], duration: float, window: float, min_words: int = 1
) -> list[bool]:
    """True for each window [i*window, (i+1)*window) that overlaps at least min_words words.

    `words` are (start, end) seconds from the clip start.
    """
    count = window_count(duration, window)
    hits = [0] * count
    for start, end in words:
        if count == 0 or end < 0 or start >= count * window:
            continue
        first = window_index(start, window, count)
        if end > start:
            last = window_index(end, window, count)
            if round(end / window, 9) == last:  # ends exactly on a window edge: not in it
                last = max(first, last - 1)
        else:
            last = first
        for i in range(first, last + 1):
            hits[i] += 1
    return [h >= min_words for h in hits]


def _window_values(track: Track, window: float, count: int) -> dict[int, list[FaceSample]]:
    found: dict[int, list[FaceSample]] = {}
    for _, sample in track.points:
        found.setdefault(window_index(sample.t, window, count), []).append(sample)
    return found


def _variance(samples: Sequence[FaceSample]) -> float:
    """Variance of the readable mouth openings; 0.0 with fewer than two of them."""
    mouths = [s.mouth for s in samples if s.mouth is not None]
    if len(mouths) < 2:
        return 0.0
    return statistics.pvariance(mouths)


def _mean_height(track: Track) -> float:
    return statistics.fmean(s.h for s in track.by_frame.values())


def _mean_cx(track: Track) -> float:
    return statistics.fmean(s.cx for s in track.by_frame.values())


def choose_speakers(
    tracks: Sequence[Track], speech: Sequence[bool], window: float, cfg: SpeakerConfig
) -> list[int | None]:
    """The track to show in each window.

    The speaker is the track with the highest variance of mouth openness inside the window, and
    only windows with speech count; a silent window keeps the current choice.

    Hysteresis: the first speech window in which anyone moves their mouth picks its leader at
    once, and the windows before it get the same choice. After that a challenger must have
    variance above switch_ratio times the current speaker's for confirm_windows speech windows in
    a row; the switch is then backdated to the first of those windows. Silent windows neither
    count nor reset a pending switch; a window the challenger does not lead resets it.

    A chosen track that has no face is held, except that once it has been absent for
    confirm_windows windows in a row, the best track present takes over (no backdating).

    With no speech or no mouth movement anywhere, the track with the largest faces is shown all
    the time (equal sizes: the most central track, then the lowest id). With no tracks every
    window is None.
    """
    count = len(speech)
    if not tracks:
        return [None] * count
    per_window = {t.id: _window_values(t, window, count) for t in tracks}

    def variance(track_id: int, w: int) -> float:
        return _variance(per_window[track_id].get(w, []))

    def best_present(w: int) -> int | None:
        present = [t.id for t in tracks if w in per_window[t.id]]
        if not present:
            return None
        return max(present, key=lambda i: (variance(i, w), _mean_height(tracks[i]), -i))

    choice: list[int | None] = [None] * count
    current: int | None = None
    pending: tuple[int, int] | None = None  # (challenger, first window of the streak)
    streak = 0
    absent = 0
    for w in range(count):
        if current is not None:
            absent = 0 if w in per_window[current] else absent + 1
            if absent >= cfg.confirm_windows:
                replacement = best_present(w)
                if replacement is not None:
                    current, pending, streak, absent = replacement, None, 0, 0
        if not speech[w]:
            choice[w] = current
            continue
        leader = best_present(w)
        if leader is None or variance(leader, w) <= MIN_VARIANCE:
            choice[w] = current
            pending, streak = None, 0
            continue
        if current is None:
            current = leader
            for earlier in range(w):
                choice[earlier] = current
            choice[w] = current
            continue
        rivals = [t.id for t in tracks if t.id != current and w in per_window[t.id]]
        challenger = max(rivals, key=lambda i: (variance(i, w), -i)) if rivals else None
        beats = (
            challenger is not None
            and variance(challenger, w) > MIN_VARIANCE
            and variance(challenger, w) > variance(current, w) * cfg.switch_ratio
        )
        if challenger is None or not beats:
            pending, streak = None, 0
            choice[w] = current
            continue
        if pending is not None and pending[0] == challenger:
            streak += 1
        else:
            pending, streak = (challenger, w), 1
        if streak >= cfg.confirm_windows:
            current = challenger
            for backdated in range(pending[1], w + 1):
                choice[backdated] = current
            pending, streak = None, 0
        else:
            choice[w] = current

    if current is None:  # nobody ever moved their mouth in a speech window
        # the largest faces; among equal sizes the most central track, then the lowest id
        fallback = max(tracks, key=lambda t: (_mean_height(t), -abs(_mean_cx(t) - 0.5), -t.id)).id
        return [fallback] * count
    return choice


# --- shots --------------------------------------------------------------------------------


def build_shots(
    choice_per_window: Sequence[int | None],
    window: float,
    min_shot_seconds: float,
    duration: float | None = None,
) -> list[Shot]:
    """Runs of one choice, with every shot shorter than min_shot_seconds merged into a neighbour
    (the longer one; the earlier on a tie) so the camera never flickers. The last shot ends at
    `duration` when given, else at the end of the last window. A whole clip shorter than the
    minimum stays one shot."""
    if not choice_per_window:
        return []
    shots: list[Shot] = []
    for i, track in enumerate(choice_per_window):
        if shots and shots[-1].track == track:
            shots[-1] = Shot(shots[-1].start, (i + 1) * window, track)
        else:
            shots.append(Shot(i * window, (i + 1) * window, track))
    if duration is not None:
        last = shots[-1]
        shots[-1] = Shot(last.start, min(max(duration, last.start), last.end), last.track)

    while len(shots) > 1:
        short = [i for i, s in enumerate(shots) if s.end - s.start < min_shot_seconds - 1e-9]
        if not short:
            break
        index = min(short, key=lambda i: (shots[i].end - shots[i].start, i))
        before = shots[index - 1] if index > 0 else None
        after = shots[index + 1] if index + 1 < len(shots) else None
        if before is not None and (
            after is None or before.end - before.start >= after.end - after.start
        ):
            target = index - 1
        else:
            target = index + 1
        merged = _merge(shots[min(index, target)], shots[max(index, target)], shots[target].track)
        shots[min(index, target) : max(index, target) + 1] = [merged]
        shots = _coalesce(shots)
    return shots


def _merge(first: Shot, second: Shot, track: int | None) -> Shot:
    return Shot(first.start, second.end, track)


def _coalesce(shots: list[Shot]) -> list[Shot]:
    out: list[Shot] = []
    for shot in shots:
        if out and out[-1].track == shot.track:
            out[-1] = Shot(out[-1].start, shot.end, shot.track)
        else:
            out.append(shot)
    return out


# --- camera path --------------------------------------------------------------------------


def _even(value: float) -> int:
    return int(round(value / 2)) * 2


def camera_path(
    shots: Sequence[Shot],
    tracks: Sequence[Track],
    frame_w_px: int,
    crop_w_px: int,
    sample_times: Sequence[float],
    cfg: SpeakerConfig,
) -> list[int]:
    """The crop's left edge in source pixels at each sample time (even integers).

    Within a shot the crop follows the shot's face centre with exponential smoothing and a dead
    zone (it does not move while the target is within dead_zone * crop width of it). At a shot
    boundary it jumps straight to the new face. It is clamped to the frame. A sample without
    the shot's face keeps the last position; samples before the first face use the first
    position found; a clip with no usable face stays centred. `tracks` use the same sample
    indices as `sample_times`.
    """
    upper = max(frame_w_px - crop_w_px, 0)
    top = upper - upper % 2
    centre = _even(upper / 2)
    by_id = {t.id: t for t in tracks}
    positions: list[float | None] = []
    x: float | None = None
    reset = True
    shot_index = -1
    for k, t in enumerate(sample_times):
        while shot_index + 1 < len(shots) and t >= shots[shot_index + 1].start - 1e-9:
            shot_index += 1
            reset = True
        shot = shots[shot_index] if 0 <= shot_index < len(shots) else None
        face = None
        if shot is not None and shot.track is not None and shot.track in by_id:
            face = by_id[shot.track].by_frame.get(k)
        if face is not None:
            target = min(max(face.cx * frame_w_px - crop_w_px / 2, 0.0), float(upper))
            if x is None or reset:
                x = target
            elif abs(target - x) > cfg.dead_zone * crop_w_px:
                x += cfg.smoothing * (target - x)
            reset = False
        positions.append(x)

    first = next((p for p in positions if p is not None), None)
    if first is None:
        return [centre] * len(sample_times)
    out: list[int] = []
    for p in positions:
        value = first if p is None else p
        out.append(min(max(_even(value), 0), top))
    return out


# --- cuts and segments --------------------------------------------------------------------


def signature_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Half the sum of absolute differences: 0..1 for two normalised histograms."""
    if len(a) != len(b):
        raise ValueError("signatures must have the same length")
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / 2


def find_cuts(signatures: Sequence[Sequence[float]], threshold: float) -> list[int]:
    """Sample indices k (1 <= k < n) where a new camera shot starts.

    A cut needs the picture to change by at least `threshold` from sample k-1 to k. A one-sample
    flash is not a cut: when sample k+1 looks like k-1 again, k is an outlier and neither k nor
    k+1 is reported. Only neighbours are compared, so a slow change (every step below the
    threshold) is never a cut, however far the picture drifts.
    """
    cuts: list[int] = []
    k = 1
    while k < len(signatures):
        if signature_distance(signatures[k - 1], signatures[k]) < threshold:
            k += 1
            continue
        if k + 1 < len(signatures) and (
            signature_distance(signatures[k - 1], signatures[k + 1]) < threshold
        ):
            k += 2  # a flash: the picture is back where it was
            continue
        cuts.append(k)
        k += 1
    return cuts


def split_at_cuts(count: int, cuts: Sequence[int]) -> list[tuple[int, int]]:
    """Half-open sample ranges [a, b) covering 0..count, split at the cuts. Cuts outside
    1..count-1 and duplicates are ignored."""
    if count <= 0:
        return []
    edges = [0, *sorted({c for c in cuts if 1 <= c < count}), count]
    return list(zip(edges, edges[1:], strict=False))


@dataclass(frozen=True)
class Followed:
    """The camera's work on a whole clip: clip-wide tracks and shots, a crop x per sample, and the
    speaker switches inside camera shots (a cut is never a switch)."""

    tracks: list[Track]
    shots: list[Shot]
    crop_xs: list[int]
    switches: int


def follow_segments(
    frames: Sequence[Sequence[FaceSample]],
    times: Sequence[float],
    words: Sequence[tuple[float, float]],
    cuts: Sequence[int],
    duration: float,
    frame_w_px: int,
    crop_w_px: int,
    cfg: SpeakerConfig,
) -> Followed:
    """Follow the speaker in each camera shot on its own and join the results.

    `times` are seconds from the clip start per sample, `words` are (start, end) seconds from the
    clip start, `cuts` are sample indices from find_cuts. Each segment is run through link_tracks,
    speech_windows, choose_speakers, build_shots and camera_path as if it were a clip of its own,
    so no track, hysteresis state or crop position crosses a cut and the minimum shot length
    applies inside a segment only. Results are shifted back: shots to clip seconds, track ids
    unique across the clip, Track.by_frame keyed by clip-wide sample index (holding the original
    FaceSamples) and crop xs in sample order.
    """
    max_gap = max(1, round(MAX_LINK_GAP_SECONDS * cfg.sample_fps))
    tracks: list[Track] = []
    shots: list[Shot] = []
    crop_xs: list[int] = []
    switches = 0
    for a, b in split_at_cuts(len(frames), cuts):
        start = times[a]
        end = times[b] if b < len(times) else duration
        length = end - start
        local_times = [round(t - start, 3) for t in times[a:b]]
        local_words = [(w0 - start, w1 - start) for w0, w1 in words]
        # the speaker is chosen per window from each sample's own time, so those shift too
        local_frames = [
            [replace(f, t=round(f.t - start, 3)) for f in faces] for faces in frames[a:b]
        ]
        originals = {
            id(local): original
            for local_faces, faces in zip(local_frames, frames[a:b], strict=True)
            for local, original in zip(local_faces, faces, strict=True)
        }
        local_tracks = link_tracks(local_frames, cfg.link_max_distance, max_gap=max_gap)
        speech = speech_windows(
            local_words, length, cfg.window_seconds, cfg.min_speech_words_per_window
        )
        choice = choose_speakers(local_tracks, speech, cfg.window_seconds, cfg)
        local_shots = build_shots(choice, cfg.window_seconds, cfg.min_shot_seconds, length)
        crop_xs += camera_path(local_shots, local_tracks, frame_w_px, crop_w_px, local_times, cfg)
        switches += sum(
            1 for x, y in zip(local_shots, local_shots[1:], strict=False) if x.track != y.track
        )
        offset = len(tracks)
        for t in local_tracks:
            tracks.append(
                Track(
                    id=offset + t.id,
                    by_frame={a + k: originals[id(s)] for k, s in t.by_frame.items()},
                )
            )
        if not local_shots:  # keep the shots gap-free
            shots.append(Shot(start, end, None))
        for shot in local_shots:
            track = None if shot.track is None else offset + shot.track
            shots.append(Shot(shot.start + start, shot.end + start, track))
    return Followed(tracks=tracks, shots=shots, crop_xs=crop_xs, switches=switches)


# --- fallback and summary -----------------------------------------------------------------


def fallback_reason(
    frames: Sequence[Sequence[FaceSample]],
    cfg: SpeakerConfig,
    frame_w: int,
    crop_w: int,
    largest_heights: Sequence[float] | None = None,
) -> str | None:
    """Why a centre crop should be used instead of following faces, or None to follow them.

    Face sizes are fractions of the frame, so the frame height is not needed here. When
    `largest_heights` is given (the tallest face the detector saw in each sample, before small ones
    were dropped; 0.0 for none), "no face" and "faces too small" are judged from it instead of
    from `frames`.
    """
    if frame_w <= crop_w:
        return FALLBACK_SOURCE_NOT_WIDER
    if largest_heights is None:
        largest = [max(f.h for f in faces) for faces in frames if faces]
    else:
        largest = [h for h in largest_heights if h > 0.0]
    if not largest:
        return FALLBACK_NO_FACE
    small = sum(1 for h in largest if h < cfg.min_face_height)
    if small / len(largest) >= SMALL_FACE_MAJORITY:
        return FALLBACK_FACES_TOO_SMALL
    return None


def summarise(
    frames: Sequence[Sequence[FaceSample]],
    tracks: Sequence[Track],
    shots: Sequence[Shot],
    cut_times: Sequence[float] = (),
) -> CameraSummary:
    """Counts for the report. A boundary between two shots that lies on a cut time (within 1e-6 s)
    is a cut, not a speaker switch."""
    with_face = sum(1 for faces in frames if faces)
    pct = 100.0 * with_face / len(frames) if frames else 0.0
    switches = sum(
        1
        for a, b in zip(shots, shots[1:], strict=False)
        if a.track != b.track and not any(abs(a.end - c) <= 1e-6 for c in cut_times)
    )
    face_count = sum(len(faces) for faces in frames)
    readable = sum(1 for faces in frames for f in faces if f.mouth is not None)
    return CameraSummary(
        faces_seen=len(tracks),
        switches=switches,
        face_time_pct=round(pct, 1),
        faces_per_sample=round(face_count / len(frames), 2) if frames else 0.0,
        mouth_read_pct=round(100.0 * readable / face_count, 1) if face_count else 0.0,
    )
