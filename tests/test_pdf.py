"""Structural tests for the dependency-free invoice PDF writer.

These assert the container is well-formed — the parts a reader actually needs
(xref offsets, page tree, stream lengths) — plus the text-honesty rules, and for
Arabic the part a customer actually sees: that a contextual form reached the
content stream as a glyph id, in the right order, drawn inside the margins.

They deliberately do not depend on a PDF library: the project's promise is a
two-dependency footprint, so a test suite that needed one to check the PDF would
contradict the reason the writer exists. Glyph ids are read from the stream and
their widths computed from the embedded font, using the same reader the writer
uses, which is the only independent measure of "does this fit" available here.
"""

from __future__ import annotations

import re
import struct
import zlib
from datetime import date
from decimal import Decimal

import pytest

from sender.domain.models import Invoice, InvoiceItem
from sender.infrastructure import arabic, truetype
from sender.infrastructure.pdf import render_invoice_pdf

FONT = truetype.load_font()

#: One drawn piece of text: ``/F2 9 Tf 1 0 0 1 479.05 645.00 Tm <...> Tj``.
#: The literal alternative is matched greedily so an escaped ``)`` inside a string
#: literal does not end the match early.
SPAN = re.compile(
    r"/(?P<font>F\d) (?P<size>[\d.]+) Tf 1 0 0 1 (?P<x>[\d.]+) (?P<y>[\d.]+) Tm "
    r"(?:<(?P<gids>[0-9A-F]*)>|\((?P<literal>.*)\)) Tj"
)

#: The page margins the layout is built from, restated here so a change to the
#: writer's constants has to be made deliberately in two places.
LEFT_MARGIN = 56.0
RIGHT_MARGIN = 595.0 - 56.0

#: The brand-header geometry the tests pin, restated from the writer's constants
#: so a change has to be made deliberately in two places — the same contract as
#: the margins above.
LOGO_SIZE = 108.0
LOGO_TOP = 783.0  # the logo's top edge: 3pt inside the top margin
BRAND_END = 415.0  # the brand name's right edge: the logo's left edge less its gap
TITLE_END = 246.0  # the title zone's right edge
META_Y = 639.0  # the meta rows' first baseline: below the header's closing rule

#: Helvetica's advance widths per 1000 em, for the characters these fixtures can
#: actually put in a string: digits, a decimal point, a thousands separator, and
#: the letters the brand name and currency code use. From the standard Helvetica
#: AFM. Deliberately not the writer's table — a character missing here makes a test
#: fail, which is the right outcome for a fixture that has grown. Upper and lower
#: case are separate entries because they have separate widths.
_HELVETICA_EM = {str(digit): 556 for digit in range(10)}
_HELVETICA_EM.update({".": 278, ",": 278, " ": 278, "-": 333, "/": 278})
# The letters, from the AFM: those of the currency code, of the "INV" prefix the
# invoice numbers here use, and of the "Aizen Paper" wordmark.
for _letter, _width in zip("EGPINV", (667, 778, 667, 278, 722, 667)):
    _HELVETICA_EM[_letter] = _width
# "Aizen Paper": A 667, i 222, z 500, e 556, n 556, space 278, P 667, p 556,
# a 556, e 556, r 333. From the same AFM, checked against the writer's own table.
for _letter, _width in zip("AizenPaper", (667, 222, 500, 556, 556, 667, 556, 556, 556, 333)):
    _HELVETICA_EM[_letter] = _width


def latin_width(text: str, size: float) -> float:
    """*text*'s width in Helvetica at *size*, in points."""
    return sum(_HELVETICA_EM[char] for char in text) / 1000.0 * size


def make_invoice(**overrides) -> Invoice:
    base = dict(
        id="42",
        number="INV-042",
        currency="USD",
        issue_date=date(2026, 3, 14),
        customer_name="Acme Trading",
        public_url="https://example.test/invoices/preview/42",
        pdf_url="https://example.test/invoices/42.pdf",
        subtotal=Decimal("100.00"),
        total=Decimal("120.00"),
        total_paid=Decimal("20.00"),
        balance_due=Decimal("100.00"),
        items=(
            InvoiceItem(
                name="Widget",
                quantity=Decimal("2"),
                unit_price=Decimal("50.00"),
                total=Decimal("100.00"),
            ),
        ),
    )
    base.update(overrides)
    return Invoice(**base)


def arabic_invoice(**overrides) -> Invoice:
    """An invoice with an Arabic customer and Arabic line items."""
    base = dict(
        customer_name="شركة النور للديكور",
        currency="EGP",
        items=(
            InvoiceItem(
                name="ورق حائط كلاسيك",
                quantity=Decimal("2"),
                unit_price=Decimal("50.00"),
                total=Decimal("100.00"),
            ),
        ),
    )
    base.update(overrides)
    return make_invoice(**base)


def stream_text(pdf: bytes) -> str:
    """The raw bytes of every stream, as latin-1 text."""
    return "\n".join(
        match.group(1).decode("latin-1") for match in re.finditer(rb"stream\n(.*?)endstream", pdf, re.S)
    )


def page_streams(pdf: bytes) -> list[str]:
    """Every page's content stream, in page order.

    The other streams in the file (the embedded font, the ToUnicode CMap) are
    found by the same filter: only a content stream has text operators in it.
    """
    return [
        body.decode("latin-1")
        for body in re.findall(rb"stream\n(.*?)endstream", pdf, re.S)
        if b" Tf " in body
    ]


def page_text(pdf: bytes) -> str:
    """The first page's content stream — the one with the text operators."""
    return page_streams(pdf)[0]


#: One filled rectangle: the colour, then the box ``x y w h re f``. A page's
#: fills are the chips, the header band and the row stripes, and all of them are
#: painted with a plain ``rg`` fill and nothing else.
RECT = re.compile(
    r"^(?P<rgb>[\d.]+ [\d.]+ [\d.]+) rg\n"
    r"(?P<x>[\d.-]+) (?P<y>[\d.-]+) (?P<w>[\d.-]+) (?P<h>[\d.-]+) re f$",
    re.M,
)

#: A money-shaped literal: thousands separators, one period, always two decimals.
#: ``_money`` is the only thing that produces one, which is what makes these the
#: figures the decimal axis has to line up.
MONEY = re.compile(r"^\d[\d,]*\.\d{2}$")


def page_count(pdf: bytes) -> int:
    return int(re.search(rb"/Count (\d+)", pdf).group(1))


def spans(pdf: bytes) -> list[dict[str, str]]:
    return [match.groupdict() for match in SPAN.finditer(page_text(pdf))]


def rows(pdf: bytes) -> dict[float, list[dict[str, str]]]:
    """Drawn spans grouped by their baseline, top line first."""
    grouped: dict[float, list[dict[str, str]]] = {}
    for span in spans(pdf):
        grouped.setdefault(float(span["y"]), []).append(span)
    return dict(sorted(grouped.items(), reverse=True))


def glyph_ids(hex_string: str) -> list[int]:
    return [int(hex_string[index : index + 4], 16) for index in range(0, len(hex_string), 4)]


def arabic_width(hex_string: str, size: float) -> float:
    """The drawn width of an F2 span, from the font's own advance widths."""
    total = sum(FONT.advance(gid) for gid in glyph_ids(hex_string))
    return total / FONT.units_per_em * size


def _ops(stream: str) -> list[str]:
    """The page's operators, one entry per self-contained ``q … Q`` group.

    Every op the writer emits opens with ``q`` and closes with ``Q``, so splitting on
    the opening one is the whole parse: what comes back is each op's body followed
    by its own ``Q``.
    """
    return [op for op in stream.split("q\n") if op.strip()]


