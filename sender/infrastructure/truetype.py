"""A minimal pure-Python TrueType reader: ``cmap`` and ``hmtx``, nothing else.

This exists because the invoice PDF has to draw Arabic (see
``docs/design.md`` #8) and a PDF viewer does **not** run a shaping engine: it maps
character codes to glyph ids itself, through a ``cmap`` the file declares. The
sender therefore has to know two things about the font *at generation time* — which
glyph id a code point is, and how wide that glyph is — and it has to know them
without adding a font library to a two-dependency project.

So this reads the five tables that answer those questions and deliberately nothing
more. There is no ``glyf``/``loca`` parsing, no outline extraction, no hinting
interpretation: the glyph *outlines* are not needed to place text, only
``unitsPerEm``, the vertical metrics, the bounding box, the character map and the
advance widths. That keeps the reader small enough to audit by eye (a PDF writer you
cannot read is a PDF writer you cannot trust) and dependency-free by construction:
``struct`` and nothing else.

Deliberate omissions:

- the ``name`` table is never parsed. The PDF names the font with a fixed
  ``/BaseFont`` string, so there is no reason to read a string table here;
- ``head.indexToLocFormat``/``loca`` are ignored for the same reason;
- hinting, ``GSUB``/``GPOS`` and vertical metrics are not read. Nothing in this
  project shapes, and the shaper in :mod:`sender.infrastructure.arabic` already
  resolved every contextual form before asking for a glyph.

Every malformed input raises :class:`ValueError` with a message naming the table
and the offset, because a wrong width or a wrong glyph id produces a PDF that
*looks* fine and is subtly wrong — a loud failure at load time is the only cheap
way to notice.
"""

from __future__ import annotations

import struct
from pathlib import Path

#: The font this project embeds. A subset of Noto Naskh Arabic (SIL OFL 1.1) —
#: see ``fonts/README.md`` for how it was produced and why it is a subset.
FONT_PATH = Path(__file__).with_name("fonts") / "NotoNaskhArabic-Regular.subset.ttf"

#: sfnt version numbers we accept. ``\x00\x01\x00\x00`` is a TrueType outline
#: font, ``OTTO`` is a CFF one; both carry the same ``head``/``hhea``/``hmtx``/
#: ``maxp``/``cmap`` tables, so the text tables are readable either way.
_TTF_SIG = 0x00010000
_OTTO_SIG = 0x4F54544F

#: cmap subtable preference: (platformID, encodingID) in the order we try them.
#: (3,10) is Windows UCS-4, (3,1) Windows BMP, (0,x) Unicode. Preference matters
#: because the two families disagree on characters outside the BMP more than a
#: reader would expect, and a full-range table is the one with no holes.
_CMAP_PREFERENCE = ((3, 10), (3, 1), (0, 4), (0, 3), (0, 2), (0, 1), (0, 0))


