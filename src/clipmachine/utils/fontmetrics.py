"""Text measurements read straight from a TrueType/OpenType font file (stdlib only).

libass measures text from the same file, so these numbers place a drawn box around a caption.
What libass does, as measured in Brief R1: the ASS Fontsize becomes an em size of
`font_size * unitsPerEm / (usWinAscent + usWinDescent)` pixels; kerning is not applied, so a
width is the sum of the glyphs' horizontal advances; the line box is that tall, with the baseline
`usWinAscent` (scaled) below its top.

The font file is an input that can be malformed: `load_font` raises `FontError` for anything
wrong with it, and nothing else.
"""

import struct
from dataclasses import dataclass, field
from pathlib import Path

CAP_HEIGHT_FALLBACK = 0.7  # of the em, for OS/2 tables older than version 2
MAX_CMAP_ENTRIES = 0x110000  # more mapped characters than this means a broken table
_SFNT_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"true")


class FontError(Exception):
    """The font file is missing, unreadable or not a usable TrueType/OpenType font."""


@dataclass(frozen=True)
class FontMetrics:
    units_per_em: int
    win_ascent: int  # font units: line-box top to baseline (hhea ascender if the OS/2 sum is 0)
    win_descent: int  # font units below the baseline in the line box
    hhea_descent: int  # font units, positive: how far descenders reach below the baseline
    cap_height: float  # font units
    advances: tuple[int, ...] = field(repr=False)  # advance widths of the first glyphs
    cmap: dict[int, int] = field(repr=False, compare=False)  # code point -> glyph index

    def em_px(self, font_size: float) -> float:
        """Pixels per em for an ASS Fontsize (the way libass converts it)."""
        return font_size * self.units_per_em / (self.win_ascent + self.win_descent)

    def advance_px(self, char: str, font_size: float) -> float:
        """The glyph's horizontal advance. A character the font lacks counts as glyph 0."""
        glyph = self.cmap.get(ord(char), 0)
        units = self.advances[min(glyph, len(self.advances) - 1)]
        return units * self.em_px(font_size) / self.units_per_em

    def text_width(self, text: str, font_size: float, letter_spacing: float = 0.0) -> float:
        """Advances summed (no kerning) plus `letter_spacing` pixels after every character."""
        return sum(self.advance_px(c, font_size) for c in text) + letter_spacing * len(text)

    def space_px(self, font_size: float) -> float:
        return self.advance_px(" ", font_size)

    def ascent_px(self, font_size: float) -> float:
        """From the top of the line box down to the baseline."""
        return self.win_ascent * self.em_px(font_size) / self.units_per_em

    def cap_px(self, font_size: float) -> float:
        """Height of a capital letter above the baseline."""
        return self.cap_height * self.em_px(font_size) / self.units_per_em

    def descent_px(self, font_size: float) -> float:
        """How far descenders reach below the baseline."""
        return self.hhea_descent * self.em_px(font_size) / self.units_per_em

    def has_glyph(self, char: str) -> bool:
        return ord(char) in self.cmap