def _span_box(span: dict[str, str]) -> tuple[float, float, float, float]:
    """The ink box of a span as ``(left, bottom, right, top)``, in points.

    Generous on purpose: a line of text is treated as reaching a quarter of its size
    below the baseline and three quarters above it, so an overlap test built on this
    counts more overlaps than really happen and can only ever be too strict.
    """
    size = float(span["size"])
    left = float(span["x"])
    width = (
        arabic_width(span["gids"], size)
        if span["gids"]
        else latin_width(span["literal"], size)
    )
    y = float(span["y"])
    return (left, y - 0.25 * size, left + width, y + 0.75 * size)


def _overlaps(one: tuple[float, float, float, float], other: tuple[float, float, float, float]) -> bool:
    return one[0] < other[2] and one[2] > other[0] and one[1] < other[3] and one[3] > other[1]


def _figure_rows(pdf: bytes) -> list[tuple[dict[str, str], ...]]:
    """Every drawn item row's three figures, by the column each one lands in.

    Classified by the right edge each figure lands on rather than by counting the
    spans in a row, so the header cells (Arabic labels and a currency), the meta
    rows and the totals (one figure per line) cannot be mistaken for an item row.
    The edges themselves are what the columns were measured to: 239 is the price
    column's right edge, 154 the line total's, and anything to the right of 239 and
    no further than 289 — the quantity column's — is a quantity.
    """
    found: list[tuple[dict[str, str], ...]] = []
    for row in rows(pdf).values():
        cells: dict[str, dict[str, str]] = {}
        for span in row:
            if not span["literal"]:
                continue
            end = float(span["x"]) + latin_width(span["literal"], float(span["size"]))
            # if/elif, not two tests: a drawn x is rounded to two decimals, so a
            # figure sitting exactly on a column's edge can be a hair inside the
            # band below it and would otherwise be read as two figures.
            if abs(end - 239.0) < 0.01:
                cells["price"] = span
            elif abs(end - 154.0) < 0.01:
                cells["total"] = span
            elif 239.0 < end <= 289.0:
                cells["qty"] = span
        if len(cells) == 3:
            found.append((cells["qty"], cells["price"], cells["total"]))
    return found


def shaped_gids(value: str) -> str:
    """The glyph ids of the first RTL run the writer emits for *value*.

    Derived through the real pipeline — shape, then reorder, then the embedded
    font's ``cmap`` — so this is the end-to-end statement of what the content
    stream has to contain for a given string. Values that mix scripts (a label with
    a currency suffix, an item name with digits) draw as several runs, so pass the
    whole string and take the Arabic one.
    """

    runs = [run for run, is_rtl in arabic.reorder(arabic.shape(value)) if is_rtl]
    assert runs, f"{value!r} has no right-to-left run"
    return "".join(f"{FONT.glyph_id(ord(char)):04X}" for char in runs[0])


def xref_offsets(pdf: bytes) -> dict[int, int]:
    """Object number -> byte offset, read out of the xref table."""
    table = pdf.split(b"xref\n", 1)[1].split(b"trailer", 1)[0].decode("ascii")
    lines = table.splitlines()
    count = int(lines[0].split()[1])
    offsets: dict[int, int] = {}
    for number, line in enumerate(lines[1 : count + 1], start=0):
        # Entries are "<offset> <gen> n " — a trailing space is part of the spec.
        if line.split()[2:3] == ["n"]:
            offsets[number] = int(line.split()[0])
    return offsets


def object(pdf: bytes, number: int) -> str:
    """One indirect object's dictionary and body, as latin-1 text."""
    match = re.search(
        rb"%d 0 obj\n(.*?)\nendobj" % number, pdf, re.S
    )
    assert match, f"object {number} not found"
    return match.group(1).decode("latin-1")


# --- container -------------------------------------------------------------------


def test_pdf_has_the_expected_header_and_trailer() -> None:
    pdf = render_invoice_pdf(make_invoice())
    assert pdf.startswith(b"%PDF-1.4")
    assert pdf.rstrip().endswith(b"%%EOF")


def test_xref_offsets_point_at_the_real_objects() -> None:
    """Every xref entry must address the byte offset of ``N 0 obj``.

    A wrong offset is the classic hand-rolled-PDF bug: readers silently
    reconstruct the file instead of failing, so it has to be asserted directly.
    """
    pdf = render_invoice_pdf(make_invoice())
    for number, offset in xref_offsets(pdf).items():
        assert pdf[offset : offset + len(f"{number} 0 obj")] == f"{number} 0 obj".encode()


def test_startxref_points_at_the_xref_table() -> None:
    pdf = render_invoice_pdf(make_invoice())
    start = int(pdf.rsplit(b"startxref\n", 1)[1].split(b"%%EOF")[0].strip())
    assert pdf[start : start + 4] == b"xref"


def test_trailer_size_covers_every_object() -> None:
    """``/Size`` is one past the highest object number; a mismatch makes readers
    report "xref num N not found"."""
    pdf = render_invoice_pdf(make_invoice())
    size = int(re.search(rb"/Size (\d+)", pdf).group(1))
    assert max(xref_offsets(pdf)) == size - 1
    assert pdf.count(b" 0 obj\n") == size - 1


def test_the_font_objects_keep_their_documented_numbers() -> None:
    """Objects 3-8 are the two fonts, and the page's resources name them.

    The numbering is a promise in the design notes: ``/F1`` is the Latin font at
    object 3, the Arabic Type0 at 4, and the rest of the Arabic font's objects
    follow in the order a reader needs them (CIDFont, descriptor, the embedded
    file, the ToUnicode CMap). The logo's two image XObjects follow at 9 and 10
    (asserted by the logo tests below), and the pages start at 11.
    """
    pdf = render_invoice_pdf(arabic_invoice())
    page = object(pdf, 11)  # one page: the first page object after the fonts and the logo
    assert "/F1 3 0 R" in page and "/F2 4 0 R" in page
    assert "/BaseFont /Helvetica" in object(pdf, 3)
    assert "/Encoding /WinAnsiEncoding" in object(pdf, 3)
    assert "/Subtype /Type0" in object(pdf, 4)
    assert "NotoNaskhArabic-Subset" in object(pdf, 4)
    assert "/DescendantFonts [5 0 R]" in object(pdf, 4)
    assert "/ToUnicode 8 0 R" in object(pdf, 4)
    assert "/Subtype /CIDFontType2" in object(pdf, 5)
    assert "/FontDescriptor 6 0 R" in object(pdf, 5)
    assert "/Type /FontDescriptor" in object(pdf, 6)
    assert "/FontFile2 7 0 R" in object(pdf, 6)
    assert "/Length1 " in object(pdf, 7)
    assert "beginbfchar" in object(pdf, 8)


def test_the_arabic_font_is_identity_h_with_a_direct_gid_mapping() -> None:
    """``Identity-H`` plus ``/CIDToGIDMap /Identity`` means the ids are the ids.

    With anything else — a CMap that maps codes to glyph names, a ``/D`` offset, a
    non-identity CIDToGIDMap — the glyph ids the writer computed would be looked up
    a second time by the viewer and the joins would come out wrong. This is the
    whole reason the shaper runs before the bytes are written.
    """
    pdf = render_invoice_pdf(arabic_invoice())
    assert "/Encoding /Identity-H" in object(pdf, 4)
    assert "/CIDToGIDMap /Identity" in object(pdf, 5)
    assert "/Subtype /Type0" in object(pdf, 4)


def test_the_base_font_name_carries_a_subset_tag() -> None:
    """Six uppercase letters and a plus, as a viewer expects of a subset.

    The name is fixed rather than read out of the font's ``name`` table — see the
    reader's docstring — and the tag is derived from the embedded bytes, so the
    same input always produces the same file.
    """
    pdf = render_invoice_pdf(arabic_invoice())
    base_font = re.search(r"/BaseFont /([A-Z]{6}\+)?(\S+)", object(pdf, 4))
    assert base_font, object(pdf, 4)
    tag, name = base_font.groups()
    assert tag, "a subset font must carry a six-letter tag"
    assert name == "NotoNaskhArabic-Subset"
    assert render_invoice_pdf(arabic_invoice()) == pdf  # deterministic


