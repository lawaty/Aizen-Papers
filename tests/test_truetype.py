"""Tests for the TrueType reader, against the font this project actually embeds.

Two halves:

- the **committed subset**, read for real. These are the facts the PDF writer
  depends on — the four contextual forms of a letter are four *different* glyph
  ids, the lam-alef ligature exists, the font cannot draw Latin (which is why
  Helvetica is not a style choice but a requirement) — plus the coverage the
  subsetting command promised, asserted code point by code point where it matters.
- **hand-built fonts** for the paths the subset never takes: a ``cmap`` format 12
  subtable, the ``(3,10)``-before-``(3,1)`` preference, the monospaced tail of
  ``hmtx``, and every malformed-input error. Those branches exist in the reader
  and would otherwise be untested code that only runs when it is already broken.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from sender.infrastructure import arabic, truetype
from sender.infrastructure.truetype import TrueTypeFont

FONT = truetype.load_font()


# --- the committed subset --------------------------------------------------------


def test_the_font_is_the_committed_file() -> None:
    """The reader embeds *this* file, and returns it whole for FontFile2.

    ``font_bytes()`` is what goes into the PDF as ``/FontFile2``, and its length is
    the ``/Length1``; slicing it would produce a font a viewer rejects.
    """
    assert truetype.FONT_PATH.name == "NotoNaskhArabic-Regular.subset.ttf"
    assert truetype.FONT_PATH.parent.name == "fonts"
    assert truetype.FONT_PATH.is_file()
    assert FONT.font_bytes() == Path(truetype.FONT_PATH).read_bytes()
    assert len(FONT.font_bytes()) == 87368


def test_loading_is_cached() -> None:
    """One read per process: the PDF is rendered inside a poll loop."""
    assert truetype.load_font() is FONT
    assert truetype.load_font("/nonexistent/font.ttf") is FONT  # cache hit, no read


def test_the_vertical_metrics_match_the_font_header() -> None:
    """``head``/``hhea`` at the exact offsets the spec gives them.

    Pinning the numbers, not just the property names: an off-by-one in an offset
    yields a *plausible* wrong number (999 instead of 1000), and text drawn at the
    wrong scale is exactly the bug this reader exists to prevent.
    """
    assert FONT.units_per_em == 1000
    assert FONT.num_glyphs == 1011
    assert (FONT.ascender, FONT.descender, FONT.line_gap) == (1069, -634, 0)
    assert FONT.bbox == (-247, -590, 12176, 1405)


def test_every_form_a_letter_has_is_a_different_glyph() -> None:
    """The reason shaping matters, asserted on the font itself.

    If a letter's contextual forms mapped to one glyph, shaping would be cosmetic:
    the joins would be missing no matter how correct the shaper was. Right-joining
    letters (``د ذ ر ز و``) only have two forms — isolated and final — which is
    itself the shape of the Arabic script, so the invariant is "distinct", not
    "four".
    """
    for letter in "بتثجحخدذرزسشصضطظعغفقكلمنهوي":
        gids = [FONT.glyph_id(codepoint) for codepoint in arabic._FORMS_BY_BASE[ord(letter)].values()]
        assert None not in gids, f"U+{ord(letter):04X} is missing a form: {gids}"
        assert len(set(gids)) == len(gids), f"U+{ord(letter):04X} forms share a glyph: {gids}"
    assert len(arabic._FORMS_BY_BASE[ord("ب")]) == 4  # dual-joining: four forms
    assert len(arabic._FORMS_BY_BASE[ord("د")]) == 2  # right-joining: two
    # And the connecting forms are narrow, the standing-alone ones wide: a
    # beh-initial really is a lead-in stroke (275) and a beh-isolated a whole
    # letter (772).
    beh = arabic._FORMS_BY_BASE[ord("ب")]
    assert FONT.advance(FONT.glyph_id(beh["INITIAL"])) == 275
    assert FONT.advance(FONT.glyph_id(beh["ISOLATED"])) == 772


def test_the_lam_alef_ligatures_have_their_own_glyphs() -> None:
    """``لا`` must be one joined glyph, not a lam with an alef next to it."""
    for alef in "اآأإ":
        isolated, final = arabic._LAM_ALEF[ord(alef)]
        assert FONT.glyph_id(isolated) is not None
        assert FONT.glyph_id(final) is not None
        assert FONT.glyph_id(isolated) != FONT.glyph_id(final)
    assert (FONT.glyph_id(0xFEFB), FONT.glyph_id(0xFEFC)) == (879, 880)


def test_the_font_has_a_glyph_for_everything_the_shaper_can_emit() -> None:
    """The two halves of the feature agree.

    The shaper resolves a letter to a presentation form; the writer then asks the
    font for that code point's glyph. If the subset and the derived table ever
    disagree, this is the test that says so — every form the shaper can produce,
    and the tatweel it passes through, exists in the font.
    """
    missing = [
        f"U+{codepoint:04X}"
        for forms in arabic._FORMS_BY_BASE.values()
        for codepoint in forms.values()
        if FONT.glyph_id(codepoint) is None
    ]
    assert missing == []
    assert FONT.glyph_id(arabic._TATWEEL) is not None
    for isolated, final in arabic._LAM_ALEF.values():
        assert FONT.glyph_id(isolated) is not None and FONT.glyph_id(final) is not None


def test_the_subset_covers_the_arabic_blocks_it_promised() -> None:
    """The code point ranges the subsetting command asked for, as coverage.

    The Presentation Forms-B block is the point of the subset: it is where the
    shaper's output lives, so a single missing code point there is a blank space in
    an invoice. Every *assigned* code point in the three Arabic blocks is present.
    U+FE75, U+FEFD and U+FEFE are the three gaps in U+FE70–U+FEFF, and all three
    are unassigned in Unicode — there is no glyph for them in any font, and the
    shaper can never select them.
    """
    import unicodedata

    for start, end, count in ((0x0600, 0x0700, 256), (0xFB50, 0xFE00, 631), (0xFE70, 0xFF00, 141)):
        assigned = [cp for cp in range(start, end) if unicodedata.name(chr(cp), None)]
        assert len(assigned) == count, f"U+{start:04X} assigned count changed"
        missing = [cp for cp in assigned if FONT.glyph_id(cp) is None]
        assert missing == [], [f"U+{cp:04X}" for cp in missing]
    assert [cp for cp in range(0xFE70, 0xFF00) if FONT.glyph_id(cp) is None] == [
        0xFE75,
        0xFEFD,
        0xFEFE,
    ]
    for codepoint in (0xFE75, 0xFEFD, 0xFEFE):
        assert unicodedata.name(chr(codepoint), "") == ""  # unassigned in Unicode


def test_the_font_cannot_draw_latin_which_is_why_helvetica_is_used() -> None:
    """No A-Z in the subset: every Latin glyph in the PDF comes from /F1.

    Noto Naskh Arabic is an Arabic-only face. The subset carries the handful of
    ASCII characters the source happened to have (digits, space, a little
    punctuation) and nothing else, so a PDF that sent ``INV-001`` to the Arabic
    font would draw ``* ԱԱԱ`` or nothing at all.
    """
    assert all(FONT.glyph_id(cp) is None for cp in range(ord("A"), ord("Z") + 1))
    assert all(FONT.glyph_id(cp) is not None for cp in range(ord("0"), ord("9") + 1))
    assert FONT.glyph_id(0x20) is not None and FONT.glyph_id(0x2C) is not None
    # Outside the subset, cleanly absent: a Cyrillic letter and an emoji.
    assert FONT.glyph_id(0x0410) is None
    assert FONT.glyph_id(0x1F600) is None


def test_a_missing_glyph_is_none_and_asking_for_its_width_says_so() -> None:
    """``None`` is an answer, not a crash — and feeding it back is a loud error."""
    assert FONT.glyph_id(0x0410) is None
    with pytest.raises(ValueError, match="None means the font has no glyph"):
        FONT.advance(FONT.glyph_id(0x0410))
    with pytest.raises(ValueError, match="out of range"):
        FONT.advance(FONT.num_glyphs)
    with pytest.raises(ValueError, match="out of range"):
        FONT.advance(-1)


def test_every_mapped_glyph_has_a_usable_advance() -> None:
    """The advance widths the writer will actually use are sane.

    Zero is legal — 54 code points in this font are the Arabic combining marks and
    the letter mark, which sit on top of another glyph by design (and are stripped
    by the shaper anyway). The font's widest glyph is U+FDE5, a Quranic ligature
    12 ems across, which no invoice draws; what matters is that every
    *presentation form* — everything the shaper can emit — fits inside two ems, so a
    column of Arabic cannot overflow its box.
    """
    advances = [FONT.advance(gid) for gid in FONT._cmap.values() if gid < FONT.num_glyphs]
    assert advances
    assert min(advances) == 0
    form_advances = [
        FONT.advance(FONT.glyph_id(codepoint))
        for forms in arabic._FORMS_BY_BASE.values()
        for codepoint in forms.values()
    ]
    assert min(form_advances) == 210
    assert max(form_advances) == 1099
    assert max(form_advances) < 2 * FONT.units_per_em


def test_a_missing_font_file_is_reported_with_its_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one failure an operator has to act on names the file it could not open.

    The message has to survive to the log, because "the invoice PDF cannot draw
    Arabic without it" is the difference between a fixable bug report and a
    mystery. The cache is cleared so the read is actually attempted.
    """
    monkeypatch.setattr(truetype, "_font", None)
    with pytest.raises(ValueError, match="cannot read .*NotoNaskhArabic-Regular"):
        truetype.load_font("/nonexistent/NotoNaskhArabic-Regular.subset.ttf")


