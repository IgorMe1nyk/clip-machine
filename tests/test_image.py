from pathlib import Path

import cv2
import numpy as np
import pytest

from campaign_helpers import black_mark, png_bytes, transparent_mark, write_png
from clipmachine.utils.image import (
    VISIBLE_ALPHA,
    VISIBLE_BRIGHTNESS,
    ImageError,
    background_kind,
    color_type_name,
    load_pixels,
    png_info,
    visible_box,
)


def test_png_info_for_rgba_rgb_and_greyscale(tmp_path: Path) -> None:
    rgba = tmp_path / "a.png"
    write_png(rgba, np.zeros((5, 7, 4), np.uint8))
    info = png_info(rgba)
    assert (info.width, info.height, info.bit_depth, info.color_type) == (7, 5, 8, 6)
    assert info.has_alpha

    rgb = tmp_path / "b.png"
    write_png(rgb, np.zeros((5, 7, 3), np.uint8))
    info = png_info(rgb)
    assert (info.color_type, info.has_alpha) == (2, False)

    grey = tmp_path / "c.png"
    write_png(grey, np.zeros((5, 7), np.uint8))
    info = png_info(grey)
    assert (info.color_type, info.has_alpha) == (0, False)


def test_png_info_palette_has_alpha_only_with_a_trns_chunk_before_the_data(
    tmp_path: Path,
) -> None:
    plte = (b"PLTE", b"\x10\x20\x30")
    rows = b"\x00\x00"
    plain = tmp_path / "plain.png"
    plain.write_bytes(png_bytes(1, 1, 3, 8, rows, before_idat=(plte,)))
    assert png_info(plain).color_type == 3 and not png_info(plain).has_alpha

    clear = tmp_path / "clear.png"
    clear.write_bytes(png_bytes(1, 1, 3, 8, rows, before_idat=(plte, (b"tRNS", b"\x00"))))
    assert png_info(clear).has_alpha


def test_png_info_greyscale_with_alpha(tmp_path: Path) -> None:
    path = tmp_path / "ga.png"
    path.write_bytes(png_bytes(1, 1, 4, 8, b"\x00\x64\x32"))
    info = png_info(path)
    assert (info.color_type, info.has_alpha) == (4, True)


