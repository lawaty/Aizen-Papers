"""A tiny, dependency-free PDF writer for the invoice attachment.

Daftra exposes no PDF export endpoint and its ``invoice_pdf_url`` is
session-gated, so the bytes have to be produced here (see ``docs/design.md`` #8).
A PDF is a text-based container, and an invoice needs nothing more than two fonts,
some text, a few rules and the brand's logo — so this module writes the file
directly instead of adding a PDF library to a project whose whole design pitch is
a two-dependency footprint (see ``docs/design.md`` #7).

What it emits:

- A4 pages laid out **right-to-left**, with the chrome in Arabic — the customers
  are Egyptian, so the invoice is Arabic. The *numbers* stay Western
  (``INV-042``, ``1,250.00``, ``14/03/2026``) because that is what the business
  asked for and what they read on the ERP screen.
- Two fonts: the standard-14 ``Helvetica`` (WinAnsi, so nothing is embedded) for
  Latin and digits, and an embedded subset of Noto Naskh Arabic for the Arabic,
  drawn as an ``Identity-H`` ``CIDFontType2``.
- Each page 1 opens with a letterhead brand header: the bundled logo — embedded
  as a Flate-compressed image XObject with a DeviceGray soft mask for its
  transparent ground — with the brand name أوراق عايزن set into the gap it leaves,
  the 26pt title and the payment-status chip in the title zone on the other side,
  and an accent rule closing the band. Under it, four meta rows (invoice number,
  issue date, customer, currency), the line items under بنود الفاتورة on a tinted
  header band with a teal rule under it and alternating row stripes, then a totals
  block whose last line — المتبقي, the balance due — is the one figure on the page
  set larger and in the accent colour. The band, its rule and its column labels
  repeat on every continuation page (the letterhead does not: the logo is page-1
  branding), and the totals block is atomic: it is never split across a page break,
  so the last item row and the four totals always travel together.
- Every page carries a centred ``صفحة n من m`` footer. Money, quantities and the
  totals are aligned on a shared **decimal axis** rather than right-aligned, so the
  decimal points read as a column the eye can compare down.
- A correct cross-reference table, with byte offsets measured on the encoded bytes
  exactly as written.

The palette is a luma ladder (:data:`_INK` through :data:`_ZEBRA`, see the geometry
block) rather than a set of hues, so the hierarchy survives being printed in
grayscale — which is where a lot of these invoices are read. Emphasis is size and
colour, not a bold face: the balance due is faked bold with a stroke over its own
fill (``2 Tr``), which keeps the object numbering and the single Latin width table
below intact, and which is applied to Latin only because a stroked calligraphic
naskh letter closes its own joins.

Arabic without a shaping engine: **a PDF viewer does not shape text.** It maps the
bytes in a content stream to glyph ids through the font's ``cmap`` and draws them
left to right, so an Arabic letter written as a plain code point comes out as an
isolated form with no joins — and the standard-14 fonts cannot draw Arabic at all.
So the work is done here, at generation time:
:mod:`sender.infrastructure.arabic` resolves every Arabic character to its
contextual presentation form and reorders the line into visual runs, and this
module maps those code points to glyph ids (through
:mod:`sender.infrastructure.truetype`) and writes the *ids* as a hex string. The
viewer then performs no character lookup and no shaping of its own. That is the
entire reason the font subset keeps the Presentation Forms-B block.

Text honesty: a character is drawn when **either** font can draw it — Helvetica for
Latin, the Arabic font for Arabic. What neither can (CJK, emoji) is dropped and a
WARNING names the field and the code points, so the loss is visible in the log
instead of being buried in the bytes; a value left empty renders as ``-``. Money is
never shortened or fitted: a truncated amount is a wrong amount.
"""

from __future__ import annotations

import hashlib
import logging
import re
import zlib
from datetime import date
from decimal import Decimal

from sender.domain.models import Invoice
from sender.infrastructure.arabic import reorder, shape, unshaped_arabic
from sender.infrastructure.logo import load_logo
from sender.infrastructure.truetype import TrueTypeFont, load_font

log = logging.getLogger(__name__)

# --- geometry (A4 in points) -----------------------------------------------------

_PAGE_WIDTH = 595
_PAGE_HEIGHT = 842
_MARGIN_X = 56
_TOP_Y = 786
_BOTTOM_Y = 56
_LINE = 13
_TITLE_SIZE = 26
_BRAND_SIZE = 22
_SECTION_SIZE = 10.5
_META_SIZE = 9
_TABLE_SIZE = 9
_TOTALS_SIZE = 9.5
_BALANCE_SIZE = 12.5
_CHIP_SIZE = 8.5
_FOOTER_SIZE = 8

# --- palette ---------------------------------------------------------------------
#
# Every colour below is chosen so the *luma* ladder survives a grayscale print or a
# fax, which is where an invoice often ends up: Y = 0.299R + 0.587G + 0.114B, and
# the hierarchy is carried by Y (0.13 / 0.25 / 0.46 / 0.81 / 0.94 / 0.97) rather
# than by hue, so nothing collapses into one grey when the hue is gone. Hue is only
# ever an *addition* to that ladder — it groups, it never replaces it.

#: Body ink: titles, figures, item names. Y ≈ 0.13, the darkest thing on the page.
_INK = (0.11, 0.13, 0.15)
#: Secondary ink: labels, meta values, the footer. Y ≈ 0.46 — half of body ink, so
#: it reads as a step quieter on paper and still passes as text when photocopied.
_SOFT = (0.44, 0.46, 0.48)
#: The default rule colour — a quiet hairline. Y ≈ 0.81 — present, not loud. The
#: header's own closing rule is the accent (see the brand-header block below).
_HAIRLINE = (0.79, 0.81, 0.83)
#: The single accent hue: deep teal, Y ≈ 0.25. It carries the table's header rule
#: and the balance due — the two things on the page that are worth finding first.
_ACCENT = (0.05, 0.33, 0.36)
#: The table header band. Y ≈ 0.94: a tint, not a fill, so the labels on it stay
#: the same body ink as every other figure.
_BAND = (0.93, 0.945, 0.95)
#: Alternate table rows. Y ≈ 0.97, one step off the paper: a stripe you can see
#: across the table, invisible under a row of text.
_ZEBRA = (0.965, 0.97, 0.972)