# --- hand-built fonts: the branches the subset never takes -----------------------


def _head(units_per_em: int = 1000) -> bytes:
    table = bytearray(54)
    struct.pack_into(">H", table, 18, units_per_em)
    struct.pack_into(">hhhh", table, 36, -100, -200, 900, 800)  # xMin yMin xMax yMax
    return bytes(table)


def _hhea(ascender: int, descender: int, line_gap: int, num_h_metrics: int) -> bytes:
    table = bytearray(36)
    struct.pack_into(">hhh", table, 4, ascender, descender, line_gap)
    struct.pack_into(">H", table, 34, num_h_metrics)
    return bytes(table)


def _maxp(num_glyphs: int) -> bytes:
    table = bytearray(6)
    struct.pack_into(">H", table, 4, num_glyphs)
    return bytes(table)


def _hmtx(advances: list[int]) -> bytes:
    return b"".join(struct.pack(">Hh", advance, 0) for advance in advances)


def _sfnt(tables: dict[bytes, bytes], version: int = 0x00010000) -> bytes:
    """Assemble an sfnt file from *tables*, in a readable table order."""
    directory_size = 12 + 16 * len(tables)
    out = bytearray(struct.pack(">IHHHH", version, len(tables), 0, 0, 0))
    body = bytearray()
    for tag, table in tables.items():
        out += struct.pack(">4sIII", tag, 0, directory_size + len(body), len(table))
        body += table
    assert len(out) == directory_size
    return bytes(out) + bytes(body)