def test_png_info_rejects_bad_files(tmp_path: Path) -> None:
    good = png_bytes(2, 2, 0, 8, b"\x00\x01\x02\x00\x03\x04")
    bad_signature = tmp_path / "sig.png"
    bad_signature.write_bytes(b"GIF89a" + good[6:])
    with pytest.raises(ImageError, match="not a PNG"):
        png_info(bad_signature)

    truncated = tmp_path / "cut.png"
    truncated.write_bytes(good[: len(good) // 2])
    with pytest.raises(ImageError, match="cut short"):
        png_info(truncated)

    cut_in_header = tmp_path / "cut2.png"
    cut_in_header.write_bytes(good[:20])
    with pytest.raises(ImageError, match="cut short"):
        png_info(cut_in_header)

    empty = tmp_path / "empty.png"
    empty.write_bytes(b"")
    with pytest.raises(ImageError):
        png_info(empty)

    zero = tmp_path / "zero.png"
    zero.write_bytes(png_bytes(0, 4, 0, 8, b""))
    with pytest.raises(ImageError, match="zero"):
        png_info(zero)

    with pytest.raises(ImageError, match="cannot read"):
        png_info(tmp_path / "missing.png")


def test_color_type_names() -> None:
    assert [color_type_name(n) for n in (0, 2, 3, 4, 6)] == [
        "greyscale",
        "RGB",
        "palette",
        "greyscale + alpha",
        "RGBA",
    ]


def test_load_pixels_gives_bgra_for_every_kind(tmp_path: Path) -> None:
    bgra = np.zeros((3, 4, 4), np.uint8)
    bgra[1, 2] = (10, 20, 30, 40)
    write_png(tmp_path / "a.png", bgra)
    assert load_pixels(tmp_path / "a.png").tolist() == bgra.tolist()

    bgr = np.zeros((3, 4, 3), np.uint8)
    bgr[1, 2] = (10, 20, 30)
    write_png(tmp_path / "b.png", bgr)
    got = load_pixels(tmp_path / "b.png")
    assert got.shape == (3, 4, 4) and tuple(got[1, 2]) == (10, 20, 30, 255)

    grey = np.zeros((3, 4), np.uint8)
    grey[1, 2] = 77
    write_png(tmp_path / "c.png", grey)
    got = load_pixels(tmp_path / "c.png")
    assert tuple(got[1, 2]) == (77, 77, 77, 255) and got[0, 0, 3] == 255


def test_load_pixels_grey_with_alpha(tmp_path: Path) -> None:
    path = tmp_path / "ga.png"
    path.write_bytes(png_bytes(1, 1, 4, 8, b"\x00\x64\x32"))
    assert tuple(load_pixels(path)[0, 0]) == (100, 100, 100, 50)


def test_load_pixels_scales_16_bit_down(tmp_path: Path) -> None:
    wide = np.zeros((2, 2, 4), np.uint16)
    wide[0, 0] = (65535, 257 * 100, 0, 65535)
    write_png(tmp_path / "w.png", wide)
    assert png_info(tmp_path / "w.png").bit_depth == 16
    got = load_pixels(tmp_path / "w.png")
    assert got.dtype == np.uint8
    assert tuple(got[0, 0]) == (255, 100, 0, 255)


def test_load_pixels_reads_non_ascii_paths_and_rejects_non_images(tmp_path: Path) -> None:
    folder = tmp_path / "café"
    folder.mkdir()
    write_png(folder / "mé.png", transparent_mark())
    assert load_pixels(folder / "mé.png").shape == (96, 360, 4)

    junk = tmp_path / "junk.png"
    junk.write_bytes(b"not an image at all")
    with pytest.raises(ImageError, match="decoded"):
        load_pixels(junk)


def test_visible_box_of_a_transparent_mark_uses_alpha_only() -> None:
    pixels = transparent_mark(bar=(130, 38, 230, 58))
    assert visible_box(pixels, by_brightness=False) == (130, 38, 230, 58)
    assert visible_box(pixels, by_brightness=True) == (130, 38, 230, 58)


def test_visible_box_of_a_black_background_depends_on_brightness() -> None:
    pixels = black_mark(bar=(130, 38, 230, 58))
    assert visible_box(pixels, by_brightness=False) == (0, 0, 360, 96)
    assert visible_box(pixels, by_brightness=True) == (130, 38, 230, 58)


def test_visible_box_thresholds() -> None:
    def one(alpha: int, level: int) -> tuple[int, int, int, int] | None:
        pixels = np.zeros((6, 8, 4), np.uint8)
        pixels[2, 5] = (0, level, 0, alpha)  # only green set: max(B, G, R) is `level`
        return visible_box(pixels, by_brightness=True)

    assert one(VISIBLE_ALPHA, 255) is None
    assert one(VISIBLE_ALPHA + 1, 255) == (5, 2, 6, 3)
    assert one(255, VISIBLE_BRIGHTNESS) is None
    assert one(255, VISIBLE_BRIGHTNESS + 1) == (5, 2, 6, 3)

    plain = np.zeros((6, 8, 4), np.uint8)
    plain[2, 5, 3] = VISIBLE_ALPHA + 1  # alpha alone is enough without the brightness rule
    assert visible_box(plain, by_brightness=False) == (5, 2, 6, 3)
    assert visible_box(plain, by_brightness=True) is None


def test_visible_box_is_none_when_nothing_is_visible() -> None:
    assert visible_box(np.zeros((4, 4, 4), np.uint8), by_brightness=False) is None
    assert visible_box(black_mark(bar=(0, 0, 0, 0)), by_brightness=True) is None


def test_background_kinds() -> None:
    assert background_kind(transparent_mark()) == "transparent"
    assert background_kind(black_mark()) == "opaque_black"
    white = np.full((4, 4, 4), 255, np.uint8)
    assert background_kind(white) == "opaque_other"

    clear = transparent_mark()
    clear[0, 10] = (255, 255, 255, 255)  # one visible pixel on the border
    assert background_kind(clear) == "opaque_other"

    dark = black_mark()
    dark[-1, 3, :3] = VISIBLE_BRIGHTNESS + 1
    assert background_kind(dark) == "opaque_other"
    dark[-1, 3, :3] = VISIBLE_BRIGHTNESS
    assert background_kind(dark) == "opaque_black"


def test_the_header_is_read_without_decoding(tmp_path: Path) -> None:
    path = tmp_path / "h.png"
    ok, buf = cv2.imencode(".png", np.zeros((9, 3, 4), np.uint8))
    assert ok
    path.write_bytes(buf.tobytes())
    assert (png_info(path).width, png_info(path).height) == (3, 9)