#: The payment status, as the chip at the head of page 1 states it.
#:
#: Keyed by the *English* label :class:`~sender.domain.models.Invoice` carries —
#: the values :data:`sender.infrastructure.daftra.mapper.STATUS_LABELS` produces —
#: because the PDF must never disagree with the rest of the system about what an
#: invoice's state is. The Arabic is the customer's word for it; the colour is a
#: muted tint of the status, dark enough to read as text on the chip's own pale
#: fill and separated in luma from :data:`_INK` so a colour-blind reader still gets
#: a second channel.
_STATUS_STYLE: dict[str, tuple[str, tuple[float, float, float]]] = {
    "Draft": ("مسودة", (0.42, 0.44, 0.47)),  # neutral grey: nothing to celebrate
    "Unpaid": ("غير مدفوعة", (0.30, 0.34, 0.40)),  # slate: the default, quiet state
    "Partially Paid": ("مدفوعة جزئياً", (0.55, 0.40, 0.08)),  # amber: money in hand
    "Paid": ("مدفوعة", (0.10, 0.42, 0.24)),  # green: settled
    "Refunded": ("مستردة", (0.52, 0.20, 0.16)),  # rust: money on the way back
    "Overpaid": ("مدفوعة بالزيادة", (0.22, 0.28, 0.50)),  # indigo: a credit
    "Unknown": ("غير معروفة", (0.42, 0.44, 0.47)),  # neutral grey, same as Draft
}
#: The chip colour for a status this table has never heard of.
_STATUS_NEUTRAL = (0.42, 0.44, 0.47)
#: The chip is never allowed to grow into the title's zone; a status word this
#: long has lost any right to it and falls back to raw Latin text.
_STATUS_CHIP_MAX = 150
#: The chip's own geometry. The box is 13pt tall and its bottom edge sits
#: ``_CHIP_UNDER`` below the label's baseline, which leaves 2pt of air both under
#: the descenders and over the caps instead of hugging them.
_CHIP_HEIGHT = 13.0
_CHIP_BASELINE = 5.0
_CHIP_UNDER = 4.25
_CHIP_PADDING = 10.0  # 5pt each side of the label
_CHIP_GAP = 12.0  # the air between the chip and the title

#: A target box for a cell: its left edge, the width available, and how the text
#: sits inside it. ``right`` is the natural alignment for a right-to-left document
#: (everything is anchored at the right margin and grows leftwards), which is why
#: it is what the layout uses; ``left`` exists for a box anchored instead.
Box = tuple[float, float, str]

#: The right margin, and every column measured from it *inwards* in reading order:
#: the item name is the rightmost column, then the quantity, then the unit price,
#: and the line total the leftmost — a right-to-left table, because that is how the
#: document is read. Deriving the columns from the right margin is what keeps them
#: inside the page: the old left-to-right arithmetic put the total column 30pt past
#: the right margin, which is how a large amount ended up off the paper.
_RIGHT = _PAGE_WIDTH - _MARGIN_X
_COL_NAME_WIDTH = 250
_COL_QTY_WIDTH = 50
_COL_PRICE_WIDTH = 85
_COL_TOTAL_WIDTH = 70
_COL_NAME: Box = (_RIGHT - _COL_NAME_WIDTH, _COL_NAME_WIDTH, "right")
_COL_QTY: Box = (_COL_NAME[0] - _COL_QTY_WIDTH, _COL_QTY_WIDTH, "right")
_COL_PRICE: Box = (_COL_QTY[0] - _COL_PRICE_WIDTH, _COL_PRICE_WIDTH, "right")
_COL_TOTAL: Box = (_COL_PRICE[0] - _COL_TOTAL_WIDTH, _COL_TOTAL_WIDTH, "right")

#: The meta rows and the total lines: a label at the right margin, its value to the
#: left of it, both right-aligned so the value sits right next to its own label.
_LABEL_WIDTH = 120
_LABEL_GAP = 12
_LABEL: Box = (_RIGHT - _LABEL_WIDTH, _LABEL_WIDTH, "right")
_VALUE: Box = (_MARGIN_X, _LABEL[0] - _MARGIN_X - _LABEL_GAP, "right")
#: The whole text column, for a heading that belongs to no single column.
_FULL: Box = (_MARGIN_X, _RIGHT - _MARGIN_X, "right")

# --- vertical rhythm -------------------------------------------------------------
#
# One 13pt leading runs the whole page — meta rows, table rows, section headings —
# so the baselines are a single grid rather than a set of one-off spacings. Only
# the totals block steps off it (13.5), because it is the block the eye reads last
# and it needs a hair more air to separate from the table above it.

#: The page-1 brand header, in two halves closed by the accent rule. On the
#: right, where a right-to-left reader starts: the logo, with the brand name set
#: into the gap it leaves. On the left: the document's own identity — the title
#: in its zone, the status chip beside it on the same line, exactly the pair the
#: layout has always drawn. The halves never touch: the air between them is what
#: makes the header a composition rather than a crowd.

#: The bundled logo, on page 1 only: a square, 108pt on a side — large enough to
#: read as the brand at a glance, small enough to leave the page to the invoice.
#: Its top edge sits 3pt inside the top margin, level with where the old 20pt
#: title's cap stood, so the tallest ink on the page stays inside the margin the
#: rest of the layout is built from.
_LOGO_SIZE = 108.0
_LOGO_TOP = _TOP_Y - 3.0
_LOGO_X = _RIGHT - _LOGO_SIZE
#: The brand name, right-aligned into the gap the logo leaves and optically
#: centred against it: the baseline sits 6pt under the logo's mid-line, which is
#: where a word with descenders reads as level with a mark.
_BRAND_TEXT = "أوراق عايزن"
_BRAND_GAP = 16.0
_BRAND_END = _LOGO_X - _BRAND_GAP
_BRAND_Y = _LOGO_TOP - _LOGO_SIZE / 2 - 6.0
#: The title's zone: its right edge, 190pt in from the left margin — room for the
#: 26pt title and the chip beside it, with air to spare before the brand block. A
#: status too long to share the zone drops below the title rather than growing
#: into the brand's half.
_TITLE_END = _MARGIN_X + 190.0
#: The title's baseline: 23pt under the logo's top edge — the same
#: cap-clears-the-margin arithmetic the old 18pt drop did for a 20pt title, now
#: for a 26pt one.
_TITLE_Y = _LOGO_TOP - 23.0
#: The accent rule that closes the header — the old hairline under the title,
#: promoted: one rule, in the one accent hue, closing the whole band — and where
#: the meta rows start under it.
_HEADER_RULE_Y = _LOGO_TOP - _LOGO_SIZE - 14.0
_META_Y = _HEADER_RULE_Y - 22.0
_META_PITCH = _LINE
#: The table's own verticals, all relative to the running baseline.
_HEADING_AIR = 13.0  # air between the meta block and the section heading
_BAND_AIR = 17.0  # section heading to the header band
_BAND_HEIGHT = 16.0  # the header band itself
_BAND_BASELINE = 4.5  # header label above the band's bottom edge
_BAND_CLEAR = 10.0  # first item row below the band's bottom edge: enough for a
# 9pt figure's cap to clear the 1pt accent rule under the band, and the only "air"
# in the table that is not a multiple of the 13pt row pitch.
_ROW_PITCH = _LINE  # item to item
_STRIPE_ABOVE = 4.0  # the zebra band's top edge above its row's baseline
_STRIPE_BELOW = 8.0  # and its bottom edge below
_NAME_GUTTER = 4.0  # the gutter reserved at the name|quantity boundary
#: The repeated table header's own height: the band, its 4.5pt of label rise, and
#: the 4.5pt it keeps clear below — the space a page break has to leave for it.
_HEAD_SPACE = _BAND_BASELINE + _BAND_HEIGHT + _BAND_CLEAR
#: A continuation page drops its repeated header this far below the top margin,
#: because the page break hands the writer :data:`_TOP_Y` as the baseline and a
#: 9pt cap standing on it would poke 6.75pt above the margin. 14pt leaves 2pt of
#: air above the band's top edge, which is the tallest thing on the line.
_CONTINUE_TOP = 14.0
#: The totals block, and the space it has to fit in to stay whole on one page.
_TOTALS_AIR = 6.0  # last table row to the rule above the totals
_TOTALS_HEAD_GAP = 10.0  # that rule to the first total: room for a 9.5pt cap to
# clear the 0.75pt rule without the rule reading as a caption underline
_TOTALS_PITCH = 13.5
_BALANCE_AIR = 6.0  # the last ordinary total to the balance line
_TOTALS_BLOCK = (
    _TOTALS_AIR + _TOTALS_HEAD_GAP + 3 * _TOTALS_PITCH + _BALANCE_AIR + _BALANCE_SIZE
)
#: Footers sit at 60, so content stops at :data:`_BOTTOM_Y + 18` = 74 and keeps at
#: least 6pt of air above its own page number.
_FOOTER_Y = 60
_CONTENT_BOTTOM = _BOTTOM_Y + 18