def _cmap(subtables: dict[tuple[int, int], bytes]) -> bytes:
    """A ``cmap`` table from ``(platformID, encodingID) -> subtable bytes``."""
    header_length = 4 + 8 * len(subtables)
    out = bytearray(struct.pack(">HH", 0, len(subtables)))
    body = bytearray()
    for (platform, encoding), table in subtables.items():
        out += struct.pack(">HHI", platform, encoding, header_length + len(body))
        body += table
    return bytes(out) + bytes(body)


def _format4(segments: list[tuple[int, int, int]], glyph_ids: bytes = b"") -> bytes:
    """A format 4 subtable: one segment per ``(start, end, idDelta)``, all direct.

    The glyph id is ``(code point + idDelta) mod 65536`` — the delta is added to the
    *code point*, not to an index, so 0xFFC0 is what maps 0x41 to glyph 1. That is
    also why a delta near 0xFFFF wraps, and can land on glyph 0, the notdef.
    ``idRangeOffset`` is 0 everywhere here; the indirect form (a glyph index array
    at a byte offset) is exercised by the committed subset instead, 13 of whose
    segments use it.
    """
    seg_count = len(segments)
    seg_x2 = 2 * seg_count
    ends, starts, deltas = b"", b"", b""
    for start, end, delta in segments:
        ends += struct.pack(">H", end)
        starts += struct.pack(">H", start)
        deltas += struct.pack(">H", delta)
    body = (
        ends
        + struct.pack(">H", 0)  # reservedPad
        + starts
        + deltas
        + b"\x00\x00" * seg_count  # idRangeOffset
        + glyph_ids
    )
    header = struct.pack(">HHHHHHH", 4, 16 + len(body), 0, seg_x2, 0, 0, 0)
    return header + body