def test_only_the_used_glyph_widths_are_in_the_widths_array() -> None:
    """``/W`` covers the glyphs that were drawn, grouped into consecutive runs.

    A ``/W`` entry for a glyph the file never shows is dead weight in every
    invoice; an entry with a wrong width shifts everything drawn after it. The
    array is read between ``/W [`` and the ``/CIDToGIDMap`` that ends it, because
    the groups contain brackets of their own.
    """
    pdf = render_invoice_pdf(arabic_invoice())
    descendant = object(pdf, 5)
    body = descendant.split("/W [", 1)[1].split("] /CIDToGIDMap", 1)[0]
    emitted = {gid for span in spans(pdf) for gid in glyph_ids(span["gids"] or "")}

    listed: dict[int, int] = {}
    for first, values in re.findall(r"(\d+) \[([^\]]*)\]", body):
        for offset, width in enumerate(values.split()):
            listed[int(first) + offset] = int(width)
    # Consecutive glyphs with the same width may also be written as "first last w".
    for first, last, width in re.findall(r"(\d+) (\d+) (\d+)(?! \[)", body):
        for gid in range(int(first), int(last) + 1):
            listed[gid] = int(width)
    assert set(listed) == emitted
    for gid, width in listed.items():
        assert width == FONT.advance(gid), f"glyph {gid} has the wrong advance"
    # The default width is glyph 0's, the .notdef: it is what a viewer uses for a
    # glyph with no /W entry, and it should never be a space's width pretending to
    # be one.
    assert f"/DW {FONT.advance(0)}" in descendant


def test_the_embedded_font_is_the_committed_subset() -> None:
    """``/Length1`` is the uncompressed length, and it is the file on disk."""
    pdf = render_invoice_pdf(arabic_invoice())
    font_file = object(pdf, 7)
    length1 = int(re.search(r"/Length1 (\d+)", font_file).group(1))
    assert length1 == len(FONT.font_bytes()) == 87368
    # The descriptor's metrics come from the same font.
    descriptor = object(pdf, 6)
    assert f"/Ascent {FONT.ascender}" in descriptor
    assert f"/Descent {FONT.descender}" in descriptor
    assert f"/FontBBox [{FONT.bbox[0]} {FONT.bbox[1]} {FONT.bbox[2]} {FONT.bbox[3]}]" in descriptor


def test_the_tounicode_map_covers_every_glyph_that_was_drawn() -> None:
    """Copy/paste and search have to work: text extraction reads this CMap.

    One ``bfchar`` per drawn glyph, each mapping the used glyph id back to the code
    point it came from — so a text layer of an Arabic invoice comes out as Arabic
    rather than as a row of private-use characters. The declared count has to
    match, or a reader that trusts it stops early and loses half the text. All of
    the ``bfchar`` blocks are read, not just the first: the CMap is written in
    chunks of 100 and the chunking is a property of the writer, not of the text,
    so a test that only read the first chunk would quietly stop covering the rest
    of the glyphs the day the page grew past one.
    """
    pdf = render_invoice_pdf(arabic_invoice())
    stream = re.search(rb"8 0 obj\n<< /Length \d+ >>\nstream\n(.*?)\nendstream", pdf, re.S)
    assert stream, "the ToUnicode CMap must be a stream"
    cmap = stream.group(1).decode("latin-1")
    assert "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange" in cmap
    pairs: list[tuple[str, str]] = []
    for declared, body in re.findall(r"(\d+) beginbfchar\n(.*?)endbfchar", cmap, re.S):
        found = re.findall(r"<([0-9A-F]{4})> <([0-9A-F]{4})>", body)
        assert int(declared) == len(found), "a bfchar block must declare its own length"
        pairs += found
    emitted = {gid for span in spans(pdf) for gid in glyph_ids(span["gids"] or "")}
    assert len(pairs) == len(emitted)
    assert {int(gid, 16) for gid, _ in pairs} == emitted
    for gid, codepoint in pairs:
        # The mapping is the identity on the font's own cmap: the code point the
        # shaper chose is the one the text layer has to report.
        assert FONT.glyph_id(int(codepoint, 16)) == int(gid, 16)


@pytest.mark.parametrize("count", [0, 1, 5, 60, 200])
def test_page_count_grows_with_line_items(count: int) -> None:
    items = tuple(
        InvoiceItem(
            name=f"Item {index}",
            quantity=Decimal("1"),
            unit_price=Decimal("10.00"),
            total=Decimal("10.00"),
        )
        for index in range(count)
    )
    pdf = render_invoice_pdf(make_invoice(items=items))
    pages = int(re.search(rb"/Count (\d+)", pdf).group(1))
    assert pages == len(re.findall(rb"/Type /Page[^s]", pdf))
    assert pages >= 1
    if count > 20:
        assert pages > 1


def test_declared_stream_length_matches_the_bytes_written() -> None:
    pdf = render_invoice_pdf(make_invoice())
    for match in re.finditer(rb"<< /Length (\d+) >>\nstream\n", pdf):
        declared = int(match.group(1))
        body = pdf[match.end() : match.end() + declared]
        assert body.endswith(b"endstream") is False
        assert pdf[match.end() + declared : match.end() + declared + 9] == b"endstream"


# --- Latin content ---------------------------------------------------------------


def test_document_contains_the_invoice_facts() -> None:
    text = stream_text(render_invoice_pdf(make_invoice()))
    for expected in ("INV-042", "14/03/2026", "Acme Trading", "USD", "Widget"):
        assert expected in text
    # The money lines are formatted with thousands separators and two decimals.
    for expected in ("100.00", "120.00", "20.00"):
        assert expected in text


def test_item_names_are_truncated_to_their_column() -> None:
    long_name = "W" * 400
    pdf = render_invoice_pdf(make_invoice(items=(InvoiceItem(name=long_name),)))
    text = stream_text(pdf)
    assert "W" * 400 not in text
    assert "..." in text


def test_an_over_wide_quantity_is_drawn_in_full_rather_than_truncated() -> None:
    """A quantity is a figure, so it gets money's policy: overflow, never ellipsis.

    It used to be the one numeric field routed through ``_fit``, so a quantity
    too wide for its 46pt column was silently shortened — ``99999999999`` reached
    the customer as ``9999999...``. Money has never been treated that way, because
    a truncated amount is a wrong amount.
    """
    quantity = Decimal("99999999999")
    pdf = render_invoice_pdf(
        make_invoice(items=(InvoiceItem(name="Roll", quantity=quantity, unit_price=Decimal("1"), total=quantity),))
    )
    text = stream_text(pdf)
    assert "99999999999" in text
    assert "..." not in text


def test_parentheses_and_backslashes_are_escaped() -> None:
    """``(``/``)``/``\\`` are structural inside a PDF string literal.

    An unescaped ``)`` terminates the run early and corrupts the content stream,
    so the rendered output has to contain the escaped forms and a balanced
    literal — which is exactly what the xref offsets above depend on.
    """
    pdf = render_invoice_pdf(make_invoice(customer_name=r"A (B) \ C"))
    text = stream_text(pdf)
    assert r"(A \(B\) \\ C) Tj" in text


def test_missing_values_render_as_placeholders_not_blanks() -> None:
    pdf = render_invoice_pdf(
        make_invoice(issue_date=None, customer_name="", currency="")
    )
    text = stream_text(pdf)
    assert "(N/A) Tj" in text
    assert "(-) Tj" in text


