"""Builders for the campaign tests: synthetic watermark PNGs and campaign folders.

All words and names here are invented.
"""

import struct
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml
from numpy.typing import NDArray

Bar = tuple[int, int, int, int]  # x1, y1, x2, y2 of the white bar, x2 and y2 exclusive
DEFAULT_BAR: Bar = (130, 38, 230, 58)


def write_png(path: Path, pixels: NDArray[Any]) -> None:
    """Encode with OpenCV (BGR/BGRA/grey, 8 or 16 bit) and write the bytes."""
    ok, buf = cv2.imencode(".png", pixels)
    assert ok
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.tobytes())


def transparent_mark(
    width: int = 360, height: int = 96, bar: Bar = DEFAULT_BAR
) -> NDArray[np.uint8]:
    """A white bar on a fully transparent canvas (BGRA)."""
    pixels = np.zeros((height, width, 4), np.uint8)
    x1, y1, x2, y2 = bar
    pixels[y1:y2, x1:x2] = 255
    return pixels


def black_mark(width: int = 360, height: int = 96, bar: Bar = DEFAULT_BAR) -> NDArray[np.uint8]:
    """A white bar on an opaque black canvas (BGRA)."""
    pixels = np.zeros((height, width, 4), np.uint8)
    pixels[:, :, 3] = 255
    x1, y1, x2, y2 = bar
    pixels[y1:y2, x1:x2, :3] = 255
    return pixels


def png_bytes(
    width: int,
    height: int,
    color_type: int,
    bit_depth: int,
    rows: bytes,
    *,
    before_idat: tuple[tuple[bytes, bytes], ...] = (),
) -> bytes:
    """A PNG from the standard library; `rows` is the raw scanline data with filter bytes."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    parts = [b"\x89PNG\r\n\x1a\n", chunk(b"IHDR", header)]
    parts += [chunk(kind, body) for kind, body in before_idat]
    parts += [chunk(b"IDAT", zlib.compress(rows)), chunk(b"IEND", b"")]
    return b"".join(parts)


def base_campaign() -> dict[str, Any]:
    return {
        "name": "Demo Campaign",
        "source_note": "invented",
        "clip_length": {"min": 20, "max": 60},
        "hook": {"mode": "full", "max_chars": 30},
        "watermark": {
            "file": "assets/mark.png",
            "position": {"x_center": 0.5, "y_center": 0.18},
            "scale": 1.0,
        },
        "captions": {"style": "box_karaoke"},
        "spelling": [{"from": "zorble", "to": "Zorb1e"}, {"from": "q x v", "to": "QXV"}],
        "names_to_check": ["Zorb1e", "QXV"],
        "post": {
            "prefix": {"tiktok": "@demo"},
            "closers": ["closer one", "closer two", "closer three"],
            "hashtags": [],
        },
    }


def write_campaign(
    project: Path,
    slug: str = "demo",
    *,
    mutate: Callable[[dict[str, Any]], None] | None = None,
    png: NDArray[Any] | None = None,
) -> Path:
    """Write campaigns/<slug>/ (campaign.yaml and assets/mark.png) and return the folder."""
    folder = project / "campaigns" / slug
    data = base_campaign()
    if mutate is not None:
        mutate(data)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "campaign.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    write_png(folder / "assets" / "mark.png", transparent_mark() if png is None else png)
    return folder


def moment(**fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "A01",
        "start": "10:00",
        "end": "10:40",
        "hook": "AN INVENTED HOOK",
        "caption": "says something invented.",
        "platform": "tiktok",
    }
    entry.update(fields)
    return entry


def write_moments(folder: Path, entries: list[dict[str, Any]]) -> None:
    (folder / "moments.yaml").write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")