def load_font(path: Path) -> FontMetrics:
    """Read the metrics of a font file. Raises FontError for every problem with it."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FontError(f"cannot read font file {path.name}: {exc.strerror or exc}") from exc
    try:
        return _parse(data)
    except FontError:
        raise
    except (struct.error, KeyError, IndexError, ValueError, OverflowError) as exc:
        raise FontError(
            f"font file {path.name} is damaged or not a TrueType/OpenType font"
        ) from exc


def _parse(data: bytes) -> FontMetrics:
    if data[:4] not in _SFNT_MAGIC:
        raise FontError("not a TrueType/OpenType font (bad header)")
    tables = _tables(data)
    upm = struct.unpack_from(">H", data, tables["head"] + 18)[0]
    if upm == 0:
        raise FontError("unitsPerEm is 0")
    hhea = tables["hhea"]
    hhea_ascent, hhea_descent = struct.unpack_from(">hh", data, hhea + 4)
    metric_count = struct.unpack_from(">H", data, hhea + 34)[0]
    if metric_count == 0:
        raise FontError("the font has no horizontal metrics")
    os2 = tables["OS/2"]
    os2_version = struct.unpack_from(">H", data, os2)[0]
    win_ascent, win_descent = struct.unpack_from(">HH", data, os2 + 74)
    if win_ascent + win_descent == 0:
        win_ascent, win_descent = hhea_ascent, -hhea_descent
    if win_ascent <= 0 or win_ascent + win_descent <= 0:
        raise FontError("the font has no usable line height")
    cap = struct.unpack_from(">h", data, os2 + 88)[0] if os2_version >= 2 else 0
    cap_height = float(cap) if cap > 0 else CAP_HEIGHT_FALLBACK * upm
    hmtx = tables["hmtx"]
    advances = tuple(struct.unpack_from(">H", data, hmtx + 4 * i)[0] for i in range(metric_count))
    return FontMetrics(
        units_per_em=upm,
        win_ascent=win_ascent,
        win_descent=win_descent,
        hhea_descent=abs(hhea_descent),
        cap_height=cap_height,
        advances=advances,
        cmap=_cmap(data, tables["cmap"]),
    )


def _tables(data: bytes) -> dict[str, int]:
    """Table tag -> offset for the tables this module reads; each must lie inside the file."""
    count = struct.unpack_from(">H", data, 4)[0]
    found: dict[str, int] = {}
    for i in range(count):
        raw_tag, _, offset, length = struct.unpack_from(">4sIII", data, 12 + 16 * i)
        tag = raw_tag.decode("latin-1")
        if offset + length > len(data):
            raise FontError(f"table {tag!r} lies outside the file")
        found[tag] = offset
    for needed in ("head", "hhea", "OS/2", "hmtx", "cmap"):
        if needed not in found:
            raise FontError(f"the font has no {needed!r} table")
    return found


def _cmap(data: bytes, table: int) -> dict[int, int]:
    """Code point -> glyph index from platform 3 / encoding 10 (format 12) or 1 (format 4)."""
    count = struct.unpack_from(">H", data, table + 2)[0]
    chosen: dict[tuple[int, int], int] = {}
    for i in range(count):
        platform, encoding, offset = struct.unpack_from(">HHI", data, table + 4 + 8 * i)
        chosen[(platform, encoding)] = table + offset
    if (3, 10) in chosen and struct.unpack_from(">H", data, chosen[(3, 10)])[0] == 12:
        return _cmap12(data, chosen[(3, 10)])
    if (3, 1) in chosen and struct.unpack_from(">H", data, chosen[(3, 1)])[0] == 4:
        return _cmap4(data, chosen[(3, 1)])
    raise FontError("the font has no Unicode character map (format 4 or 12)")


def _cmap4(data: bytes, off: int) -> dict[int, int]:
    segments = struct.unpack_from(">H", data, off + 6)[0] // 2
    ends = struct.unpack_from(f">{segments}H", data, off + 14)
    starts = struct.unpack_from(f">{segments}H", data, off + 16 + 2 * segments)
    deltas = struct.unpack_from(f">{segments}h", data, off + 16 + 4 * segments)
    range_at = off + 16 + 6 * segments
    ranges = struct.unpack_from(f">{segments}H", data, range_at)
    if sum(max(e - b + 1, 0) for b, e in zip(starts, ends, strict=True)) > MAX_CMAP_ENTRIES:
        raise FontError("the character map is too large")
    mapping: dict[int, int] = {}
    for i in range(segments):
        for code in range(starts[i], ends[i] + 1):
            if code == 0xFFFF:
                continue
            if ranges[i] == 0:
                glyph = (code + deltas[i]) & 0xFFFF
            else:
                at = range_at + 2 * i + ranges[i] + 2 * (code - starts[i])
                glyph = struct.unpack_from(">H", data, at)[0]
                if glyph:
                    glyph = (glyph + deltas[i]) & 0xFFFF
            if glyph:
                mapping[code] = glyph
    return mapping


def _cmap12(data: bytes, off: int) -> dict[int, int]:
    groups = struct.unpack_from(">I", data, off + 12)[0]
    mapping: dict[int, int] = {}
    for i in range(groups):
        start, end, glyph = struct.unpack_from(">III", data, off + 16 + 12 * i)
        if end < start or end > 0x10FFFF:
            raise FontError("the character map has an invalid range")
        for code in range(start, end + 1):
            mapping[code] = glyph + code - start
        if len(mapping) > MAX_CMAP_ENTRIES:
            raise FontError("the character map is too large")
    return mapping
