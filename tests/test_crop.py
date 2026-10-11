import pytest

from clipmachine.utils.crop import CropBox, center_crop, display_size

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