def _format12(groups: list[tuple[int, int, int]]) -> bytes:
    """A format 12 subtable: ``(first, last, firstGlyphId)`` ranges.

    The header is 16 bytes: format, reserved, length, language, nGroups.
    """
    body = b"".join(struct.pack(">III", *group) for group in groups)
    header = struct.pack(">HHIII", 12, 0, 16 + len(body), 0, len(groups))
    return header + body


def test_a_format12_subtable_is_read_and_preferred_over_format4() -> None:
    """(3,10) is tried before (3,1), and only one subtable is ever consulted.

    A font with both is the normal case for anything but an Arabic-only face; the
    committed subset has only format 4, so this path would otherwise be untested.
    The consequence of the preference is worth pinning: ``A`` is in the format 4
    subtable and comes back ``None``, because the format 12 subtable won and does
    not cover it. A reader that merged both would answer differently — and would be
    wrong to, since the two subtables are not required to agree.
    """
    font = TrueTypeFont(
        _sfnt(
            {
                b"head": _head(),
                b"hhea": _hhea(800, -200, 0, 2),
                b"hmtx": _hmtx([500, 600]),
                b"maxp": _maxp(3),
                b"cmap": _cmap(
                    {
                        (3, 1): _format4([(0x41, 0x42, 0xFFC0)]),  # A -> 1, B -> 2
                        (3, 10): _format12([(0x600, 0x603, 10)]),  # 0x600..3 -> 10..13
                    }
                ),
            }
        ),
        source="<synthetic format 12>",
    )
    assert [font.glyph_id(0x600 + offset) for offset in range(4)] == [10, 11, 12, 13]
    assert font.glyph_id(0x604) is None
    assert font.glyph_id(0x41) is None  # the (3,1) subtable is not consulted


def test_the_monospaced_tail_of_hmtx_repeats_the_last_advance() -> None:
    """``numberOfHMetrics`` < ``numGlyphs``: the rest repeat the final width.

    The format stores an advance per glyph only for the first ``numberOfHMetrics``
    of them; every later glyph has the same one. Reading past the table would be
    reading whatever the padding happens to be.
    """
    font = TrueTypeFont(
        _sfnt(
            {
                b"head": _head(),
                b"hhea": _hhea(800, -200, 0, 2),
                b"hmtx": _hmtx([500, 600]),
                b"maxp": _maxp(4),
                b"cmap": _cmap({(3, 1): _format4([(0x41, 0x43, 0xFFC0)])}),  # A->1, B->2, C->3
            }
        ),
        source="<synthetic hmtx tail>",
    )
    assert [font.advance(gid) for gid in range(4)] == [500, 600, 600, 600]
    assert font.advance(font.glyph_id(0x43)) == 600  # C is past numberOfHMetrics


