import pytest

from clipmachine.utils.crop import CropBox, center_crop, display_size, frame_path

TARGET = (1080, 1920)


def crop(width: int, height: int, rotation: int = 0) -> CropBox:
    return center_crop(width, height, rotation, *TARGET)


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((1920, 1080), CropBox(w=606, h=1080, x=656, y=0)),  # landscape 16:9
        ((640, 360), CropBox(w=202, h=360, x=218, y=0)),  # the real test video
        ((1440, 1080), CropBox(w=606, h=1080, x=416, y=0)),  # 4:3
        ((1080, 1920), CropBox(w=1080, h=1920, x=0, y=0)),  # already 9:16: full frame
        ((720, 1280), CropBox(w=720, h=1280, x=0, y=0)),  # 9:16 at another size
        ((1080, 1080), CropBox(w=606, h=1080, x=236, y=0)),  # square
        ((1080, 2400), CropBox(w=1080, h=1920, x=0, y=240)),  # narrower than 9:16: trim height
    ],
)
def test_crop_boxes(size: tuple[int, int], expected: CropBox) -> None:
    assert crop(*size) == expected


def test_rotation_swaps_width_and_height() -> None:
    # stored 1080x1920 with 90 degrees plays as 1920x1080 landscape
    assert display_size(1080, 1920, 90) == (1920, 1080)
    assert display_size(1080, 1920, 270) == (1920, 1080)
    assert display_size(1080, 1920, 180) == (1080, 1920)
    assert crop(1080, 1920, 90) == crop(1920, 1080, 0)
    assert crop(1920, 1080, 270) == crop(1080, 1920, 0)


@pytest.mark.parametrize("size", [(1281, 721), (853, 481), (1001, 1001), (999, 2401), (641, 361)])
def test_odd_sizes_give_even_integers_inside_the_frame(size: tuple[int, int]) -> None:
    box = crop(*size)
    assert all(isinstance(v, int) for v in (box.w, box.h, box.x, box.y))
    assert box.w % 2 == 0 and box.h % 2 == 0
    assert box.x % 2 == 0 and box.y % 2 == 0
    assert box.x >= 0 and box.y >= 0
    assert box.x + box.w <= size[0] and box.y + box.h <= size[1]
    # within 2 pixels of the exact 9:16 ratio
    assert abs(box.w * 16 - box.h * 9) <= 2 * 16


def test_frame_too_small_is_an_error() -> None:
    with pytest.raises(ValueError, match="too small"):
        crop(1, 1)


# --- frame_path ---------------------------------------------------------------------------

TWO_SHOTS = [(0.0, 5.0), (5.0, 10.0)]
SWITCH_SAMPLES = [(1.0, 100), (2.0, 200), (6.0, 300), (7.0, 400)]
# one picture, then another from 5.0 s; each shot has samples every 0.1 s at a constant x
CUT_SAMPLES = [(round(k / 10, 3), 100 if k < 50 else 300) for k in range(100)]


def test_x_is_interpolated_between_two_samples_at_each_frame_time() -> None:
    path = frame_path([(0.0, 100), (1.0, 200)], [(0.0, 1.0)], [], [], 10, 1.0)
    assert path == [(round(n / 10, 6), 100 + 10 * n) for n in range(10)]


def test_x_is_always_an_even_integer() -> None:
    path = frame_path([(0.0, 101), (1.0, 203)], [(0.0, 1.0)], [], [], 30, 1.0)
    assert all(isinstance(x, int) and x % 2 == 0 for _, x in path)
    assert path[0][1] in (100, 102) and path[-1][1] > 190


def test_a_value_is_held_before_a_shots_first_sample_and_after_its_last() -> None:
    path = frame_path(SWITCH_SAMPLES, TWO_SHOTS, [], [], 10, 10.0)
    xs = dict(path)
    assert path[0] == (0.0, 100)  # held before the first sample at 1.0
    assert xs[1.1] == 110 and xs[1.9] == 190
    assert xs[2.0] == 200 and 2.1 not in xs  # held after the last sample of the shot
    assert xs[5.0] == 300 and 5.1 not in xs  # the next shot's first value, held to 6.0
    assert xs[6.1] == 310 and xs[7.0] == 400 and path[-1] == (7.0, 400)