def test_rendering_never_raises_on_pathological_input() -> None:
    """Weird data must not take a notification down with it."""
    pdf = render_invoice_pdf(
        make_invoice(
            number=None,
            customer_name=None,
            currency="⁨invisible⁩",
            subtotal=None,
            total=None,
            total_paid=None,
            balance_due=None,
            issue_date=None,
            items=(),
        )
    )
    assert pdf.startswith(b"%PDF-1.4")
    assert stream_text(pdf)


# --- Arabic content --------------------------------------------------------------


def test_the_title_is_emitted_as_the_exact_glyph_id_sequence() -> None:
    """The key assertion of the whole feature, digit by digit.

    ``فاتورة`` shaped, reordered for drawing, and mapped through the font's
    ``cmap`` must appear in the content stream as exactly these six glyph ids, in
    this order. The comments name the presentation form behind each id, so a
    failure says which letter came out wrong:

    ============================  ====  ==========================
    presentation form            gid   letter
    ============================  ====  ==========================
    U+FE93 teh marbuta isolated    396  the end of the word
    U+FEAD reh isolated             21  waw does not join forward
    U+FEEE waw final                78  teh does not join forward
    U+FE97 teh initial             886  alef does not join forward
    U+FE8E alef final                6  feh connects into it
    U+FED3 feh initial             903  first letter
    ============================  ====  ==========================

    Note the run is reversed: the content stream draws left to right and an
    Arabic reader reads right to left, so the *last* letter of the word is the
    first glyph id emitted.
    """
    pdf = render_invoice_pdf(arabic_invoice())
    expected = "018C0015004E037600060387"  # 396, 21, 78, 886, 6, 903
    assert glyph_ids(expected) == [396, 21, 78, 886, 6, 903]
    assert [f"U+{ord(char):04X}" for char in arabic.reorder(arabic.shape("فاتورة"))[0][0]] == [
        "U+FE93",
        "U+FEAD",
        "U+FEEE",
        "U+FE97",
        "U+FE8E",
        "U+FED3",
    ]
    title = spans(pdf)[0]
    assert title["font"] == "F2"
    assert title["gids"] == expected
    assert title["gids"] == shaped_gids("فاتورة")


def test_every_arabic_label_is_drawn_from_the_arabic_font() -> None:
    """The chrome is Arabic, and every word of it reaches the page as glyph ids.

    Each label is checked by the glyph ids it must produce, through the whole
    pipeline, so this fails if a label is dropped, left unshaped, or sent to the
    Latin font — the three ways Arabic silently disappears from a PDF. The
    currency is stated once, in the meta row; the table headers carry no suffix.
    """
    pdf = render_invoice_pdf(arabic_invoice())
    drawn = {span["gids"] for span in spans(pdf) if span["font"] == "F2"}
    for label in (
        "فاتورة",
        "رقم الفاتورة",
        "العميل",
        "تاريخ الإصدار",
        "العملة",
        "بنود الفاتورة",
        "الصنف",
        "الكمية",
        "سعر الوحدة",
        "إجمالي الصنف",
        "المجموع الفرعي",
        "الإجمالي",
        "المدفوع",
        "المتبقي",
    ):
        assert shaped_gids(label) in drawn, f"{label} is not in the content stream"


def test_an_arabic_invoice_draws_its_arabic_values_in_the_arabic_font() -> None:
    """The customer name and the item name, the two values that carry the text."""
    pdf = render_invoice_pdf(arabic_invoice())
    drawn = {span["gids"] for span in spans(pdf) if span["font"] == "F2"}
    assert shaped_gids("شركة النور للديكور") in drawn
    assert shaped_gids("ورق حائط كلاسيك") in drawn
    # And the Latin parts of the same line are still drawn, still as literals.
    literals = {span["literal"] for span in spans(pdf) if span["literal"]}
    assert {"INV-042", "14/03/2026", "EGP", "100.00", "2"} <= literals


def test_mixed_arabic_and_latin_on_one_line_use_both_fonts() -> None:
    """``رول فيلكس 45 سم`` — Arabic around a Latin digit run.

    The digits are drawn from the Helvetica font in reading order, with the Arabic
    either side of them from the embedded font, all on one baseline and touching
    each other, so the line reads as one phrase. The name is found by its own glyphs
    and the two neighbours are taken, rather than assuming where in the stream the
    cell sits — the cell's position is a detail of the drawing order, the phrase is
    the thing under test.
    """
    pdf = render_invoice_pdf(arabic_invoice(items=(InvoiceItem(name="رول فيلكس 45 سم"),)))
    runs = arabic.reorder(arabic.shape("رول فيلكس 45 سم"))
    assert [is_rtl for _, is_rtl in runs] == [True, False, True]
    wanted = {
        "".join(f"{FONT.glyph_id(ord(char)):04X}" for char in run)
        for run, is_rtl in runs
        if is_rtl
    }
    row = next(row for row in rows(pdf).values() if any(s["gids"] in wanted for s in row))
    index = next(i for i, span in enumerate(row) if span["literal"] == "45")
    cell = row[index - 1 : index + 2]
    assert [span["font"] for span in cell] == ["F2", "F1", "F2"]
    assert cell[1]["literal"] == "45"
    assert cell[0]["gids"] in wanted and cell[2]["gids"] in wanted
    # The three spans are laid end to end, left to right, with no gap between them.
    for left, right in zip(cell, cell[1:]):
        end = float(left["x"]) + (
            arabic_width(left["gids"], float(left["size"]))
            if left["gids"]
            else latin_width(left["literal"], float(left["size"]))
        )
        assert abs(end - float(right["x"])) < 0.02


def test_a_lam_alef_ligature_is_one_glyph_id() -> None:
    """``بلا`` is two glyph ids: the lam-alef ligature and the beh.

    Written as lam + alef it would be three, and a reader would see two letters
    where the script has one joined pair — the most visible shaping failure there
    is, since it is in almost every word starting with لا. The ligature is drawn
    first because the run is reversed: it is the leftmost of the two glyphs on the
    page, which is where the ligature belongs when the beh comes after it.
    """
    ligature = shaped_gids("بلا")
    assert len(glyph_ids(ligature)) == 2
    assert glyph_ids(ligature) == [FONT.glyph_id(0xFEFC), FONT.glyph_id(0xFE91)]
    assert FONT.glyph_id(0xFEFC) == 880
    pdf = render_invoice_pdf(arabic_invoice(customer_name="بلا"))
    drawn = {span["gids"] for span in spans(pdf) if span["font"] == "F2"}
    assert ligature in drawn


def test_arabic_renders_without_a_warning(caplog) -> None:
    """Arabic is supported, so it must not be reported as undrawable.

    The old writer dropped every non-WinAnsi character and logged a warning per
    line. Now the only warnings left are for characters neither font can draw and
    for Arabic letters that have no presentation form.
    """
    with caplog.at_level("WARNING"):
        pdf = render_invoice_pdf(arabic_invoice())
    assert caplog.records == []
    assert pdf.startswith(b"%PDF-1.4")


def test_a_totalled_invoice_with_no_line_items_is_reported(caplog) -> None:
    """The empty-items state is designed, so it renders — but a *totalled* invoice
    with no rows is the signature of an invoice that was never re-fetched from the
    detail endpoint, and the document says so to the customer. It must never reach
    a customer unremarked.
    """
    with caplog.at_level("WARNING"):
        pdf = render_invoice_pdf(make_invoice(items=()))
    assert "no line items" in caplog.text
    assert "INV-042" in caplog.text
    assert pdf.startswith(b"%PDF-1.4")


def test_an_untouched_invoice_with_nothing_on_it_is_not_reported(caplog) -> None:
    """The narrow case has to stay quiet, or the warning is noise nobody reads.

    No rows *and* nothing charged is the legitimate version of the same state.
    """
    with caplog.at_level("WARNING"):
        render_invoice_pdf(
            make_invoice(
                items=(),
                subtotal=Decimal("0"),
                total=Decimal("0"),
                total_paid=Decimal("0"),
                balance_due=Decimal("0"),
            )
        )
    assert caplog.records == []