class TrueTypeFont:
    """The text half of a ``sfnt`` file: character map, advances and metrics.

    Attributes are read once in :meth:`__init__` and never change, so they are
    plain properties rather than lookups on every call — the PDF writer resolves
    one glyph per drawn character and reads its width right after.
    """

    def __init__(self, data: bytes, source: str = "<bytes>") -> None:
        self._data = data
        self._source = source
        tables = _table_directory(data, source)
        for required in (b"cmap", b"head", b"hhea", b"hmtx", b"maxp"):
            if required not in tables:
                raise ValueError(
                    f"TrueType font {source}: required table "
                    f"{required.decode('ascii')} is missing"
                )
        self._read_head(_slice(data, tables[b"head"], source))
        self._read_hhea_hmtx(
            _slice(data, tables[b"hhea"], source), _slice(data, tables[b"hmtx"], source)
        )
        self._read_maxp(_slice(data, tables[b"maxp"], source))
        self._cmap = _read_cmap(data, tables[b"cmap"], source)

    # --- public API ----------------------------------------------------------------

    def glyph_id(self, codepoint: int) -> int | None:
        """The glyph id for *codepoint*, or ``None`` when the font has no glyph.

        ``None`` is a real answer, not an error: a subset legitimately omits code
        points, and the caller has a documented fallback for each of them.
        """
        return self._cmap.get(codepoint)

    def advance(self, gid: int) -> int:
        """The advance width of glyph *gid* in font units (see :attr:`units_per_em`).

        Glyphs at or past ``numberOfHMetrics`` are the monospaced tail of the font
        and repeat the last advance, which is what the format means by not
        storing them.

        ``gid`` must be a glyph id. ``None`` is rejected with a ``ValueError``
        rather than a ``TypeError``, because passing one here is nearly always the
        result of asking for a code point the font has no glyph for — that is, a
        subset that does not cover the text it was given.
        """
        if gid is None or gid < 0 or gid >= self._num_glyphs:
            raise ValueError(
                f"TrueType font {self._source}: glyph id {gid!r} is out of range "
                f"(0..{self._num_glyphs - 1}); None means the font has no glyph for "
                "that code point"
            )
        index = min(gid, self._num_h_metrics - 1)
        return self._advances[index]

    @property
    def units_per_em(self) -> int:
        """Font units per em — the divisor that turns an advance into points."""
        return self._units_per_em

    @property
    def ascender(self) -> int:
        return self._ascender

    @property
    def descender(self) -> int:
        return self._descender

    @property
    def line_gap(self) -> int:
        return self._line_gap

    @property
    def num_glyphs(self) -> int:
        return self._num_glyphs

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        """``(xMin, yMin, xMax, yMax)`` in font units, straight from ``head``."""
        return self._bbox

    def font_bytes(self) -> bytes:
        """The raw file, for embedding as a ``FontFile2`` stream.

        Deliberately the *whole* file: the PDF's ``/Length1`` is its uncompressed
        length, and slicing a subset out of the table directory would only
        produce a file a viewer rejects.
        """
        return self._data

    # --- table readers -------------------------------------------------------------
    #
    # These read a *table slice*, so every offset below is an offset inside that
    # table rather than inside the file. That is the natural unit for the format
    # (the spec writes them that way) and it means a bad offset is reported as a
    # bad offset in a known table instead of silently landing in a neighbour.

    def _read_head(self, table: bytes) -> None:
        # head is 54 bytes: version(4) fontRevision(4) checkSumAdjustment(4)
        # magicNumber(4) flags(2) unitsPerEm(2) created(8) modified(8) xMin(2)
        # yMin(2) xMax(2) yMax(2) ...
        if len(table) < 54:
            raise ValueError(
                f"TrueType font {self._source}: head is {len(table)} bytes, need 54"
            )
        self._units_per_em = _u16(table, 18, "head.unitsPerEm")
        if self._units_per_em <= 0:
            raise ValueError(
                f"TrueType font {self._source}: head.unitsPerEm is {self._units_per_em}"
            )
        self._bbox = (
            _i16(table, 36, "head.xMin"),
            _i16(table, 38, "head.yMin"),
            _i16(table, 40, "head.xMax"),
            _i16(table, 42, "head.yMax"),
        )

    def _read_hhea_hmtx(self, hhea: bytes, hmtx: bytes) -> None:
        # hhea is 36 bytes: version(4) ascender(2) descender(2) lineGap(2)
        # ... numberOfHMetrics(2) ...
        if len(hhea) < 36:
            raise ValueError(
                f"TrueType font {self._source}: hhea is {len(hhea)} bytes, need 36"
            )
        self._ascender = _i16(hhea, 4, "hhea.ascender")
        self._descender = _i16(hhea, 6, "hhea.descender")
        self._line_gap = _i16(hhea, 8, "hhea.lineGap")
        self._num_h_metrics = _u16(hhea, 34, "hhea.numberOfHMetrics")
        if self._num_h_metrics == 0:
            raise ValueError(
                f"TrueType font {self._source}: hhea.numberOfHMetrics is 0, so no "
                "glyph has an advance width"
            )
        need = 4 * self._num_h_metrics
        if len(hmtx) < need:
            raise ValueError(
                f"TrueType font {self._source}: hmtx is {len(hmtx)} bytes but "
                f"numberOfHMetrics {self._num_h_metrics} needs {need}"
            )
        # Only the first u16 of each 4-byte record is an advance; the i16 after it
        # is the left side bearing, which positioning does not need.
        self._advances = [
            _u16(hmtx, 4 * index, "hmtx.advanceWidth") for index in range(self._num_h_metrics)
        ]

    def _read_maxp(self, maxp: bytes) -> None:
        if len(maxp) < 6:
            raise ValueError(
                f"TrueType font {self._source}: maxp is {len(maxp)} bytes, need 6"
            )
        self._num_glyphs = _u16(maxp, 4, "maxp.numGlyphs")
        if self._num_glyphs == 0:
            raise ValueError(f"TrueType font {self._source}: maxp.numGlyphs is 0")
        if self._num_h_metrics > self._num_glyphs:
            raise ValueError(
                f"TrueType font {self._source}: numberOfHMetrics "
                f"{self._num_h_metrics} exceeds numGlyphs {self._num_glyphs}"
            )


