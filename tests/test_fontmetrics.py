from pathlib import Path

import pytest

from clipmachine.utils.fontmetrics import FontError, FontMetrics, load_font

FONT_PATH = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "Montserrat-Bold.ttf"


@pytest.fixture(scope="module")
def font() -> FontMetrics:
    return load_font(FONT_PATH)


# --- the real font ----------------------------------------------------------------------------


def test_montserrat_basics(font: FontMetrics) -> None:
    assert font.units_per_em == 1000
    assert (font.win_ascent, font.win_descent) == (1109, 453)


def test_ass_font_size_becomes_the_em_libass_uses(font: FontMetrics) -> None:
    assert font.em_px(64) == pytest.approx(40.97, abs=0.01)


def test_space_advance_is_283_units(font: FontMetrics) -> None:
    assert font.space_px(64) == pytest.approx(283 * font.em_px(64) / 1000)


def test_text_width_matches_the_measured_render(font: FontMetrics) -> None:
    # 526.3 px is the ink width libass drew for this text at size 64 in the Brief R1 spike
    width = font.text_width("alpha beta gamma delta", 64)
    assert width == pytest.approx(526.3, rel=0.015)


def test_letter_spacing_adds_exactly_n_times_the_spacing(font: FontMetrics) -> None:
    text = "alpha beta"
    plain = font.text_width(text, 64)
    assert font.text_width(text, 64, 2.5) == pytest.approx(plain + len(text) * 2.5)


def test_vertical_metrics_are_sane(font: FontMetrics) -> None:
    assert font.ascent_px(64) == pytest.approx(1109 * font.em_px(64) / 1000)
    assert 0 < font.descent_px(64) < font.ascent_px(64)
    assert 0 < font.cap_px(64) < font.ascent_px(64)


def test_unknown_character_does_not_raise(font: FontMetrics) -> None:
    assert not font.has_glyph("\U0001f600")
    assert font.advance_px("\U0001f600", 64) > 0  # glyph 0 (.notdef)
    assert font.has_glyph("a")


def test_glyph_beyond_the_metrics_uses_the_last_advance() -> None:
    odd = FontMetrics(
        units_per_em=1000,
        win_ascent=1000,
        win_descent=1000,
        hhea_descent=200,
        cap_height=700.0,
        advances=(500, 600),
        cmap={ord("a"): 99},
    )
    assert odd.advance_px("a", 2000) == pytest.approx(600 * odd.em_px(2000) / 1000)


# --- bad files --------------------------------------------------------------------------------


def test_missing_file_raises_font_error(tmp_path: Path) -> None:
    with pytest.raises(FontError):
        load_font(tmp_path / "nothing.ttf")


def test_garbage_bytes_raise_font_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.ttf"
    bad.write_bytes(b"this is not a font at all" * 40)
    with pytest.raises(FontError):
        load_font(bad)
    bad.write_bytes(b"")
    with pytest.raises(FontError):
        load_font(bad)


@pytest.mark.parametrize("keep", [3, 11, 100, 400, 5000, 30000])
def test_truncated_font_raises_font_error(tmp_path: Path, keep: int) -> None:
    cut = tmp_path / "cut.ttf"
    cut.write_bytes(FONT_PATH.read_bytes()[:keep])
    with pytest.raises(FontError) as info:
        load_font(cut)
    assert "\n" not in str(info.value)


def test_a_valid_header_with_junk_tables_raises_font_error(tmp_path: Path) -> None:
    data = bytearray(FONT_PATH.read_bytes())
    for i in range(12, 300):  # scramble the table directory
        data[i] = (data[i] * 7 + 13) & 0xFF
    junk = tmp_path / "junk.ttf"
    junk.write_bytes(bytes(data))
    with pytest.raises(FontError):
        load_font(junk)