def test_a_character_neither_font_can_draw_is_replaced_and_reported(caplog) -> None:
    """CJK and emoji are outside both fonts: warn, and draw the placeholder.

    The behaviour is deliberate in both directions — the invoice still renders and
    the sender still sends, but the log says the text was not what the customer
    typed, so nobody discovers it from a customer's screenshot.
    """
    with caplog.at_level("WARNING"):
        pdf = render_invoice_pdf(arabic_invoice(customer_name="中文 😀"))
    text = stream_text(pdf)
    assert "can render" in caplog.text
    assert "customer name" in caplog.text
    assert "(-) Tj" in text  # nothing was drawable, so the placeholder stands in
    assert "INV-042" in text  # a sibling value is unaffected
    assert "14/03/2026" in text


def test_arabic_survives_alongside_characters_neither_font_can_draw(caplog) -> None:
    """The undrawable characters go; the Arabic in the same value stays.

    Dropping the whole value because one character in it cannot be drawn would
    lose the customer's name over a stray emoji.
    """
    with caplog.at_level("WARNING"):
        pdf = render_invoice_pdf(arabic_invoice(customer_name="شركة 😀 النور"))
    assert "can render" in caplog.text
    drawn = {span["gids"] for span in spans(pdf) if span["font"] == "F2"}
    # Both words survive, with the space the emoji was sitting in still between
    # them: the writer drops what it cannot draw rather than reflowing the rest,
    # so the gap is visible and nothing is invented in the customer's name.
    assert shaped_gids("شركة  النور") in drawn
    assert shaped_gids("شركة النور") not in drawn


def test_persian_letters_are_drawn_and_reported_as_unjoined(caplog) -> None:
    """A letter with no presentation form: drawn, never joined, and said so.

    Dropping the customer's name would be worse than a name that does not join,
    and the warning is what makes the difference visible to the operator. The
    warning names the field and the code points, so the operator can tell which
    invoice and which character.
    """
    with caplog.at_level("WARNING"):
        pdf = render_invoice_pdf(arabic_invoice(customer_name="پارس"))
    assert "customer name" in caplog.text
    assert "no Arabic presentation form" in caplog.text
    assert "U+067E" in caplog.text  # the peh, by code point
    # The peh really is in the file: the shaper passes it through unshaped, and the
    # writer draws it rather than dropping the name.
    drawn = {span["gids"] for span in spans(pdf) if span["font"] == "F2"}
    assert shaped_gids("پارس") in drawn
    assert f"{FONT.glyph_id(0x067E):04X}" in "".join(sorted(drawn))


def test_money_is_never_truncated() -> None:
    """An amount is the one value that may not be shortened to fit.

    Every other cell is fitted to its column, which ends in an ellipsis; money is
    drawn at its full width and, if it were too wide for the column, the column
    would be the thing that has to give.
    """
    big = Decimal("9876543.21")
    pdf = render_invoice_pdf(
        arabic_invoice(
            subtotal=big,
            total=big,
            total_paid=Decimal("0.00"),
            balance_due=big,
            items=(InvoiceItem(name="صنف", quantity=Decimal("1"), unit_price=big, total=big),),
        )
    )
    text = stream_text(pdf)
    assert "9,876,543.21" in text
    assert "9,876,543.2" not in text.replace("9,876,543.21", "")  # no half number
    assert text.count("9,876,543.21") >= 4  # line total, and each of the three totals


# --- layout ----------------------------------------------------------------------


def test_the_table_columns_run_from_the_right_margin_inwards() -> None:
    """Right-to-left column order: name, quantity, unit price, line total.

    Asserted from the drawn positions rather than from the writer's constants, so
    it is the document that is checked: the item name ends at the right margin and
    the line total is the leftmost column — the reverse of the old left-to-right
    table, which is also the bug that once put the total 30pt off the paper.

    Money still *ends* on its column's right edge, because two decimals always
    occupy the same width, so the decimal axis is exactly the right edge for it.
    The quantity column is the one that cannot be right-aligned any more: a whole
    number and a fraction have different widths, and right-aligning them left the
    decimal points in a ragged edge. So what is asserted for the quantity is the
    axis its decimal point sits on — still derived from the right margin inwards,
    one column width in, and still well clear of the price column beside it.
    """
    pdf = render_invoice_pdf(arabic_invoice())
    # One row of the table: the one holding the quantity "2".
    row = next(row for row in rows(pdf).values() if any(s["literal"] == "2" for s in row))
    # Each span ends on its own column's anchor — the right edge for money, the
    # decimal axis for the quantity — so the set of those anchors is the set of
    # column edges, and each value lands on the right one.
    ends = {}
    for span in row:
        right = float(span["x"]) + (
            arabic_width(span["gids"], float(span["size"]))
            if span["gids"]
            else latin_width(span["literal"], float(span["size"]))
        )
        ends[span["gids"] or span["literal"]] = round(right, 2)
    assert sorted(ends.values(), reverse=True) == [539.0, 276.49, 239.0, 154.0]
    assert ends[shaped_gids("ورق حائط كلاسيك")] == RIGHT_MARGIN  # the name column
    assert ends["2"] == 276.49  # the quantity column's decimal axis
    assert ends["50.00"] == 239.0  # the unit price column
    assert ends["100.00"] == 154.0  # the line total column


def test_nothing_is_drawn_outside_the_margins() -> None:
    """Every span has to fit between the margins, whichever font draws it.

    An Arabic span is measured from the embedded font's own advances and a Latin one
    from Helvetica's published metrics, so both ends are known without rendering:
    the claim is that no text is clipped by the paper's edge or the trim box. The
    Helvetica check is the one that used to be missing, and it is the one a long
    amount would fail — a character-count estimate is 11% narrow for digits, which is
    enough to push a full money column into the column next to it.
    """
    pdf = render_invoice_pdf(arabic_invoice())
    for span in spans(pdf):
        x = float(span["x"])
        size = float(span["size"])
        assert x >= LEFT_MARGIN - 0.01, span
        if span["gids"]:
            right = x + arabic_width(span["gids"], size)
        else:
            right = x + latin_width(span["literal"], size)
        assert right <= RIGHT_MARGIN + 0.01, span


def test_a_long_amount_stays_inside_its_column() -> None:
    """A big number is measured, not counted: it fills the total column exactly.

    ``9,876,543.21`` is 52.5pt of real Helvetica digits in a 70pt column. The old
    half-an-em-per-character estimate called that 55pt — but the same estimate would
    have let a longer number through that the viewer then drew wider than the box.
    """
    amount = Decimal("9876543.21")
    pdf = render_invoice_pdf(
        arabic_invoice(
            items=(
                InvoiceItem(
                    name="ورق حائط كلاسيك",
                    quantity=Decimal("1"),
                    unit_price=amount,
                    total=amount,
                ),
            )
        )
    )
    row = next(
        row for row in rows(pdf).values() if any(s["literal"] == "9,876,543.21" for s in row)
    )
    # The amount is in both money columns. A row is drawn column by column from the
    # right, so the unit price (154..239) arrives before the line total (84..154).
    unit_price, total = [span for span in row if span["literal"] == "9,876,543.21"]
    width = latin_width("9,876,543.21", float(total["size"]))
    assert abs(width - 52.54) < 0.01  # 9 digits, two separators, a period
    # Each right-aligns in its own column.
    assert abs(float(unit_price["x"]) - (239.0 - width)) < 0.02
    assert abs(float(total["x"]) - (154.0 - width)) < 0.02
    assert float(total["x"]) >= LEFT_MARGIN