# --- module-level font access -----------------------------------------------------


_font: TrueTypeFont | None = None


def load_font(path: Path | str = FONT_PATH) -> TrueTypeFont:
    """The embedded font, read once per process and cached.

    The PDF is rendered inside a poll loop, so this is read once and reused; the
    cache is a module-level singleton because the file never changes while the
    process runs.
    """
    global _font
    if _font is None:
        path = Path(path)
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"TrueType font: cannot read {path}: {exc}; the invoice PDF cannot "
                "draw Arabic without it"
            ) from exc
        _font = TrueTypeFont(data, source=str(path))
    return _font


# --- binary helpers ---------------------------------------------------------------


def _table_directory(data: bytes, source: str) -> dict[bytes, tuple[int, int]]:
    """``tag -> (offset, length)`` from the sfnt header.

    Offsets are *not* validated against the file length here; a truncated table is
    caught by the reader that touches it, which can name the table in the message.
    """
    if len(data) < 12:
        raise ValueError(
            f"TrueType font {source}: file is {len(data)} bytes, too short for an "
            "sfnt header"
        )
    version, num_tables = struct.unpack(">IH", data[:6])
    if version not in (_TTF_SIG, _OTTO_SIG):
        raise ValueError(
            f"TrueType font {source}: bad sfnt version 0x{version:08X} (expected "
            "0x00010000 or OTTO)"
        )
    end = 12 + 16 * num_tables
    if len(data) < end:
        raise ValueError(
            f"TrueType font {source}: header declares {num_tables} tables "
            f"({end} bytes) but the file is {len(data)} bytes"
        )
    tables: dict[bytes, tuple[int, int]] = {}
    for index in range(num_tables):
        base = 12 + 16 * index
        tag, _, offset, length = struct.unpack(">4sIII", data[base : base + 16])
        if offset + length > len(data):
            raise ValueError(
                f"TrueType font {source}: table {tag.decode('latin-1', 'replace')} "
                f"claims {length} bytes at {offset}, past the end of the file"
            )
        tables[tag] = (offset, length)
    return tables


def _slice(data: bytes, table: tuple[int, int], source: str) -> bytes:
    """The bytes of one table, so the readers below can index it from zero."""
    offset, length = table
    return data[offset : offset + length]


def _read_cmap(data: bytes, table: tuple[int, int], source: str) -> dict[int, int]:
    """The best available ``cmap`` subtable as a ``codepoint -> gid`` dict.

    A full dict (rather than the subtable's own lookup logic) because the caller
    asks about a handful of code points per invoice and the whole Arabic block is
    only a few hundred entries.
    """
    offset, length = table
    if length < 4:
        raise ValueError(f"TrueType font {source}: cmap is {length} bytes, need 4")
    count = _u16(data, offset + 2, "cmap.numTables")
    end = offset + 4 + 8 * count
    if end > offset + length:
        raise ValueError(
            f"TrueType font {source}: cmap declares {count} subtables but is only "
            f"{length} bytes"
        )
    subtables: dict[tuple[int, int], tuple[int, int]] = {}
    for index in range(count):
        base = offset + 4 + 8 * index
        platform, encoding, sub_offset = struct.unpack(">HHI", data[base : base + 8])
        subtables.setdefault((platform, encoding), (offset + sub_offset, 0))
    for platform, encoding in _CMAP_PREFERENCE:
        if (platform, encoding) in subtables:
            return _parse_cmap_subtable(data, subtables[(platform, encoding)], source)
    raise ValueError(
        f"TrueType font {source}: cmap has no usable Unicode subtable, only "
        + ", ".join(f"({p},{e})" for p, e in sorted(subtables))
    )