def test_a_format4_delta_can_wrap_and_can_point_at_notdef() -> None:
    """``(code point + idDelta) mod 65536`` — and glyph 0 means *absent*.

    A delta that wraps the 16-bit space is normal in real fonts. Landing on glyph
    0 is how a font says "this code point is not here": mapping it to 0 would draw
    the notdef box, so the reader drops it and ``glyph_id`` answers ``None``.
    """
    font = TrueTypeFont(
        _sfnt(
            {
                b"head": _head(),
                b"hhea": _hhea(800, -200, 0, 1),
                b"hmtx": _hmtx([500]),
                b"maxp": _maxp(2),
                # 0x41 + 0xFFBF == 0x10000 -> wraps to 0 (notdef); 0x42 -> 1.
                b"cmap": _cmap({(0, 4): _format4([(0x41, 0x42, 0xFFBF)])}),
            }
        ),
        source="<synthetic delta wrap>",
    )
    assert font.glyph_id(0x41) is None  # notdef, not a glyph to draw
    assert font.glyph_id(0x42) == 1


def test_an_unsupported_cmap_format_is_named_in_the_error() -> None:
    format6 = struct.pack(">HHH", 6, 10, 0) + b"\x00" * 4
    with pytest.raises(ValueError, match="format 6 is not supported"):
        _font_with_cmap({(3, 10): format6})


def test_a_cmap_with_no_unicode_subtable_is_rejected() -> None:
    with pytest.raises(ValueError, match="no usable Unicode subtable"):
        _font_with_cmap({(3, 0): _format4([(0x41, 0x41, 1)])})


@pytest.mark.parametrize(
    "data, message",
    [
        (b"", "too short for an"),
        (struct.pack(">IHHHH", 0x00010000, 0, 0, 0, 0), "required table cmap is missing"),
        (b"\x00\x02\x00\x00" + b"\x00" * 20, "bad sfnt version"),
        (b"\x00\x01\x00\x00\x00\x01" + b"\x00" * 8, "header declares 1 tables"),
    ],
)
def test_malformed_files_raise_value_error_with_a_reason(data: bytes, message: str) -> None:
    """A truncated or wrong file is a ``ValueError`` that says why, never an index error."""
    with pytest.raises(ValueError, match=message):
        TrueTypeFont(data, source="<malformed>")


def test_a_table_claiming_bytes_past_the_end_of_the_file_is_refused() -> None:
    data = bytearray(_font_with_cmap_bytes({(3, 1): _format4([(0x41, 0x41, 1)])}))
    struct.pack_into(">I", data, 12 + 8, 0x7FFFFFFF)  # head's offset
    with pytest.raises(ValueError, match="past the end of the file"):
        TrueTypeFont(bytes(data), source="<overrun>")


def test_a_short_head_table_is_refused() -> None:
    with pytest.raises(ValueError, match="head is 12 bytes, need 54"):
        TrueTypeFont(
            _sfnt(
                {
                    b"head": b"\x00" * 12,
                    b"hhea": _hhea(800, -200, 0, 1),
                    b"hmtx": _hmtx([500]),
                    b"maxp": _maxp(2),
                    b"cmap": _cmap({(3, 1): _format4([(0x41, 0x41, 1)])}),
                }
            ),
            source="<short head>",
        )


def _font_with_cmap_bytes(subtables: dict[tuple[int, int], bytes]) -> bytes:
    return _sfnt(
        {
            b"head": _head(),
            b"hhea": _hhea(800, -200, 0, 1),
            b"hmtx": _hmtx([500]),
            b"maxp": _maxp(2),
            b"cmap": _cmap(subtables),
        }
    )


def _font_with_cmap(subtables: dict[tuple[int, int], bytes]) -> None:
    TrueTypeFont(_font_with_cmap_bytes(subtables), source="<synthetic cmap>")