def test_the_headings_are_anchored_at_their_documented_edges() -> None:
    """Every heading sits at a deliberate, documented edge of the page.

    The items heading belongs to no column, so it sits at the right margin — the
    name column's right edge, where a right-to-left table starts. The title sits
    at the right edge of its own zone in the brand header, where the logo's block
    leaves room for it: the right margin itself now belongs to the brand.
    """
    pdf = render_invoice_pdf(arabic_invoice())
    ends = {
        span["gids"]: float(span["x"]) + arabic_width(span["gids"], float(span["size"]))
        for span in spans(pdf)
        if span["gids"]
    }
    assert round(ends[shaped_gids("فاتورة")], 2) == TITLE_END
    assert round(ends[shaped_gids("بنود الفاتورة")], 2) == RIGHT_MARGIN


# --- the brand header and the logo -------------------------------------------------


def test_the_logo_is_a_pair_of_flate_image_xobjects() -> None:
    """Objects 9 and 10: the colour image and its alpha mask, as documented.

    PDF 1.4 has no alpha channel on an image: the logo's transparent ground is
    a second image — a DeviceGray soft mask — referenced from the colour one
    through ``/SMask``, where 255 means opaque. Both are Flate-compressed raw
    samples, and both carry the numbering promised in ``_assemble``'s table.
    """
    pdf = render_invoice_pdf(make_invoice())
    colour = object(pdf, 9)
    assert "/Type /XObject" in colour
    assert "/Subtype /Image" in colour
    assert "/Width 360 /Height 360" in colour
    assert "/ColorSpace /DeviceRGB" in colour
    assert "/BitsPerComponent 8" in colour
    assert "/Filter /FlateDecode" in colour
    assert "/SMask 10 0 R" in colour
    mask = object(pdf, 10)
    assert "/Subtype /Image" in mask
    assert "/Width 360 /Height 360" in mask
    assert "/ColorSpace /DeviceGray" in mask
    assert "/BitsPerComponent 8" in mask
    assert "/Filter /FlateDecode" in mask


def test_the_logo_streams_decompress_to_the_exact_raw_sample_arrays() -> None:
    """A DeviceRGB image of 360×360 is exactly 388,800 bytes of samples, and
    its gray mask exactly 129,600 — the statement that the bundled PNG was
    decoded to raw samples rather than wrapped or re-encoded."""
    pdf = render_invoice_pdf(make_invoice())
    for number, expected in ((9, 360 * 360 * 3), (10, 360 * 360)):
        body = object(pdf, number)
        declared = int(re.search(r"/Length (\d+)", body).group(1))
        stream = re.search(r"stream\n(.*?)endstream", body, re.S).group(1)
        assert declared == len(stream)
        assert len(zlib.decompress(stream.encode("latin-1"))) == expected


def test_the_logo_is_drawn_once_on_page_1_inside_the_header() -> None:
    """One ``Do`` op, on page 1 only: the logo is letterhead branding, not page
    furniture, so a continuation page carries the repeated table header and
    the footer, never the brand block. The ``cm`` matrix is the placement — a
    square scaled into the header's brand block, above the meta rows, inside
    the margins, clear of the text."""
    pdf = render_invoice_pdf(arabic_invoice(items=_items(60)))
    streams = page_streams(pdf)
    assert len(streams) > 1
    drawn = re.findall(
        r"q\n([\d.]+) 0 0 ([\d.]+) ([\d.]+) ([\d.]+) cm\n/Im0 Do\nQ", streams[0]
    )
    assert len(drawn) == 1
    width, height, x, y = (float(value) for value in drawn[0])
    assert width == height == pytest.approx(LOGO_SIZE)  # square: the aspect is the brand's
    assert x >= LEFT_MARGIN
    assert x + width <= RIGHT_MARGIN + 0.01
    assert y >= META_Y  # above the meta rows
    assert y + height <= LOGO_TOP + 0.01  # and inside the top margin
    for stream in streams[1:]:
        assert "/Im0 Do" not in stream


def test_page_1_names_the_logo_in_its_resources() -> None:
    """A ``Do`` op is only legal with the XObject named in the page's resources."""
    pdf = render_invoice_pdf(make_invoice())
    page = object(pdf, 11)  # one page: the first page object after the fonts and the logo
    assert "/Font << /F1 3 0 R /F2 4 0 R >>" in page
    assert "/XObject << /Im0 9 0 R >>" in page


def test_the_brand_name_sits_against_the_logo_in_the_header() -> None:
    """Aizen Paper, right-aligned into the gap the logo leaves and inside its
    vertical span — the brand block reads as one thing: a mark and its name.

    The wordmark is English-only and is never transliterated, so it is drawn by
    Helvetica (``F1``), not the Arabic face. This asserts the font explicitly: a
    silent swap to ``F2`` would mean the brand had been transliterated.
    """
    pdf = render_invoice_pdf(arabic_invoice())
    brand = next(span for span in spans(pdf) if span["literal"] == "Aizen Paper")
    assert brand["font"] == "F1"
    assert float(brand["size"]) == 22.0
    end = float(brand["x"]) + latin_width("Aizen Paper", 22.0)
    assert round(end, 2) == BRAND_END
    assert float(brand["x"]) >= LEFT_MARGIN
    assert LOGO_TOP - LOGO_SIZE < float(brand["y"]) < LOGO_TOP  # inside the logo's span


def test_the_brand_wordmark_is_never_transliterated() -> None:
    """No Arabic-script rendering of the brand reaches the page.

    The name is a registered Latin wordmark; ``أوراق عايزن`` was a transliteration
    of it and must not come back.
    """
    pdf = render_invoice_pdf(arabic_invoice())
    assert b"Aizen Paper" in pdf
    assert "أوراق عايزن" not in page_text(pdf)


def test_the_rendered_pdf_is_deterministic_with_the_logo_embedded() -> None:
    """The media upload cache keys on the PDF's digest: the same invoice has to
    render to the same bytes, logo and all, or every poll re-uploads it."""
    assert render_invoice_pdf(arabic_invoice()) == render_invoice_pdf(arabic_invoice())


def test_the_currency_is_stated_once_in_the_meta_row() -> None:
    """``العملة EGP`` is the page's single statement of the currency.

    The currency used to ride the table headers as well. It is stated once now,
    in the meta row, because the unit column's right edge *is* the quantity
    column's left edge — a currency run in the header would have no gutter to
    sit in — and because a reader who has already been told ``العملة EGP`` at the
    top does not need it repeated over every figure column. The meta row still
    draws it as Latin, on the same baseline as its Arabic label, and the table
    header spells no currency at all.
    """
    pdf = render_invoice_pdf(arabic_invoice())
    currency_gids = shaped_gids("العملة")
    row = next(row for row in rows(pdf).values() if any(s["gids"] == currency_gids for s in row))
    label = next(s for s in row if s["gids"] == currency_gids)
    value = next(s for s in row if s["literal"] == "EGP")
    assert value["font"] == "F1"
    # The value is drawn to the label's left, its right edge clear of the label.
    value_end = float(value["x"]) + latin_width("EGP", float(value["size"]))
    assert value_end <= float(label["x"])
    # And the table header repeats nothing: its cells are Arabic only.
    header = next(row for row in rows(pdf).values() if any(s["gids"] == shaped_gids("الكمية") for s in row))
    assert not any(s["literal"] for s in header)