def _parse_cmap_subtable(
    data: bytes, location: tuple[int, int], source: str
) -> dict[int, int]:
    offset = location[0]
    fmt = _u16(data, offset, "cmap.subtable.format")
    if fmt == 4:
        return _parse_format4(data, offset, source)
    if fmt == 12:
        return _parse_format12(data, offset, source)
    raise ValueError(
        f"TrueType font {source}: cmap subtable format {fmt} is not supported "
        "(only 4 and 12)"
    )


def _parse_format4(data: bytes, offset: int, source: str) -> dict[int, int]:
    """Format 4: a sorted list of ``segment -> contiguous glyph run`` records.

    Segments are kept as runs rather than expanded one code point at a time
    because the format is built to be *not* expanded: a segment may legally be
    sparse (``idDelta`` plus a glyph id array), and only ``idRangeOffset`` entries
    that say so need per-code-point work.
    """
    seg_x2 = _u16(data, offset + 6, "cmap.format4.segCountX2")
    if seg_x2 == 0 or seg_x2 % 2:
        raise ValueError(
            f"TrueType font {source}: cmap format 4 segCountX2 is {seg_x2}"
        )
    seg_count = seg_x2 // 2
    ends = offset + 14
    starts = ends + seg_x2 + 2
    deltas = starts + seg_x2
    ranges = deltas + seg_x2
    need = ranges + seg_x2
    if need > len(data):
        raise ValueError(
            f"TrueType font {source}: cmap format 4 needs {need - offset} bytes, "
            f"only {len(data)} available"
        )
    mapping: dict[int, int] = {}
    for index in range(seg_count):
        end = _u16(data, ends + 2 * index, "cmap.format4.endCode")
        start = _u16(data, starts + 2 * index, "cmap.format4.startCode")
        if start > end:
            continue  # a reversed segment is a corrupt table, not a crash
        delta = _u16(data, deltas + 2 * index, "cmap.format4.idDelta")
        range_offset = _u16(data, ranges + 2 * index, "cmap.format4.idRangeOffset")
        for codepoint in range(start, end + 1):
            if range_offset == 0:
                gid = (codepoint + delta) & 0xFFFF
            else:
                # idRangeOffset is a byte offset *from its own slot* to the glyph
                # index array, which is the one genuinely indirect step in the
                # format.
                slot = ranges + 2 * index
                position = slot + range_offset + 2 * (codepoint - start)
                if position + 2 > len(data):
                    continue
                gid = _u16(data, position, "cmap.format4.glyphIdArray")
                if gid:
                    gid = (gid + delta) & 0xFFFF
            if gid:
                mapping[codepoint] = gid
    return mapping


def _parse_format12(data: bytes, offset: int, source: str) -> dict[int, int]:
    """Format 12: groups of ``(start, end, startGlyphId)`` covering whole ranges."""
    if offset + 16 > len(data):
        raise ValueError(
            f"TrueType font {source}: cmap format 12 header needs 16 bytes at "
            f"{offset}, only {len(data)} available"
        )
    group_count = struct.unpack(">I", data[offset + 12 : offset + 16])[0]
    base = offset + 16
    if base + 12 * group_count > len(data):
        raise ValueError(
            f"TrueType font {source}: cmap format 12 declares {group_count} groups, "
            "more than the file holds"
        )
    mapping: dict[int, int] = {}
    for index in range(group_count):
        start, end, start_gid = struct.unpack(">III", data[base + 12 * index : base + 12 * index + 12])
        if start > end:
            continue
        for codepoint in range(start, end + 1):
            mapping[codepoint] = start_gid + (codepoint - start)
    return mapping


def _u16(data: bytes, offset: int, what: str) -> int:
    if offset + 2 > len(data):
        raise ValueError(
            f"TrueType font: {what} reads 2 bytes at {offset}, past the end of the "
            f"data ({len(data)} bytes)"
        )
    return struct.unpack(">H", data[offset : offset + 2])[0]


def _i16(data: bytes, offset: int, what: str) -> int:
    if offset + 2 > len(data):
        raise ValueError(
            f"TrueType font: {what} reads 2 bytes at {offset}, past the end of the "
            f"data ({len(data)} bytes)"
        )
    return struct.unpack(">h", data[offset : offset + 2])[0]