#: The fixed chrome words, named so the layout does not repeat them inline.
_TITLE_TEXT = "فاتورة"
_ITEMS_HEADING = "بنود الفاتورة"
_ITEMS_EMPTY = "لا توجد بنود في هذه الفاتورة"

_WHITESPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# WinAnsi renders "…" as one glyph; a plain "..." is font-independent.
_ELLIPSIS = "..."
_PLACEHOLDER = "-"

#: The two fonts, by the name they are registered under in the page resources.
_F1 = "/F1"
_F2 = "/F2"
#: The font's family name inside the PDF. The real name comes from
#: :func:`_base_font`, which adds the subset tag a viewer expects.
_BASE_FONT = "NotoNaskhArabic-Subset"

#: Helvetica's advance widths, per 1000 em, for the printable ASCII range. Helvetica
#: is a standard-14 font so it is never embedded, but its metrics are published and
#: every viewer uses exactly these, which is what makes a Latin run measurable
#: without shipping a font file. Taken from the standard Helvetica AFM.
_HELVETICA_EM: tuple[int, ...] = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333,
    278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278,
    584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278,
    500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944,
    667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556,
    278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500,
    278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)

#: The rest of WinAnsi, which a customer name can legally contain. Not the whole
#: encoding — just enough not to guess at the accented letters, a currency code that
#: uses one, or the placeholders this module inserts.
_HELVETICA_REST_EM = 556

#: One drawable piece of a line: the font to draw it with, the characters, and the
#: glyph ids they resolve to (empty for Helvetica, a standard-14 font whose
#: metrics live in the viewer). ``/W`` and ``/ToUnicode`` are built from the ids.
Span = tuple[str, str, tuple[int, ...]]


# --- text handling ---------------------------------------------------------------


def _flatten(value) -> str:
    """Collapse whitespace and drop C0 controls (illegal in a PDF string)."""
    if value is None:
        return ""
    return _CONTROL.sub("", _WHITESPACE.sub(" ", str(value))).strip()


def _is_winansi(char: str) -> bool:
    """True if *char* encodes to cp1252 — what the standard-14 Helvetica declares."""
    try:
        char.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def _is_drawable(char: str) -> bool:
    """True if either font can draw *char*: Helvetica (WinAnsi) or the Arabic one."""
    return _is_winansi(char) or load_font().glyph_id(ord(char)) is not None