def test_an_arabic_item_list_paginates_with_the_header_repeated() -> None:
    """A long Arabic list breaks between items, and each page repeats the header.

    The header is drawn again after a page break because a table continued onto
    the next page without its column labels is a table nobody can read. And the
    break happens between whole items: the last thing on a page is a complete
    Arabic name, not half a word.
    """
    items = tuple(
        InvoiceItem(
            name=f"صنف رقم {index}",
            quantity=Decimal("1"),
            unit_price=Decimal("10.00"),
            total=Decimal("10.00"),
        )
        for index in range(60)
    )
    pdf = render_invoice_pdf(arabic_invoice(items=items))
    pages = int(re.search(rb"/Count (\d+)", pdf).group(1))
    assert pages > 1
    streams = [
        body.decode("latin-1")
        for body in re.findall(rb"stream\n(.*?)endstream", pdf, re.S)
        if b" Tf " in body
    ]
    assert len(streams) == pages
    for stream in streams:
        assert shaped_gids("الصنف") in stream  # the header cells
    # The break lands between items: every page ends with a drawn row, and the
    # first page's last Arabic span is one whole item name. The footer is drawn
    # after the layout, so it is skipped here — page furniture is not a row, and
    # content stops well above the band the footer sits in.
    first_page = [m.groupdict() for m in SPAN.finditer(streams[0])]
    content = [span for span in first_page if float(span["y"]) > 74.0]
    last_arabic = [span for span in content if span["gids"]][-1]
    names = {span["gids"] for span in content if span["gids"]}
    assert any(last_arabic["gids"] == shaped_gids(f"صنف رقم {index}") for index in range(60))
    assert names


def _items(count: int) -> tuple[InvoiceItem, ...]:
    """*count* line items, one of them with a quantity in exponent form."""
    return tuple(
        InvoiceItem(
            name=f"صنف رقم {index}",
            quantity=Decimal("1E+2") if index == 0 else Decimal("1"),
            unit_price=Decimal("10.00"),
            total=Decimal("10.00"),
        )
        for index in range(count)
    )


@pytest.mark.parametrize(
    ("status", "label"),
    [
        ("Draft", "مسودة"),
        ("Unpaid", "غير مدفوعة"),
        ("Partially Paid", "مدفوعة جزئياً"),
        ("Paid", "مدفوعة"),
        ("Refunded", "مستردة"),
        ("Overpaid", "مدفوعة بالزيادة"),
        ("Unknown", "غير معروفة"),
    ],
)
def test_the_status_chip_states_the_invoice_in_arabic_inside_the_margins(
    status: str, label: str
) -> None:
    """The first thing a customer looks for, in the language they read.

    The chip is a filled box with the status drawn on it, on the title's line and to
    the left of it, so it is the first fill on the page and it has to clear the title
    by more than the 6pt the two are allowed to come near. Every status the mapper
    can produce is checked by the glyph ids it must produce: a missing key would fall
    through to the raw text, and the Arabic customer would be the one to notice.
    """
    pdf = render_invoice_pdf(make_invoice(status=status))
    stream = page_text(pdf)
    chip = next(span for span in spans(pdf) if float(span["size"]) == 8.5)
    assert chip["font"] == "F2"
    assert chip["gids"] == shaped_gids(label)
    fill = RECT.search(stream)
    assert fill, "the chip must be a filled box, not just coloured text"
    left, width = float(fill["x"]), float(fill["w"])
    assert left >= LEFT_MARGIN and left + width <= RIGHT_MARGIN
    # The label sits inside the box it is painted on — vertically as well as
    # horizontally, which is what stops a chip whose text hangs off its own bottom
    # edge — and the box clears the title by more than the 6pt the two are allowed to
    # come near.
    low = float(fill["y"])
    assert low < float(chip["y"]) < low + float(fill["h"])
    assert left <= float(chip["x"])
    assert float(chip["x"]) + arabic_width(chip["gids"], 8.5) <= left + width
    # ...and the box is clear of the title, which is the first text on the page.
    title = spans(pdf)[0]
    assert left + width <= float(title["x"]) - 6


def test_a_status_the_table_does_not_know_is_shown_verbatim() -> None:
    """An unmapped status is drawn, not dropped and not guessed at.

    The mapper title-cases whatever the API sent, so a state this design has never
    heard of — "On Hold" — can reach the PDF. Drawing the operator's own words in
    the neutral chip puts one Latin word in an Arabic document; hiding the state puts
    a document on the wire that disagrees with the system that produced it. The
    first is the smaller problem, and the second is the one an operator cannot see,
    so the raw text is drawn, in the same chip every other status gets.
    """
    pdf = render_invoice_pdf(make_invoice(status="On Hold"))
    stream = page_text(pdf)
    assert "(On Hold) Tj" in stream
    chip = next(span for span in spans(pdf) if float(span["size"]) == 8.5)
    assert chip["font"] == "F1"  # drawn as written, not transliterated or dropped
    fill = RECT.search(stream)
    assert fill, "an unmapped status still gets the neutral chip"
    # The same neutral grey the Unknown and Draft chips are painted with, pre-mixed
    # with white: the chip says "no strong opinion here" by colour as well as by word.
    assert [float(value) for value in fill["rgb"].split()] == pytest.approx(
        [0.9304, 0.9328, 0.9364], abs=1e-4
    )


def test_every_page_is_numbered_and_a_lone_page_says_so_too() -> None:
    """``صفحة n من m`` on every page, with the right *n* on each one.

    Page furniture is drawn after the layout, into each page's own op list, so the
    number is the page's real position rather than a guess made while laying out.
    The one-page case is not a special case: the same footer is written, which is
    also what keeps a page from ever having an empty content stream.
    """
    pdf = render_invoice_pdf(arabic_invoice(items=_items(60)))
    pages = page_count(pdf)
    assert pages > 1
    streams = page_streams(pdf)
    assert len(streams) == pages
    for number, stream in enumerate(streams, start=1):
        assert shaped_gids("صفحة") in stream  # the word "page"
        assert shaped_gids("من") in stream  # the word "of"
        assert f"({number}) Tj" in stream  # and this page's own number
    # The last page is numbered with the whole document's length, not a guess.
    assert f"({pages}) Tj" in streams[-1]

    single = page_streams(render_invoice_pdf(make_invoice()))
    assert len(single) == 1
    assert shaped_gids("صفحة") in single[0] and shaped_gids("من") in single[0]
    assert single[0].count("(1) Tj") == 2  # "صفحة 1 من 1": both the page and the total


def test_the_decimal_points_of_the_items_table_share_one_axis_per_column() -> None:
    """The axis is what the viewer actually does with the pen, read from the stream.

    The dot of a figure sits at its pen position plus the width of the digits before
    it, so this measures the drawn position rather than the writer's arithmetic. Two
    money columns and a quantity column, each with its own axis, and each constant
    down the page: a quantity of 2, 12.5 and 100 has three different widths, and
    right-aligned those would leave the dots in a ragged edge the eye has to read
    across. Two decimals always occupy the same width, so a money column's axis is
    also its right edge — the position the columns are measured to.
    """
    pdf = render_invoice_pdf(
        arabic_invoice(
            items=(
                InvoiceItem("ورق حائط كلاسيك", Decimal("2"), Decimal("125.50"), Decimal("251.00")),
                InvoiceItem("رول فيلكس", Decimal("12.5"), Decimal("89.00"), Decimal("1112.50")),
                InvoiceItem("شريط لاصق", Decimal("100"), Decimal("4.25"), Decimal("425.00")),
            )
        )
    )
    figure_rows = _figure_rows(pdf)
    assert len(figure_rows) == 3
    quantity_axis = 289.0 - latin_width(".00", 9)  # the quantity column's right edge
    axes: dict[str, list[float]] = {"quantity": [], "price": [], "total": []}
    for row in figure_rows:
        for column, span in zip(axes, row):
            size = float(span["size"])
            head, dot, _tail = span["literal"].partition(".")
            # An integer has no dot, so its right edge *is* the axis: that is the
            # whole reason a quantity column of 2, 12.5 and 100 can be read down.
            axes[column].append(
                round(float(span["x"]) + latin_width(head, size), 2)
                if dot
                else round(float(span["x"]) + latin_width(span["literal"], size), 2)
            )
    # Half a hundredth of a point, because the writer rounds every pen position to
    # two decimals: the axis is exact on the page and within one rounding step here.
    def spread(values: list[float]) -> float:
        return max(values) - min(values)

    assert spread(axes["quantity"]) <= 0.05  # 2, 12.5 and 100 all agree
    assert axes["quantity"][0] == pytest.approx(quantity_axis, abs=0.05)  # 276.49
    for column in ("price", "total"):
        assert spread(axes[column]) <= 0.05
    # Two money columns, one axis each: 85pt apart, which is the column width.
    assert axes["total"][0] == pytest.approx(axes["price"][0] - 85.0, abs=0.05)


