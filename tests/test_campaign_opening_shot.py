"""A very short opening shot is skipped (fix/low-contrast-cuts): when the source cuts within the
first campaign.skip_opening_shot_s and no word starts before the cut, the clip starts at the cut.
Invented words only; the tracker is stubbed."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clipmachine import tracking
from clipmachine.campaign import render as campaign_render
from clipmachine.utils import ffmpeg

START, END = 100.0, 140.0
NAME = "Q01_youtube"


def segments(*words: tuple[float, float]) -> list[SimpleNamespace]:
    """One segment whose words start and end at these source times."""
    return [
        SimpleNamespace(
            words=[
                SimpleNamespace(word=f" zorb{i}", start=a, end=b) for i, (a, b) in enumerate(words)
            ]
        )
    ]


@pytest.fixture
def stub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """The tracker, stubbed: `cuts` are what it reports (seconds from the clip start); analyses
    write <id>.track.json like the real one; `cached` maps a clip id to a reusable track."""
    state = SimpleNamespace(cuts=[0.46, 9.1], error=None, calls=[], cached={}, previews=[])

    def track_for_clip(
        plan: Any, clip_id: str, start: float, end: float, *, preview: bool, crop_w: int
    ) -> tuple[Any, bool]:
        state.calls.append((clip_id, start, end))
        state.previews.append(preview)
        if state.error is not None:
            raise state.error
        tracking.track_path(tmp_path, clip_id).write_text(f"track {start}", encoding="utf-8")
        return SimpleNamespace(cuts_exact=list(state.cuts)), False

    def load_plan_track(plan: Any, clip_id: str, start: float, end: float, *, crop_w: int) -> Any:
        return state.cached.get(clip_id)

    monkeypatch.setattr(tracking, "track_for_clip", track_for_clip)
    monkeypatch.setattr(tracking, "load_plan_track", load_plan_track)
    state.dir = tmp_path
    return state


def context(
    stub: SimpleNamespace,
    *words: tuple[float, float],
    limit: float = 0.6,
    speaker: bool = True,
    min_len: float = 20,
    preview: bool = False,
) -> Any:
    plan = SimpleNamespace(
        speaker=SimpleNamespace(clips_dir=stub.dir) if speaker else None,
        preview=preview,
        crop=SimpleNamespace(w=810),
        segments=segments(*words),
    )
    return SimpleNamespace(
        plan=plan,
        app=SimpleNamespace(campaign=SimpleNamespace(skip_opening_shot_s=limit)),
        campaign=SimpleNamespace(
            config=SimpleNamespace(clip_length=SimpleNamespace(min=min_len, max=60))
        ),
    )


def work() -> Any:
    return SimpleNamespace(moment=SimpleNamespace(clip_name=NAME), snap_notes=[], start=START)


def skip(ctx: Any, w: Any | None = None) -> float:
    return campaign_render._skip_opening_shot(ctx, w or work(), START, END)


def test_a_flash_before_the_first_word_is_skipped(stub: SimpleNamespace) -> None:
    w = work()
    moved = skip(context(stub, (100.5, 100.9)), w)
    assert moved == pytest.approx(100.46)
    assert w.start == moved
    assert w.snap_notes == ["start moved +0.46 s to a camera cut (a short opening shot skipped)"]


def test_a_word_before_the_cut_keeps_the_start(stub: SimpleNamespace) -> None:
    w = work()
    assert skip(context(stub, (100.2, 100.6)), w) == START
    assert w.snap_notes == []


def test_a_word_already_running_at_the_start_keeps_it(stub: SimpleNamespace) -> None:
    # the word began before the clip: clamped to 0, so it starts before the cut
    assert skip(context(stub, (99.8, 100.3))) == START


def test_a_cut_after_the_limit_or_none_keeps_the_start(stub: SimpleNamespace) -> None:
    stub.cuts = [0.9, 9.1]
    assert skip(context(stub, (101.0, 101.4))) == START
    stub.cuts = []
    assert skip(context(stub, (101.0, 101.4))) == START


def test_a_cut_exactly_at_the_limit_counts_and_one_at_zero_does_not(stub: SimpleNamespace) -> None:
    stub.cuts = [0.6]
    assert skip(context(stub, (101.0, 101.4))) == pytest.approx(100.6)
    stub.cuts = [0.0, 9.1]
    assert skip(context(stub, (101.0, 101.4))) == START


def test_two_flashes_are_both_skipped_but_never_past_the_first_word(
    stub: SimpleNamespace,
) -> None:
    stub.cuts = [0.2, 0.5, 9.1]
    assert skip(context(stub, (101.0, 101.4))) == pytest.approx(100.5)
    assert skip(context(stub, (100.3, 100.7))) == pytest.approx(100.2)  # speech from 0.3 s


def test_a_move_that_breaks_the_length_range_keeps_the_start(stub: SimpleNamespace) -> None:
    ctx = context(stub, (100.5, 100.9), min_len=40)  # 40 s now, 39.54 s after the move
    assert skip(ctx) == START


def test_off_or_without_a_tracker_nothing_is_analysed(stub: SimpleNamespace) -> None:
    assert skip(context(stub, (100.5, 100.9), limit=0)) == START
    assert skip(context(stub, (100.5, 100.9), speaker=False)) == START
    assert stub.calls == []


def test_a_tracking_failure_keeps_the_start(stub: SimpleNamespace) -> None:
    stub.error = ffmpeg.MediaError("no frames")
    assert skip(context(stub, (100.5, 100.9))) == START


def test_the_original_starts_track_is_kept_when_the_start_moves_and_reused_next_time(
    stub: SimpleNamespace,
) -> None:
    kept = tracking.track_path(stub.dir, f"{NAME}{campaign_render.OPENING_SUFFIX}")
    assert skip(context(stub, (100.5, 100.9))) == pytest.approx(100.46)
    assert kept.read_text(encoding="utf-8") == f"track {START}"  # saved before the crop's own
    assert stub.calls == [(NAME, START, END)]

    stub.cached[kept.name.removesuffix(".track.json")] = SimpleNamespace(cuts_exact=[0.46])
    assert skip(context(stub, (100.5, 100.9))) == pytest.approx(100.46)
    assert stub.calls == [(NAME, START, END)]  # the second render analysed nothing


def test_an_unmoved_clip_keeps_no_opening_track_and_the_preview_setting_is_passed(
    stub: SimpleNamespace,
) -> None:
    assert skip(context(stub, (100.2, 100.6), preview=True)) == START
    assert stub.previews == [True]
    assert not tracking.track_path(stub.dir, f"{NAME}{campaign_render.OPENING_SUFFIX}").exists()