def test_a_speaker_switch_is_a_hard_jump_at_the_shots_start() -> None:
    path = frame_path(SWITCH_SAMPLES, TWO_SHOTS, [], [], 10, 10.0)
    before = [e for e in path if e[0] < 5.0][-1]
    assert before == (2.0, 200)  # nothing between 200 and 300 on either side
    assert [e for e in path if e[0] >= 5.0][0] == (5.0, 300)


def test_a_camera_cut_jumps_at_its_exact_time_when_that_is_before_the_nominal_time() -> None:
    path = frame_path(CUT_SAMPLES, TWO_SHOTS, [5.0], [4.96], 10, 10.0)
    assert path == [(0.0, 100), (4.96, 300)]


def test_a_camera_cut_jumps_at_its_exact_time_when_that_is_after_the_nominal_time() -> None:
    # the new shot's first sample is at 5.0, before the exact cut: it must not pull 4.9..5.04 over
    path = frame_path(CUT_SAMPLES, TWO_SHOTS, [5.0], [5.04], 10, 10.0)
    assert path == [(0.0, 100), (5.04, 300)]


def test_two_cuts_each_use_their_own_exact_time() -> None:
    shots = [(0.0, 3.0), (3.0, 6.0), (6.0, 9.0)]
    samples = [(k / 10, (100, 300, 500)[k // 30]) for k in range(90)]
    path = frame_path(samples, shots, [3.0, 6.0], [3.02, 5.97], 10, 9.0)
    assert path == [(0.0, 100), (3.02, 300), (5.97, 500)]


@pytest.mark.parametrize("exact", [[], [4.96, 7.0]])
def test_without_matching_exact_times_the_nominal_time_is_used(exact: list[float]) -> None:
    path = frame_path(CUT_SAMPLES, TWO_SHOTS, [5.0], exact, 10, 10.0)
    assert path == [(0.0, 100), (5.0, 300)]


def test_a_shot_start_that_is_not_a_cut_stays_nominal() -> None:
    path = frame_path(CUT_SAMPLES, TWO_SHOTS, [7.0], [7.02], 10, 10.0)
    assert path == [(0.0, 100), (5.0, 300)]


def test_exact_times_are_clamped_to_the_clip() -> None:
    assert frame_path(CUT_SAMPLES, TWO_SHOTS, [5.0], [-1.0], 10, 10.0) == [(0.0, 300)]
    assert frame_path(CUT_SAMPLES, TWO_SHOTS, [5.0], [12.0], 10, 10.0) == [(0.0, 100)]


def test_frame_times_follow_a_fractional_frame_rate() -> None:
    fps = 30000 / 1001
    path = frame_path([(0.0, 0), (1.0, 1000)], [(0.0, 1.0)], [], [], fps, 1.0)
    times = [round(n * 1001 / 30000, 6) for n in range(30)]
    assert [t for t, _ in path] == times
    assert [x for _, x in path] == [2 * round(500 * t) for t in times]


def test_a_path_that_never_moves_is_one_entry() -> None:
    assert frame_path([(0.0, 218), (5.0, 218)], [(0.0, 10.0)], [], [], 30, 10.0) == [(0.0, 218)]


def test_only_changes_are_emitted_in_strictly_increasing_time() -> None:
    path = frame_path(SWITCH_SAMPLES, TWO_SHOTS, [5.0], [5.03], 30000 / 1001, 10.0)
    assert len(path) > 3
    assert all(a[0] < b[0] and a[1] != b[1] for a, b in zip(path, path[1:], strict=False))


def test_a_shot_without_samples_takes_the_nearest_sample() -> None:
    assert frame_path([(1.0, 100)], TWO_SHOTS, [], [], 10, 10.0) == [(0.0, 100)]
    assert frame_path([(8.0, 400)], TWO_SHOTS, [], [], 10, 10.0) == [(0.0, 400)]


def test_no_shots_means_one_shot() -> None:
    assert frame_path([(0.0, 100), (1.0, 200)], [], [], [], 10, 1.0)[1] == (0.1, 110)


def test_frame_path_refuses_no_samples_and_a_bad_frame_rate() -> None:
    with pytest.raises(ValueError, match="sample"):
        frame_path([], TWO_SHOTS, [], [], 30, 10.0)
    for fps in (0, -30):
        with pytest.raises(ValueError, match="fps"):
            frame_path([(0.0, 100)], TWO_SHOTS, [], [], fps, 10.0)