def test_the_four_totals_share_one_axis_across_the_balance_due() -> None:
    """All four amounts line up, including the one drawn a size larger.

    The balance due is set at 12.5pt against 9.5pt totals and could easily sit half
    a digit to the left of them; it stays on their axis instead, so the four figures
    a reader is comparing are four figures in one column. The label beside it is
    further left still, which is the other half of the same check.
    """
    pdf = render_invoice_pdf(arabic_invoice())
    totals = [span for span in spans(pdf) if span["literal"] and MONEY.match(span["literal"])]
    totals = [span for span in totals if float(span["x"]) > 300.0]  # the totals zone
    assert len(totals) == 4
    assert sorted({float(span["size"]) for span in totals}) == [9.5, 12.5]
    axes = [
        float(span["x"]) + latin_width(span["literal"].partition(".")[0], float(span["size"]))
        for span in totals
    ]
    # Half a hundredth of a point: the axis is exact on the page, and every pen
    # position in the stream is rounded to two decimals.
    assert max(axes) - min(axes) <= 0.05  # one axis, four lines, two sizes
    # And the largest figure keeps clear of the labels to its right.
    balance = max(float(span["x"]) + latin_width(span["literal"], float(span["size"])) for span in totals)
    assert 419.0 - balance >= 2.0  # the label column starts at 419


def test_the_balance_due_is_faked_bold_and_nothing_else_is() -> None:
    """Emphasis is size, colour and a stroke over the fill — on Latin only.

    There is no bold face: the balance due outlines its own glyphs over their fill,
    which keeps the documented object numbering and the single Latin width table
    intact, and it is applied to Latin only because a stroked calligraphic naskh
    letter fills its counters and strokes its joins shut. The assertion is that
    exactly one span on the page is stroked, that it is the money, and that no
    Arabic span anywhere is.
    """
    for stream in page_streams(render_invoice_pdf(arabic_invoice())):
        for op in _ops(stream):
            if "2 Tr" in op:
                assert "/F1 " in op, "only a Latin span may be stroked"
    pdf = render_invoice_pdf(arabic_invoice())
    stroked = [op for op in _ops(page_text(pdf)) if "2 Tr" in op]
    assert len(stroked) == 1
    assert "/F1 12.5 Tf" in stroked[0] and "0.35 w" in stroked[0]
    # Stroked *and* filled in the accent, so it reads as one heavier number.
    assert "0.05 0.33 0.36 rg" in stroked[0] and "0.05 0.33 0.36 RG" in stroked[0]


def test_a_fill_is_always_painted_before_the_text_that_sits_on_it() -> None:
    """Order is the only thing that keeps a stripe *behind* a row of figures.

    PDF paints in the order the operators appear, so a fill written after the text it
    covers hides that text completely — and there is nothing in the file to say so.
    Every fill on every page is therefore checked to come before every span it
    actually touches, with each operator wrapped in its own ``q … Q`` so no colour or
    line width can leak from one into the next. The fills are counted as well: a
    stripe that stopped being drawn would leave the order assertions intact while
    quietly deleting half the table's texture.
    """
    pdf = render_invoice_pdf(
        arabic_invoice(
            items=(
                InvoiceItem("ورق حائط كلاسيك", Decimal("2"), Decimal("50.00"), Decimal("100.00")),
                InvoiceItem("رول فيلكس", Decimal("3"), Decimal("60.00"), Decimal("180.00")),
                InvoiceItem("شريط لاصق", Decimal("4"), Decimal("70.00"), Decimal("280.00")),
                InvoiceItem("مسمار فولاذي", Decimal("5"), Decimal("80.00"), Decimal("400.00")),
            )
        )
    )
    for stream in page_streams(pdf):
        lines = stream.splitlines()
        # Every op is self-contained, so the two counts are equal: no colour, line
        # width or text state can survive from one operator into the next.
        assert lines.count("q") == lines.count("Q") > 0
        fills: list[tuple[int, tuple[float, float, float, float]]] = []
        drawn: list[tuple[int, tuple[float, float, float, float]]] = []
        for order, op in enumerate(_ops(stream)):
            rect = RECT.match(op)
            if rect:
                left, low = float(rect["x"]), float(rect["y"])
                box = (left, low, left + float(rect["w"]), low + float(rect["h"]))
                # A fill painted over text that is already on the page hides it
                # completely, and nothing in the file would say so.
                assert not any(
                    _overlaps(box, other) for _, other in drawn
                ), f"a fill is painted over the text it covers: {rect['x']} {rect['y']}"
                fills.append((order, box))
                continue
            span = SPAN.search(op)
            if span:
                drawn.append((order, _span_box(span)))
        # The four fills page 1 is made of: the status chip beside the title, the
        # table's header band, and a stripe under the second and fourth item rows —
        # alternate rows, so four items get two stripes and the first row none.
        # A stripe is 12pt tall, 4pt of air above the baseline and 8pt below, so it
        # holds a 9pt row's ink box entirely within itself.
        assert sorted(round(box[3] - box[1]) for _order, box in fills) == [12, 12, 13, 16]
        # And the fills are not decoration: every one of them has text sitting on it,
        # which is what makes the check above a statement about the design.
        for order, box in fills:
            assert any(
                later > order and _overlaps(box, other) for later, other in drawn
            ), f"a fill has nothing on it: {box}"


@pytest.mark.parametrize(
    ("quantity", "drawn"),
    [
        (Decimal("1E+2"), "100"),  # the API's own notation, written out in full
        (Decimal("1.25E+2"), "125"),
        (Decimal("2"), "2"),  # the ordinary case, untouched
        (Decimal("2.50"), "2.50"),
    ],
)
def test_a_quantity_is_drawn_as_plain_digits(quantity: Decimal, drawn: str) -> None:
    """A quantity the API sent in exponent form reaches the customer as a number.

    ``str(Decimal("1E+2"))`` is ``1E+2``, which is a correct number written in a way
    nobody types into a spreadsheet. The value is drawn in full — nothing about a
    quantity is shortened to make it fit — and the ordinary cases come out exactly as
    they always did.
    """
    pdf = render_invoice_pdf(
        arabic_invoice(items=(InvoiceItem(name="صنف", quantity=quantity),))
    )
    text = stream_text(pdf)
    assert f"({drawn}) Tj" in text
    assert "E+" not in text  # no exponent notation anywhere, on any page


def test_the_font_embedding_is_compressed_but_still_the_right_bytes() -> None:
    """The font stream is Flate-compressed, so its bytes are not the file's.

    Asserted on the *decompressed* length instead: a viewer inflates the stream
    and checks what comes out, which is what ``/Length1`` promises.
    """
    import zlib

    pdf = render_invoice_pdf(arabic_invoice())
    match = re.search(rb"<< /Length \d+ /Length1 \d+ /Filter /FlateDecode >>\nstream\n(.*?)endstream", pdf, re.S)
    assert match, "the embedded font must be Flate-compressed"
    inflated = zlib.decompress(match.group(1))
    assert len(inflated) == len(FONT.font_bytes())
    assert inflated[:4] == b"\x00\x01\x00\x00"  # a TrueType sfnt version
    assert struct.unpack(">H", inflated[4:6])[0] == 14  # 14 tables in the subset
