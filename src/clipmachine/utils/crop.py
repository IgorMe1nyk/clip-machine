"""Centre-crop arithmetic for the render stage. Pure integer math, no filter expressions."""

from dataclasses import dataclass

AUTO_ROTATED = frozenset({90, 270})


@dataclass(frozen=True)
class CropBox:
    """A crop rectangle in pixels of the upright (auto-rotated) frame."""

    w: int
    h: int
    x: int
    y: int


def display_size(width: int, height: int, rotation: int) -> tuple[int, int]:
    """Frame size after ffmpeg's auto-rotation: width and height swap at 90 and 270 degrees."""
    if rotation % 360 in AUTO_ROTATED:
        return height, width
    return width, height


def _even(value: int) -> int:
    return value - value % 2


def center_crop(
    width: int, height: int, rotation: int, target_width: int, target_height: int
) -> CropBox:
    """The largest centred rectangle with the target's aspect ratio, in even integers.

    width, height and rotation are the stored (ffprobe) values of the source. A source that
    already has the target ratio gets a full-frame crop. Raises ValueError for a frame too small
    to hold a 2x2 crop.
    """
    frame_w, frame_h = display_size(width, height, rotation)
    if frame_w * target_height > frame_h * target_width:  # wider than the target
        crop_h = frame_h
        crop_w = frame_h * target_width // target_height
    else:
        crop_w = frame_w
        crop_h = frame_w * target_height // target_width
    crop_w, crop_h = _even(crop_w), _even(crop_h)
    if crop_w < 2 or crop_h < 2:
        raise ValueError(f"frame {frame_w}x{frame_h} is too small to crop")
    return CropBox(
        w=crop_w,
        h=crop_h,
        x=_even((frame_w - crop_w) // 2),
        y=_even((frame_h - crop_h) // 2),
    )
