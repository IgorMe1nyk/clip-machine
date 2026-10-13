"""PNG facts and pixels for the campaign watermark (Brief 11). No dependency beyond OpenCV.

`png_info` reads the header chunks with the standard library only, so it needs no decoder.
`load_pixels` decodes with OpenCV from the file's bytes (`cv2.imread` fails on non-ASCII Windows
paths). The visible box and the background kind are measured on the decoded pixels; the file
itself is never modified.
"""

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
VISIBLE_ALPHA = 8  # a pixel with alpha above this (of 255) counts as visible
VISIBLE_BRIGHTNESS = 16  # with blend screen, max(B, G, R) above this (of 255) is visible too

_COLOR_TYPE_NAMES = {
    0: "greyscale",
    2: "RGB",
    3: "palette",
    4: "greyscale + alpha",
    6: "RGBA",
}


class ImageError(Exception):
    """An image file that cannot be read as the PNG it should be."""


@dataclass(frozen=True)
class PngInfo:
    width: int
    height: int
    bit_depth: int
    color_type: int  # the IHDR number: 0, 2, 3, 4 or 6
    has_alpha: bool  # colour type 4 or 6, or a tRNS chunk before the image data


def color_type_name(color_type: int) -> str:
    return _COLOR_TYPE_NAMES.get(color_type, f"unknown ({color_type})")


def png_info(path: Path) -> PngInfo:
    """Width, height, bit depth, colour type and alpha, from the chunks. ImageError when the file
    is not a PNG, is cut short, or has no size."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ImageError(f"cannot read {path.name}: {exc.strerror or exc}") from exc
    if not data.startswith(PNG_SIGNATURE):
        raise ImageError(f"{path.name} is not a PNG file (bad signature)")
    pos = len(PNG_SIGNATURE)
    header: tuple[int, int, int, int] | None = None
    trns = False
    while True:
        if pos + 8 > len(data):
            raise ImageError(f"{path.name} is cut short (no image data chunk)")
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        body = data[pos + 8 : pos + 8 + length]
        if len(body) < length or pos + 12 + length > len(data):
            raise ImageError(f"{path.name} is cut short (a {kind.decode('latin-1')} chunk)")
        if header is None:
            if kind != b"IHDR" or length != 13:
                raise ImageError(f"{path.name} does not start with an IHDR chunk")
            width, height, depth, color = struct.unpack(">IIBB", body[:10])
            if width == 0 or height == 0:
                raise ImageError(f"{path.name} has a zero width or height")
            if color not in _COLOR_TYPE_NAMES:
                raise ImageError(f"{path.name} has an unknown colour type ({color})")
            header = (width, height, depth, color)
        elif kind == b"tRNS":
            trns = True
        elif kind == b"IDAT":
            break
        elif kind == b"IEND":
            raise ImageError(f"{path.name} has no image data")
        pos += 12 + length
    width, height, depth, color = header
    return PngInfo(width, height, depth, color, color in (4, 6) or trns)


def load_pixels(path: Path) -> "NDArray[np.uint8]":
    """The image as BGRA, 8 bits, shape (height, width, 4). ImageError when OpenCV cannot decode
    it."""
    import cv2
    import numpy as np

    try:
        raw = np.frombuffer(path.read_bytes(), np.uint8)
    except OSError as exc:
        raise ImageError(f"cannot read {path.name}: {exc.strerror or exc}") from exc
    decoded: Any = cv2.imdecode(raw, cv2.IMREAD_UNCHANGED)
    if decoded is None:
        raise ImageError(f"{path.name} could not be decoded as an image")
    img: Any = decoded
    if img.dtype == np.uint16:
        img = (img // 257).astype(np.uint8)  # 65535 -> 255
    elif img.dtype != np.uint8:
        raise ImageError(f"{path.name} has an unsupported pixel type ({img.dtype})")
    if img.ndim == 2:
        grey, alpha = img, np.full(img.shape, 255, np.uint8)
        out = np.dstack([grey, grey, grey, alpha])
    elif img.shape[2] == 2:
        grey = img[:, :, 0]
        out = np.dstack([grey, grey, grey, img[:, :, 1]])
    elif img.shape[2] == 3:
        out = np.dstack([img, np.full(img.shape[:2], 255, np.uint8)])
    elif img.shape[2] == 4:
        out = img
    else:
        raise ImageError(f"{path.name} has {img.shape[2]} channels, which is not supported")
    result: NDArray[np.uint8] = np.ascontiguousarray(out, dtype=np.uint8)
    return result


def _visible_mask(pixels: "NDArray[np.uint8]", by_brightness: bool) -> "NDArray[np.bool_]":
    mask: NDArray[np.bool_] = pixels[:, :, 3] > VISIBLE_ALPHA
    if by_brightness:
        mask = mask & (pixels[:, :, :3].max(axis=2) > VISIBLE_BRIGHTNESS)
    return mask


def visible_box(
    pixels: "NDArray[np.uint8]", *, by_brightness: bool
) -> tuple[int, int, int, int] | None:
    """(x1, y1, x2, y2) of the visible pixels, x2 and y2 exclusive; None when nothing is visible.
    Visible: alpha above VISIBLE_ALPHA and, with `by_brightness`, max(B, G, R) above
    VISIBLE_BRIGHTNESS."""
    import numpy as np

    mask = _visible_mask(pixels, by_brightness)
    cols = np.flatnonzero(mask.any(axis=0))
    if cols.size == 0:
        return None
    rows = np.flatnonzero(mask.any(axis=1))
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


def background_kind(pixels: "NDArray[np.uint8]") -> str:
    """From the one-pixel border: "transparent" (every border pixel nearly clear), "opaque_black"
    (every border pixel nearly black), else "opaque_other"."""
    import numpy as np

    border = np.concatenate([pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1]])
    if bool((border[:, 3] <= VISIBLE_ALPHA).all()):
        return "transparent"
    if bool((border[:, :3].max(axis=1) <= VISIBLE_BRIGHTNESS).all()):
        return "opaque_black"
    return "opaque_other"