def _shown(value, field: str, placeholder: str = _PLACEHOLDER) -> str:
    """The text that will actually be drawn for *value*.

    Flattens it, drops what neither font can draw (see :func:`_is_drawable`), and
    falls back to *placeholder* when nothing is left. The emptiness check has to
    happen *after* the drop: a value made entirely of undrawable characters
    flattens to a perfectly non-empty string and only then reduces to nothing, and
    rendering an empty run would leave a blank where the customer should be.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for char in _flatten(value):
        (kept if _is_drawable(char) else dropped).append(char)
    if dropped:
        sample = " ".join(f"U+{ord(c):04X}" for c in dict.fromkeys(dropped))
        log.warning(
            "invoice PDF: dropped %d character(s) that neither the standard-14 font "
            "nor the Arabic font can render (not WinAnsi, no glyph in the font's "
            "cmap) from %s: %s; the PDF shows the text without them",
            len(dropped), field, sample,
        )
    return "".join(kept).strip() or placeholder


def _escape(data: bytes) -> bytes:
    """Escape the three characters that are special inside a PDF string literal."""
    for char, escaped in ((b"\\", b"\\\\"), (b"(", b"\\("), (b")", b"\\)")):
        data = data.replace(char, escaped)
    return b"(" + data + b")"


def _literal(text: str) -> bytes:
    """A PDF string literal ``(...)`` around already-safe, already-encoded text.

    Only ever called with characters :func:`_is_drawable` accepted, so the encode
    cannot fail. ``errors="replace"`` is a guard, not a policy: it would draw a
    visible ``?`` rather than raise in the middle of rendering an invoice.
    """
    return _escape(text.encode("cp1252", "replace"))


def _tint(colour: tuple[float, float, float], amount: float) -> tuple[float, float, float]:
    """*colour* mixed *amount* of the way toward white.

    The chip's pale fill is pre-mixed rather than drawn with an alpha, because
    alpha in PDF 1.4 means an ``/ExtGState`` resource — a new entry in every page's
    resource dictionary, on a page layout whose object numbering is a documented
    promise. Mixing the colour here keeps the object graph exactly as it was, and
    the result is the same paint: a solid fill of the tinted colour.
    """
    return tuple(channel + (1.0 - channel) * amount for channel in colour)  # type: ignore[return-value]


def _money(value) -> str:
    if not isinstance(value, Decimal):
        try:
            value = Decimal(str(value))
        except Exception:  # noqa: BLE001 - never let a weird value break the send
            return "-"
    return f"{value:,.2f}"


def _quantity(value) -> str:
    """The quantity as the plain digits a person would write.

    ``str(Decimal("1E+2"))`` is ``1E+2`` — scientific notation, straight into a
    customer-facing invoice, from a quantity the API expressed in exponent form.
    ``format(..., "f")`` writes the value out in full instead, and leaves the
    ordinary cases exactly as they were: ``Decimal("2")`` is still ``"2"``.
    """
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _format_date(value: date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else "N/A"


def _latin_em(text: str) -> float:
    """*text*'s width in em, by Helvetica's own metrics."""
    total = 0.0
    for char in text:
        code = ord(char)
        if 0x20 <= code <= 0x7E:
            total += _HELVETICA_EM[code - 0x20]
        else:
            total += _HELVETICA_REST_EM
    return total / 1000.0


def _text_width(text: str, size: int) -> float:
    """How wide *text* is at *size* as it will actually be drawn.

    Measured through the same pipeline the drawing uses, so the answer is the real
    one: an Arabic run is the sum of the embedded font's advances for the glyphs
    shaping chose, and a Latin run is Helvetica's published metrics. Guessing from a
    character count gets both wrong — a lam-alef ligature is one glyph for two
    characters, and a joined form can be wider than the isolated one.
    """
    return sum(_span_width(span, size) for span in _runs(text))


def _fit(text: str, size: int, width: float) -> str:
    """Truncate *text* with an ellipsis until it fits *width*.

    Measured by drawing it: the loop re-measures after each dropped character, so the
    result is what will be on the page rather than an estimate of it. A name that
    fits is returned untouched — the common case does not pay for the loop.
    """
    if _text_width(text, size) <= width:
        return text
    trimmed = text
    while trimmed and _text_width(trimmed + _ELLIPSIS, size) > width:
        trimmed = trimmed[:-1]
    if trimmed:
        return trimmed + _ELLIPSIS
    # Not even one character plus the ellipsis fits: the ellipsis is the honest
    # thing to show, and if even that does not fit, the placeholder is.
    if _text_width(_ELLIPSIS, size) <= width:
        return _ELLIPSIS
    return _PLACEHOLDER


def _fitted(value, size: int, width: float) -> str:
    """:func:`_fit` on a raw field value, flattened first."""
    return _fit(_flatten(value), size, width)


# --- runs: shaping, reordering, and picking a font --------------------------------


def _font_of(char: str) -> str:
    """The font that draws *char*: the Arabic one if it can, else Helvetica.

    The common case is Arabic in an Arabic run, which belongs in the Arabic font.
    The exception is a neutral that ended up inside the run — a space, a slash, a
    bracket: the Arabic subset carries the space and a couple of marks but almost
    no ASCII punctuation, so those go to Helvetica rather than disappearing.
    """
    return _F2 if load_font().glyph_id(ord(char)) is not None else _F1


def _span_width(span: Span, size: int) -> float:
    """How wide *span* is when drawn at *size*, in points."""
    font, chars, gids = span
    if font == _F2:
        return sum(load_font().advance(gid) for gid in gids) * size / load_font().units_per_em
    return _latin_em(chars) * size


def _runs(text: str) -> list[Span]:
    """Break one line into drawable spans, already in visual (drawing) order.

    The pipeline is *shape → reorder → font*, and the order matters: shaping needs
    its neighbours to pick the right contextual form, so it happens in logical
    order; reordering then produces the runs a content stream can draw left to
    right. Shaped output is still inside the Arabic ranges, so the bidi pass
    classifies it exactly like the input did.

    Runs are also merged per font, so one ``Tf`` covers as much of the line as it
    can. (Where a Latin run butts up to an Arabic one the two fonts' side bearings
    leave a small visible gap: that is how naskh and Helvetica are drawn, not a
    defect, and closing it would mean drawing the Arabic over its own neighbour.)
    """
    spans: list[Span] = []
    for run, is_rtl in reorder(shape(text)):
        if not is_rtl:
            # A Latin run is Helvetica's by definition: the invoice number, the
            # date and the amounts are Latin-only, and the Arabic subset has no
            # Latin letters at all.
            if run:
                spans.append((_F1, run, ()))
            continue
        buffer: list[str] = []
        current: str | None = None
        for char in run:
            wanted = _font_of(char)
            if wanted != current and buffer:
                spans.append(_span(current, "".join(buffer)))
                buffer = []
            current = wanted
            buffer.append(char)
        if buffer and current is not None:
            spans.append(_span(current, "".join(buffer)))
    return [span for span in spans if span[1]]


def _span(font: str, chars: str) -> Span:
    """One span of *chars*, with the glyph ids the Arabic font resolves them to.

    Every character of an Arabic span got here through :func:`_font_of`, which
    only picked the Arabic font for characters that *have* a glyph, so the two are
    index-aligned: ``chars[i]`` is drawn as ``gids[i]``. That is what lets
    ``/ToUnicode`` be written from the drawn text without a second lookup.
    """
    if font != _F2:
        return (font, chars, ())
    data = load_font()
    return (font, chars, tuple(data.glyph_id(ord(char)) for char in chars))


# --- page canvas -----------------------------------------------------------------


class _Canvas:
    """Collects drawing operations, one list per page, in the order they are drawn.

    A page is a list of *self-contained* operators rather than one long ``BT…ET``
    block, because the page is no longer only text: a fill, a hairline, a zebra
    band and a text span all have to interleave in the order the eye reads them.
    Each op carries its own ``q … Q`` and, for text, its own ``BT … ET``, so no
    colour, line width or text state can leak from one op into the next — the
    failure mode of a shared block, where a rule's stroke colour ends up painting
    the figure printed after it.

    Every piece of text still goes through :func:`_runs`, so a line is drawn as one
    or more spans, each with its own font — Arabic as a hex string of glyph ids,
    Latin as a literal string. The glyph ids are remembered here because the font
    dictionary (``/W``, ``/ToUnicode``) is written after the last page.
    """

    def __init__(self) -> None:
        self._pages: list[list[bytes]] = [[]]
        self._page = 0
        self._y = _TOP_Y
        #: glyph id -> the code point it was drawn for.
        self.glyphs: dict[int, str] = {}

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def y(self) -> float:
        """The baseline the next op will be drawn on."""
        return self._y

    def gap(self, height: float = _LINE) -> None:
        self._y -= height

    def move_to(self, y: float) -> None:
        """Put the next baseline exactly at *y* — layout is stated, not accumulated."""
        self._y = y

    def on_page(self, page: int, y: float) -> None:
        """Send what follows to *page* (0-based) with its next baseline at *y*."""
        self._page = page
        self._y = y

    def rule(
        self,
        x: float = _MARGIN_X,
        width: float = _PAGE_WIDTH - 2 * _MARGIN_X,
        y: float | None = None,
        colour: tuple[float, float, float] = _HAIRLINE,
        weight: float = 0.5,
    ) -> None:
        """A horizontal line, as a self-contained stroke op."""
        at = self._y if y is None else y
        self._op(f"q\n{_paint(colour, 'RG')}\n{weight:g} w\n{x:.2f} {at:.2f} m {x + width:.2f} {at:.2f} l S\nQ")

    def fill(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        colour: tuple[float, float, float],
    ) -> None:
        """A solid rectangle, as a self-contained fill op."""
        self._op(f"q\n{_paint(colour, 'rg')}\n{x:.2f} {y:.2f} {width:.2f} {height:.2f} re f\nQ")

    def image(self, x: float, y: float, width: float, height: float) -> None:
        """The brand logo, as a self-contained ``q … cm … Do … Q`` op.

        The XObject is a unit square, so the *cm* matrix is the whole
        placement: *width* and *height* scale it, and (*x*, *y*) is its
        bottom-left corner. Drawn on page 1 only — the logo is letterhead
        branding, and a continuation page already says what it is with the
        repeated table header.
        """
        self._op(f"q\n{width:.2f} 0 0 {height:.2f} {x:.2f} {y:.2f} cm\n/Im0 Do\nQ")

    def text(
        self,
        box: Box,
        value,
        field: str,
        size: float = _TABLE_SIZE,
        colour: tuple[float, float, float] = _INK,
        stroke_w: float = 0.0,
    ) -> None:
        """Draw *value* inside *box*, right-aligned to the box's right edge.

        The whole line is measured first and then drawn left to right from
        ``right - total``: a right-to-left line is only correct if the *line* is
        aligned, not each run separately, or a mixed line would have its number
        one column away from its Arabic label.
        """
        shown = _shown(value, field)
        spans = _runs(shown)
        if not spans:
            # Everything this value had was stripped as an unshaped mark (a name
            # written only in harakat, say). A blank where a value belongs is
            # worse than the placeholder.
            spans = _runs(_PLACEHOLDER)
        total = sum(_span_width(span, size) for span in spans)
        x, width, align = box
        start = x if align == "left" else x + width - total
        # Never grow left past the box: an over-long value overflows to the right,
        # into the neighbouring cell, rather than across the page margin.
        cursor = max(start, x) if align != "left" else start
        self.spans(spans, cursor, size, colour, stroke_w)

    def row(
        self,
        cells: tuple[tuple[Box, object, str], ...],
        size: float = _TABLE_SIZE,
        colour: tuple[float, float, float] = _INK,
    ) -> None:
        for box, value, field in cells:
            self.text(box, value, field, size, colour)

    def spans(
        self,
        spans: list[Span],
        x: float,
        size: float,
        colour: tuple[float, float, float] = _INK,
        stroke_w: float = 0.0,
    ) -> None:
        """Draw already-shaped *spans* from *x* left to right on the current baseline."""
        cursor = x
        for font, chars, gids in spans:
            self._op(self._text_op(font, chars, gids, cursor, size, colour, stroke_w))
            cursor += _span_width((font, chars, gids), size)
            for gid, char in zip(gids, chars):
                self.glyphs.setdefault(gid, char)

    def need(self, height: float = _LINE) -> bool:
        """Break to a new page when *height* no longer fits. True if it did."""
        if self._y - height >= _CONTENT_BOTTOM:
            return False
        self._pages.append([])
        self._page = len(self._pages) - 1
        self._y = _TOP_Y
        return True

    def _text_op(
        self,
        font: str,
        chars: str,
        gids: tuple[int, ...],
        x: float,
        size: float,
        colour: tuple[float, float, float],
        stroke_w: float,
    ) -> str:
        """One text span, as a self-contained ``q … BT … ET … Q`` op.

        ``stroke_w`` is a fake-bold: the same outline painted over the fill, which
        is how the balance due gets its weight without a second font. It is applied
        to the *Latin* span only, and dropped for the Arabic one: a stroked
        calligraphic naskh letter fills its own counters and strokes its joins shut,
        so the word stops being a word. A real bold face would also mean a third
        font and a second AFM width table, for one number on the page — the fixed
        object numbering 1-8 is worth more than the weight.

        ``2 Tr`` goes inside ``BT``, before the font: it is a text-state operator, so
        it has to be, and the ``Tf … Tj`` that follows stays one contiguous run of
        operators — the shape every reader of the content stream expects to find.
        """
        if font == _F2:
            body = "<" + "".join(f"{gid:04X}" for gid in gids) + ">"
            stroke_w = 0.0
        else:
            body = _literal(chars).decode("latin-1")
        stroke = "" if not stroke_w else f"{stroke_w:g} w\n{_paint(colour, 'RG')}\n"
        return (
            f"q\n{_paint(colour, 'rg')}\n{stroke}BT\n"
            f"{'2 Tr ' if stroke_w else ''}{font} {size:g} Tf 1 0 0 1 {x:.2f} {self._y:.2f} Tm {body} Tj\n"
            "ET\nQ"
        )

    def _op(self, body: str) -> None:
        self._pages[self._page].append(body.encode("latin-1"))

    def content(self, page: int) -> bytes:
        return b"\n".join(self._pages[page])


def _paint(colour: tuple[float, float, float], operator: str) -> str:
    """*colour* as PDF colour components for a fill (``rg``) or a stroke (``RG``)."""
    return " ".join(f"{channel:g}" for channel in colour) + f" {operator}"


# --- document layout --------------------------------------------------------------


def _draw_status_chip(
    canvas: _Canvas, status, title_y: float, title_end: float, title_width: float
) -> float:
    """The status chip, to the left of the title. Returns its own baseline.

    The chip is the one place the page says what kind of document it is, and it is
    read first, so it sits on the title's line rather than below it: a customer
    looking for "is this paid" should not have to read four meta rows first. The
    baseline is the title's, the box is centred on the title's cap, and the fill
    is the status colour mixed most of the way to white so the label on it is still
    drawn in the status colour itself.

    The title is anchored at *title_end* — the right edge of its zone in the brand
    header — and the chip takes the line to its left. A status too long to share
    the zone drops below the title, anchored at the title's own end, rather than
    growing into the brand block: no real status comes near this — the longest is
    half the zone — but a long custom status must not push the brand off the page.
    """
    label, colour = _STATUS_STYLE.get(status, (None, _STATUS_NEUTRAL))  # type: ignore[arg-type]
    if label is None:
        # A status this table has never seen is *shown*, not hidden: the operator
        # reads the raw value on the page, and a Latin word in an Arabic document is
        # a smaller problem than a document that silently disagrees with the system.
        label = _fit(_flatten(status), _CHIP_SIZE, _STATUS_CHIP_MAX)
    text = _shown(label, "status")
    width = _text_width(text, _CHIP_SIZE)
    chip_width = width + _CHIP_PADDING
    gap = _CHIP_GAP
    y = title_y
    left = title_end - title_width - gap - chip_width
    if title_width + gap + chip_width > title_end - _MARGIN_X:
        # Too long to share the line: it drops below the title, anchored at the
        # title's own end, rather than growing into the brand block.
        y = title_y - 22
        left = title_end - chip_width
    canvas.fill(left, y + _CHIP_BASELINE - _CHIP_UNDER, chip_width, _CHIP_HEIGHT, _tint(colour, 0.88))
    canvas.move_to(y + _CHIP_BASELINE)
    canvas.text((left + _CHIP_PADDING / 2, width, "left"), text, "status", _CHIP_SIZE, colour)
    canvas.move_to(title_y)  # hand the caller's running baseline back
    return y


def _draw_heading(canvas: _Canvas, invoice: Invoice, currency: str) -> None:
    """The page-1 brand header: the letterhead band, then the meta rows.

    Two halves on one band, closed by the accent rule. On the right, where a
    right-to-left reader starts: the logo, and the brand name set into the gap
    it leaves. On the left: the title in its zone with the status chip beside it
    on the same line — the pair the layout has always drawn, moved into the room
    the brand block leaves. The accent rule closes the band; the meta rows start
    under it.

    The draw order is the one the tests read: the title is the first text on the
    page and the chip's box the first fill, so the pair is drawn before the brand
    block that shares the band with them — and the logo, an image rather than a
    fill, cannot come between a chip and its own label.
    """
    canvas.move_to(_TITLE_Y)
    title_width = _text_width(_TITLE_TEXT, _TITLE_SIZE)
    canvas.text((_MARGIN_X, _TITLE_END - _MARGIN_X, "right"), _TITLE_TEXT, "title", _TITLE_SIZE)
    chip_y = _draw_status_chip(canvas, invoice.status, _TITLE_Y, _TITLE_END, title_width)
    canvas.image(_LOGO_X, _LOGO_TOP - _LOGO_SIZE, _LOGO_SIZE, _LOGO_SIZE)
    canvas.move_to(_BRAND_Y)
    canvas.text((_MARGIN_X, _BRAND_END - _MARGIN_X, "right"), _BRAND_TEXT, "brand name", _BRAND_SIZE)
    canvas.rule(y=_HEADER_RULE_Y, colour=_ACCENT, weight=1.5)
    y = min(_META_Y, chip_y - _META_PITCH)
    for index, (label, value, field, width) in enumerate(
        (
            ("رقم الفاتورة", invoice.number, "invoice number", 120),
            ("تاريخ الإصدار", _format_date(invoice.issue_date), "issue date", 120),
            ("العميل", invoice.customer_name, "customer name", _VALUE[1]),
            ("العملة", currency, "currency", 120),
        )
    ):
        canvas.need(index * _META_PITCH + _META_SIZE)
        canvas.move_to(y - index * _META_PITCH)
        canvas.text(_LABEL, label, f"{field} label", _META_SIZE, _SOFT)
        canvas.text(_VALUE, _fitted(value, _META_SIZE, width), field, _META_SIZE, _INK)


def _header_cells() -> tuple[tuple[Box, str, str], ...]:
    """The items table header, rightmost column first: الصنف, الكمية, سعر, الإجمالي.

    No currency suffix: the meta row has already stated it, and the unit column's
    right edge *is* the quantity column's left edge — a currency run would have no
    gutter to sit in without crowding the figures.
    """
    return (
        (_COL_NAME, "الصنف", "items header"),
        (_COL_QTY, "الكمية", "items header"),
        (_COL_PRICE, "سعر الوحدة", "items header"),
        (_COL_TOTAL, "إجمالي الصنف", "items header"),
    )


def _draw_table_head(canvas: _Canvas) -> None:
    """The table header: the tinted band, its labels, and the accent rule under it.

    Drawn again at the top of every continuation page, because a table continued
    onto a fresh page without its column labels is a table nobody can read.

    There are no vertical column rules. The columns are edge-adjacent by
    construction — the unit price's right edge *is* the quantity's left edge — so a
    separator there would run its line straight through the last digit of every
    figure beside it. The band, the zebra and the accent rule say "this is a table"
    without putting ink where a number has to sit.
    """
    header = _header_cells()
    bottom = canvas.y - _BAND_BASELINE
    canvas.fill(_MARGIN_X, bottom, _RIGHT - _MARGIN_X, _BAND_HEIGHT, _BAND)
    canvas.row(header, _TABLE_SIZE)
    canvas.rule(y=bottom, colour=_ACCENT, weight=1)
    canvas.move_to(bottom - _BAND_CLEAR)


def _draw_figure(
    canvas: _Canvas,
    box: Box,
    text: str,
    size: float,
    colour: tuple[float, float, float],
    mode: float,
    stroke_w: float = 0.0,
) -> None:
    """Draw *text* in *box* with its decimal point on the column's shared axis.

    The pen starts where the whole figure has to be for the point to land in the
    same place on every line: ``axis = right - width(".00")`` measured at *mode*,
    and the figure hangs off that axis by the width of its integer part. Two things
    fall out of that, which is why it is worth a helper rather than a right-align:

    - Money is always written with two decimals, so its right edge lands exactly on
      the column's right edge — the position the columns were measured to, and the
      one the layout's geometry is documented in terms of.
    - Quantities carry a varying number of decimals and the totals block is set a
      size apart from the table, so right-aligning would leave a ragged edge of
      decimal points down the page. On the axis they line up, and a reader can
      compare magnitudes between two lines without reading either.

    *mode* is the size the axis itself is measured at, which is *size* for every
    figure except the balance due: it is drawn larger but stays on the totals'
    axis, so the last number on the page lines up with the three above it. The
    clamp is the same overflow policy as :meth:`_Canvas.text` — a figure too wide
    for its column runs over into the margin rather than being shortened, because
    a truncated amount is a wrong amount.
    """
    left, width, _ = box
    axis = left + width - _text_width(".00", mode)
    head, dot, _tail = text.partition(".")
    x = axis - _text_width(head if dot else text, size)
    canvas.spans(_runs(text), max(x, left), size, colour, stroke_w)


def _draw_items(canvas: _Canvas, invoice: Invoice) -> None:
    canvas.need(_HEADING_AIR + _BAND_AIR + _HEAD_SPACE + _LINE)
    canvas.gap(_HEADING_AIR)
    # The heading belongs to the table, not to a column: it is anchored at the
    # right margin, which is the name column's right edge anyway.
    canvas.text(_FULL, _ITEMS_HEADING, "line items heading", _SECTION_SIZE)
    canvas.gap(_BAND_AIR)
    _draw_table_head(canvas)
    items = invoice.items or ()
    if not items:
        canvas.need(_LINE)
        canvas.text(_FULL, _ITEMS_EMPTY, "line items empty", _TABLE_SIZE, _SOFT)
    for index, item in enumerate(items):
        if canvas.need(_HEAD_SPACE + _LINE):
            canvas.move_to(_TOP_Y - _CONTINUE_TOP)
            _draw_table_head(canvas)
        if index % 2:
            # The stripe goes down before the text that sits on it: a fill painted
            # after the text would cover the figures it is meant to sit behind.
            canvas.fill(_MARGIN_X, canvas.y - _STRIPE_ABOVE, _RIGHT - _MARGIN_X, _STRIPE_ABOVE + _STRIPE_BELOW, _ZEBRA)
        canvas.text(
            _COL_NAME,
            _fitted(item.name, _TABLE_SIZE, _COL_NAME_WIDTH - _NAME_GUTTER),
            f"item {index + 1} name",
            _TABLE_SIZE,
        )
        _draw_figure(
            canvas,
            _COL_QTY,
            _fit(_quantity(item.quantity), _TABLE_SIZE, _COL_QTY_WIDTH - _NAME_GUTTER),
            _TABLE_SIZE,
            _INK,
            _TABLE_SIZE,
        )
        _draw_figure(canvas, _COL_PRICE, _money(item.unit_price), _TABLE_SIZE, _INK, _TABLE_SIZE)
        _draw_figure(canvas, _COL_TOTAL, _money(item.total), _TABLE_SIZE, _INK, _TABLE_SIZE)
        canvas.gap(_ROW_PITCH)


def _draw_totals(canvas: _Canvas, invoice: Invoice) -> None:
    # One reservation for the whole block, rule and balance included. Splitting it
    # would put an amount due — the number the document exists to state — at the
    # top of a page by itself, under a rule that belongs to the rows above it.
    canvas.need(_TOTALS_BLOCK)
    canvas.gap(_TOTALS_AIR)
    canvas.rule(colour=_INK, weight=0.75)
    canvas.gap(_TOTALS_HEAD_GAP)
    for label, value, field in (
        ("المجموع الفرعي", invoice.subtotal, "subtotal"),
        ("الإجمالي", invoice.total, "total"),
        ("المدفوع", invoice.total_paid, "paid"),
    ):
        canvas.need(_TOTALS_PITCH)
        canvas.text(_LABEL, label, f"{field} label", _TOTALS_SIZE, _SOFT)
        # No currency suffix here: the meta row has already established it, and a
        # bare figure is the only way four amounts can share one decimal axis.
        _draw_figure(canvas, _VALUE, _money(value), _TOTALS_SIZE, _INK, _TOTALS_SIZE)
        canvas.gap(_TOTALS_PITCH)
    canvas.need(_BALANCE_AIR + _BALANCE_SIZE)
    canvas.gap(_BALANCE_AIR)
    canvas.text(_LABEL, "المتبقي", "balance due label", _SECTION_SIZE, _INK)
    _draw_figure(
        canvas, _VALUE, _money(invoice.balance_due), _BALANCE_SIZE, _ACCENT, _TOTALS_SIZE, stroke_w=0.35
    )


def _draw_footers(canvas: _Canvas) -> None:
    """``صفحة n من m``, centred, on every page including the first and the last.

    Drawn after the layout, straight into each page's op list, so it needs no second
    pass and no knowledge of where the content stopped: the page count is already
    known and every page gets its own number, on a single page too — which is also
    what guarantees no page is left with an empty content stream.
    """
    total = canvas.page_count
    for index in range(total):
        text = f"صفحة {index + 1} من {total}"
        canvas.on_page(index, _FOOTER_Y)
        width = _text_width(text, _FOOTER_SIZE)
        canvas.text(((_PAGE_WIDTH - width) / 2, width, "left"), text, "footer", _FOOTER_SIZE, _SOFT)


def render_invoice_pdf(invoice: Invoice) -> bytes:
    """Render *invoice* as a single-or-more page PDF document.

    Returns the complete file as ``bytes``, ready to upload. Never raises for odd
    data: a value neither font can represent is dropped with a WARNING, and a
    value that is empty renders as ``-``.
    """
    currency = _flatten(invoice.currency)
    _warn_unshaped(invoice)
    canvas = _Canvas()
    _draw_heading(canvas, invoice, currency)
    _draw_items(canvas, invoice)
    _draw_totals(canvas, invoice)
    _draw_footers(canvas)
    return _assemble([canvas.content(page) for page in range(canvas.page_count)], canvas.glyphs)


def _warn_unshaped(invoice: Invoice) -> None:
    """Report Arabic-block characters that were drawn but never joined.

    A Persian or Urdu letter has no presentation form, so it is drawn as a
    stand-alone glyph in the middle of an Arabic word. It is *not* dropped — the
    subset covers the whole Arabic block, so the letter is there — but a joined
    word drawn half-joined is a defect an operator should be able to see, and
    this is the only place it is visible.
    """
    fields: list[tuple[str, object]] = [
        ("customer name", invoice.customer_name),
        ("status", invoice.status),
    ]
    fields += [
        (f"item {index + 1} name", item.name) for index, item in enumerate(invoice.items or ())
    ]
    for name, value in fields:
        unshaped = unshaped_arabic(_flatten(value))
        if not unshaped:
            continue
        sample = " ".join(f"U+{ord(c):04X}" for c in dict.fromkeys(unshaped))
        log.warning(
            "invoice PDF: %s contains %d character(s) with no Arabic presentation "
            "form (%s); they are drawn, but not joined, because shaping them needs "
            "a font with presentation forms for them",
            name, len(unshaped), sample,
        )


# --- file assembly ----------------------------------------------------------------


def _assemble(streams: list[bytes], glyphs: dict[int, str]) -> bytes:
    """Concatenate objects + xref + trailer into a PDF file.

    Object numbering (fixed for the fonts and the logo, then the pages, so the
    xref table has no holes):

    ==== ==================================================================
    1    catalog
    2    page tree
    3    ``/F1`` Helvetica (standard-14, WinAnsi)
    4    ``/F2`` the Arabic Type0 font
    5    its ``CIDFontType2`` descendant
    6    the font descriptor
    7    the embedded ``FontFile2`` (the subset, Flate-compressed)
    8    the ``ToUnicode`` CMap
    9    the logo image XObject ``/Im0`` (DeviceRGB, Flate-compressed)
    10   its ``/SMask`` — the logo's alpha as a DeviceGray image
    11+2i page *i* (``i`` is 0-based)
    12+2i the content stream of page *i*
    ==== ==================================================================

    The highest object number is ``2 * pages + 10``, and ``/Size`` is one more
    than that — a ``/Size`` that disagrees with the table is exactly how a reader
    ends up reporting "xref num N not found".
    """
    pages = len(streams)
    size = 2 * pages + 11
    kids = " ".join(f"{11 + 2 * index} 0 R" for index in range(pages))
    font_objects = _font_objects(glyphs)

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode("ascii"),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        **font_objects,
        **_logo_objects(),
    }
    for index, stream in enumerate(streams):
        page_object = 11 + 2 * index
        content_object = 12 + 2 * index
        # Page 1 names the logo in its resources — the only page whose content
        # stream draws it. Every other page keeps the bare font dictionary, so a
        # page's resources say exactly what that page uses.
        xobject = " /XObject << /Im0 9 0 R >>" if index == 0 else ""
        objects[page_object] = (
            "<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >>{xobject} >> "
            f"/Contents {content_object} 0 R >>"
        ).encode("ascii")
        objects[content_object] = b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode("ascii") + objects[number] + b"\nendobj\n"

    start_xref = len(out)
    out += f"xref\n0 {size}\n".encode("ascii")
    out += b"0000000000 65535 f \n"  # object 0 is always the free-list head
    for number in range(1, size):
        out += f"{offsets[number]:010d} 00000 n \n".encode("ascii")
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{start_xref}\n%%EOF\n".encode("ascii")
    return bytes(out)


def _font_objects(glyphs: dict[int, str]) -> dict[int, bytes]:
    """Objects 4-8: the Arabic font, as the viewer needs to draw glyph ids.

    ``Identity-H`` + ``CIDToGIDMap /Identity`` is the whole point: the content
    stream carries glyph ids, the encoding maps each 2-byte code to the glyph with
    that id, and the ``CIDToGIDMap`` says "the CID *is* the glyph id". No lookup
    and no shaping happen in the viewer, because none is needed — the shaper
    already did that work in :mod:`sender.infrastructure.arabic`.
    """
    font = load_font()
    raw = font.font_bytes()
    compressed = zlib.compress(raw)
    base_font = _base_font(font)
    widths = _widths(sorted(glyphs), font)
    to_unicode = _to_unicode(glyphs)
    ascent = _scaled(font.ascender, font)
    return {
        4: (
            f"<< /Type /Font /Subtype /Type0 /BaseFont /{base_font} "
            "/Encoding /Identity-H /DescendantFonts [5 0 R] /ToUnicode 8 0 R >>"
        ).encode("ascii"),
        5: (
            f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{base_font} "
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            "/FontDescriptor 6 0 R "
            f"/DW {_scaled(font.advance(0), font)} /W [{widths}] /CIDToGIDMap /Identity >>"
        ).encode("ascii"),
        6: (
            f"<< /Type /FontDescriptor /FontName /{base_font} /Flags 4 "
            f"/FontBBox [{' '.join(str(_scaled(value, font)) for value in font.bbox)}] "
            f"/ItalicAngle 0 /Ascent {ascent} /Descent {_scaled(font.descender, font)} "
            f"/CapHeight {ascent} /StemV 80 /FontFile2 7 0 R >>"
        ).encode("ascii"),
        7: (
            f"<< /Length {len(compressed)} /Length1 {len(raw)} /Filter /FlateDecode >>\n"
            "stream\n"
        ).encode("ascii")
        + compressed
        + b"endstream",
        8: b"<< /Length " + str(len(to_unicode)).encode("ascii") + b" >>\nstream\n" + to_unicode + b"endstream",
    }


def _logo_objects() -> dict[int, bytes]:
    """Objects 9-10: the brand logo as a colour image and its soft mask.

    PDF 1.4 has no alpha channel on an image: transparency is a second image —
    a DeviceGray *soft mask* — referenced from the colour one through
    ``/SMask``, where 255 is opaque. The bundled logo is black artwork on a
    transparent ground, so the mask is what makes it a logo rather than a
    black square. Both streams are the raw sample arrays
    :func:`sender.infrastructure.logo.load_logo` decoded, Flate-compressed with
    the same call the font stream uses — deterministic bytes, because the media
    upload cache keys on the PDF's digest.
    """
    image = load_logo()
    colour = zlib.compress(image.rgb)
    mask = zlib.compress(image.alpha)
    return {
        9: (
            "<< /Type /XObject /Subtype /Image "
            f"/Width {image.width} /Height {image.height} "
            "/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
            f"/Length {len(colour)} /SMask 10 0 R >>\nstream\n"
        ).encode("ascii")
        + colour
        + b"endstream",
        10: (
            "<< /Type /XObject /Subtype /Image "
            f"/Width {image.width} /Height {image.height} "
            "/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
            f"/Length {len(mask)} >>\nstream\n"
        ).encode("ascii")
        + mask
        + b"endstream",
    }


def _base_font(font: TrueTypeFont) -> str:
    """The ``/BaseFont`` name, carrying the six-letter subset tag a PDF uses.

    A reader treats an ``ABCDEF+`` prefix as "this is a subset of the font with
    that name", which is exactly what the file is (a ``pyftsubset`` of Noto Naskh
    Arabic — see ``fonts/README.md``). The tag is derived from the font bytes, so
    it is stable for a given subset and changes when the subset changes; the digest
    is mapped to letters because the tag is defined as six uppercase letters.
    """
    digest = hashlib.sha256(font.font_bytes()).hexdigest()[:6]
    tag = "".join(chr(ord("A") + int(digit, 16) % 26) for digit in digest)
    return f"{tag}+{_BASE_FONT}"


def _scaled(value: int, font: TrueTypeFont) -> int:
    """A font-unit measurement as the 1/1000ths of an em a PDF wants."""
    return round(value * 1000 / font.units_per_em)


def _widths(gids: list[int], font: TrueTypeFont) -> str:
    """The ``/W`` array: the advance of every glyph the document drew.

    Only the used glyphs are listed, grouped into runs of consecutive ids — the
    format is ``[first [w1 w2 …] …]``, and consecutive ids are the only case
    where a list is shorter than the glyph count.
    """
    parts: list[str] = []
    index = 0
    while index < len(gids):
        run = [_scaled(font.advance(gids[index]), font)]
        while index + 1 < len(gids) and gids[index + 1] == gids[index] + 1:
            index += 1
            run.append(_scaled(font.advance(gids[index]), font))
        parts.append(f"{gids[index - len(run) + 1]} [{' '.join(str(w) for w in run)}]")
        index += 1
    return " ".join(parts)


def _to_unicode(glyphs: dict[int, str]) -> bytes:
    """The ``ToUnicode`` CMap: glyph id → the code point it was drawn for.

    This is what makes the PDF *readable* rather than merely drawable: text
    extraction (and therefore search, copy-paste and screen readers) sees the
    Arabic presentation forms the shaper chose, instead of nothing at all.
    """
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    # A bfchar block holds at most 100 entries, and an invoice draws far more than
    # that across its letters, so the mapping is emitted in chunks.
    items = sorted(glyphs.items())
    for start in range(0, len(items), 100):
        chunk = items[start : start + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        for gid, char in chunk:
            lines.append(f"<{gid:04X}> <{ord(char):04X}>")
        lines.append("endbfchar")
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return ("\n".join(lines) + "\n").encode("ascii")
