# Invoice PDF attachment

**Breadcrumb:** [Home](../index.md) / [Guide](.) / [Invoice PDF](invoice-pdf.md)

---

Customers want the invoice itself, not just a link to it. This page explains how
the PDF reaches them, why it is generated here rather than fetched from Daftra,
and how to change the behaviour.

## Why the PDF is generated locally

Daftra v2 has **no PDF export endpoint**. The only file URL in the invoice
payload is `invoice_pdf_url`, and it is **session-gated** — it redirects to the
login page for anything that is not an authenticated browser session. That rules
out both options that would otherwise be obvious:

- **Meta cannot fetch it.** In `link` mode Meta downloads the `link` itself, with
  no session, and gets an HTML login page instead of a PDF.
- **We cannot fetch it either.** The Daftra API key is not a browser session, so
  the same redirect applies to the sender.

So the sender renders the PDF from the invoice data it already has and hands the
bytes to Meta directly. The result is a real `media_id` in the template's header
`document` object, which is the only form that works with a session-gated source.

## Modes

| Mode | `document` object | What it needs |
|---|---|---|
| `upload` *(default)* | `{"id": "<media_id>", "filename": "<no>.pdf"}` | nothing — the default just works |
| `link` | `{"link": "<public url>", "filename": "<no>.pdf"}` | a PDF reachable **without a login** |
| `none` | *no header component at all* | — |

```bash
python -m sender preview --invoice-id 1 --attachment link   # one run
python -m sender poll --attachment none                     # one poller
```

| Env var | Default | Meaning |
|---|---|---|
| `INVOICE_ATTACHMENT` | `upload` | `upload` / `link` / `none` (aliases: `hosted`, `url` → `link`; `off`, `disabled` → `none`) |
| `INVOICE_ATTACHMENT_CAPTION` | *(empty)* | optional document caption — see the warning below |

`--attachment` overrides the environment variable for a single run. A
misspelled value is rejected with an error rather than silently defaulting.

The clean (`--builder new`) template has no header document, so it attaches
nothing regardless of the mode.

## The PDF itself

`sender/infrastructure/pdf.py` writes the file with the standard library only —
no PDF library, keeping the project's two-dependency footprint (see
[design.md](../design.md) #7). It writes a correct cross-reference table and uses
two fonts:

| Font | Used for | Embedded? |
|---|---|---|
| standard-14 `Helvetica`, `/WinAnsiEncoding` | digits, dates, invoice numbers, currency codes, Latin text in a name | no — 14 bytes of dictionary |
| `Identity-H` `CIDFontType2` + `FontFile2` | all Arabic, including every contextual form and lam-alef ligature | yes — 87 KB subset, Flate-compressed |

The document contains the invoice number, issue date, customer, currency,
subtotal / total / paid / balance due, the invoice's own description, the line
items (name, description, quantity, unit price, line total), and paginates when
the item list is long, repeating the column headings on every page.

### The two free-text fields

Daftra lets a seller write prose in two places, and both reach the page:

| Where | Daftra field | Model field | Drawn as |
|---|---|---|---|
| the invoice | `notes` | `Invoice.description` | one labelled, wrapped paragraph (`ملاحظات`) under the meta rows, above the products |
| one line item | `InvoiceItem.description` | `InvoiceItem.description` | a second line under the item's name, in the name column |

They are **wrapped, never fitted**. This is the one thing that separates them from
every other free field on the page: a name or a customer is `_fit`-shortened to
its column with `...`, because a shortened name still says which product it is.
A description is a sentence — a delivery instruction, a size, a payment term —
and ellipsising one silently deletes the half that made it worth writing, with
nothing on the page to say anything was cut. So `_wrap` in `pdf.py` breaks these
two fields at word boundaries and draws every line, and a word too wide for its
column (a URL, a long code) is broken by character rather than allowed to run into
the neighbouring column.

The two are separate fields on purpose: `notes` is about the invoice and is
printed once above the products, because a customer reading the items first would
never reach a note placed below them. The line description is about one row, so it
sits inside that row, under its name, in the secondary ink — at body ink a
paragraph under every item name would be the loudest thing on the page, and the
figures are what a customer reconciles. A row with no description takes no extra
line at all, and neither field draws anything when it is empty: no label, no `-`,
and no line of air.

### Arabic and right-to-left

The invoice is laid out right-to-left: the headings, the field labels and the
table all anchor at the right margin, and the columns run inwards from it —
name, quantity, unit price, line total. Money is never truncated; a name too
wide for its column is shortened with `...` instead.

Arabic needs three steps a PDF viewer will not do for us, so the sender does
them before writing:

1. **Shape** — each character is resolved to its contextual form (initial,
   medial, final, isolated) from the joining behaviour of its neighbours, with
   lam-alef pairs collapsed to their ligature, so `لا` is one glyph.
2. **Reorder** — the runs are reversed so the page is drawn in visual order, and
   a Latin or digit run inside an Arabic value keeps its own order: a name like
   `رول فيلكس 45 سم` is drawn as three spans on one baseline.
3. **Map to glyph ids** — the shaped code points go through the embedded font's
   `cmap`, and those ids go into the content stream. The viewer is told the
   widths in `/W` and the reverse mapping in `/ToUnicode`, so the file is both
   correctly drawn and correctly copyable.

The font subset and its licence are documented in
[`sender/infrastructure/fonts/README.md`](../../sender/infrastructure/fonts/README.md).
Persian and Urdu letters that have no presentation form (پ, چ, گ) are drawn
unshaped — they have no other option without a full OpenType shaper — and are
reported in the log, once per value.

### Hierarchy, palette and the table

The page is black-and-white-first: its hierarchy is carried by size and ink
density, colour second. Every colour is chosen so that grayscale printing keeps
the levels distinct. The constants live at the top of `pdf.py` with their luma
(`Y = 0.299R + 0.587G + 0.114B`) in a comment:

| Level | RGB | Y | Job |
|---|---|---|---|
| `_INK` | 0.11 0.13 0.15 | 0.13 | titles, figures, item names |
| `_ACCENT` | 0.05 0.33 0.36 | 0.25 | the one accent: table header rule, balance due |
| `_SOFT` | 0.44 0.46 0.48 | 0.46 | secondary: labels, meta values, footer |
| `_HAIRLINE` | 0.79 0.81 0.83 | 0.81 | the rule under the title block |
| `_BAND` | 0.93 0.945 0.95 | 0.94 | table header band |
| `_ZEBRA` | 0.965 0.97 0.972 | 0.97 | alternate item rows |

One 13pt leading runs the whole page, so the baselines form a single grid. The
sizes on it: title 20, section heading 10.5, meta and table 9, totals 9.5, the
balance due 12.5 (its label 10.5), footer 8. **There is no Helvetica-Bold.**
Emphasis is size and colour; the balance due is the one text set with `2 Tr`
(fill and stroke) — a faux bold that needs no second width table, no third font
and no change to the fixed object numbering. Stroking the calligraphic naskh
face would clog its joins, so Arabic is never stroked.

- **Status chip.** The payment status (`Draft`, `Unpaid`, `Partially Paid`,
  `Paid`, `Refunded`, `Overpaid`, model default `Unknown`) renders as an Arabic
  chip beside the title, in one of six deliberate hues; the fill is the status
  colour pre-mixed most of the way to white (no alpha blend, so no `ExtGState`
  resource). A status this table has never seen — Daftra can return arbitrary
  title-cased strings like `On Hold` — is **shown verbatim** on a neutral chip:
  hiding operator-visible data would be worse than a Latin word on the page.
- **Page numbers.** Every page ends with `صفحة 1 من 2`, centred, in `_SOFT`.
  Content stops 18pt above the bottom margin so the number never crowds the last
  row. The repeated table header on a continuation page drops 14pt below the top
  margin so its band stays inside it.
- **Decimal-axis alignment.** Every figure in a column has its decimal point on
  the column's axis: the axis is the column's right edge less one two-decimal
  fraction, and the figure hangs off it by the width of its integer part. Money
  is always two decimals, so its right edge lands exactly on the column edge —
  the position the geometry is documented in terms of — while quantities with a
  varying number of decimals (`2`, `2.50`, `0.125`) and the totals block set
  across two sizes all line their points up. Money is never padded or shortened
  to get here; only the pen position moves.
- **Band, accent rule, zebra.** The table header sits on a pale band with a 1pt
  accent rule under it; alternate item rows carry a zebra tint. There are no
  vertical column rules: the columns are edge-adjacent by construction (the unit
  price's right edge *is* the quantity's left edge), so a separator would run
  its line straight through the last digit of every figure beside it. A zebra
  band is measured **downward from its own row's baseline** and grows with the
  row, never upward: a band anchored above its baseline still clears a 13pt row,
  but a row with a description under it is several lines tall, and such a band
  lands on the *previous* item's description — which an opaque fill erases, with
  nothing in the file to say the words are gone.

The layout's numbers are the constants in the geometry block of `pdf.py`
(`_TITLE_Y`, `_HEADING_AIR`, `_BAND_*`, `_STRIPE_*`, `_TOTALS_*`,
`_CONTINUE_TOP`, …), each with a comment saying why it is what it is. If you
change one, the tests that pin the columns run from the right margin inwards
and the fills-sit-behind-text order will tell you.

### Brand header and logo

Page 1 opens with a brand header: the committed logo
(`sender/infrastructure/assets/logo.png`, a 360×360 RGBA PNG, black on a
transparent background), the brand name **Aizen Paper**, the invoice
title, and the status chip — with an accent rule closing the block. The logo is
always included on page 1 (it is not repeated on continuation pages).

The brand is a registered Latin wordmark and is set in Helvetica, never
transliterated: it used to be drawn as `أوراق عايزن` in the Arabic face, which is
now a pinned regression (`test_the_brand_wordmark_is_never_transliterated`).

The PDF writer embeds it with the **standard library only** (no Pillow, keeping
the two-dependency footprint): a pure-stdlib PNG decoder in
`sender/infrastructure/png.py` parses `IHDR`, inflates `IDAT`, reverses the
per-row filters (types 0–4), and splits the alpha channel out. The colour
scanlines are Flate-compressed into an image XObject
(`/Subtype /Image`, `/ColorSpace /DeviceRGB`, `/BitsPerComponent 8`); the alpha
becomes a second, DeviceGray image XObject referenced as the colour image's
`/SMask` (PDF 1.4 transparency) — so the transparent background does not render
as a black square. The committed 360×360 RGBA asset decodes byte-for-byte
identically to a reference decoder (Pillow on the build machine, used only for
that one-off cross-check, never at runtime). To replace the logo, overwrite the
committed PNG at `sender/infrastructure/assets/logo.png` with another 8-bit RGBA
PNG of the same name; the image objects are inserted after the fonts in the
fixed object numbering (see the table in `_assemble`).

### Text that cannot be drawn

Neither font has a glyph for CJK, emoji, or most other scripts. Rather than
emit mojibake, those characters are dropped, the affected field is replaced with
`-` if nothing is left, and a WARNING names the field and the characters that
were lost:

```
WARNING invoice PDF: dropped 6 character(s) that neither the standard-14 font
nor the Arabic font can render (not WinAnsi, no glyph in the font's cmap) from
customer name: U+4E2D U+6587 U+1F600; the PDF shows the text without them
```

Arabic itself never warns: every Arabic letter in the invoice is drawn, shaped
and joined. Both free-text fields are covered by this tripwire and by the
unjoined-letter report below, exactly like the customer name and the item names:
a description is operator-supplied text arriving by the same door and must not be
the one field that quietly draws half-joined.

### An invoice with a total and no products

The empty items table is a designed state, so it renders (`لا توجد بنود في هذه
الفاتورة`) rather than fails. But an invoice that has a **non-zero total** and
no rows is never what was meant, and the document is a statement of what the
customer is being billed for — so it warns:

```
WARNING invoice PDF: invoice 000002 totals 105,000.00 but carries no line
items; the document will state that the invoice has no products. This is what a
Daftra list row (GET /invoices.json) produces on its own — it carries no
InvoiceItem, so the invoice must be re-fetched from GET /invoices/{id}.json
before rendering.
```

The most common cause is rendering an invoice that came from the **listing**
endpoint rather than the detail endpoint. `GET /invoices.json` embeds no
`InvoiceItem`, so a row from it has nothing to draw. The poller re-fetches the
detail before it sends, so this warning appearing in production means that path
was bypassed — check `sender/application/poller.py::_needs_detail` before
suspecting the mapper. The check is deliberately narrow (a zero total stays
quiet), so a genuinely empty invoice does not cry wolf on every send.

## Checking a PDF by hand

The writer is hand-rolled, so the output is worth inspecting rather than
trusting. Rendering one needs no account and no network:

```bash
python - <<'PY'
from datetime import date
from decimal import Decimal
from pathlib import Path
from sender.domain.models import Invoice, InvoiceItem
from sender.infrastructure.pdf import render_invoice_pdf

invoice = Invoice(
    id="1", number="INV-001", currency="EGP", issue_date=date(2026, 3, 14),
    customer_name="شركة النور للديكور",
    public_url="https://example.test/i/1", pdf_url="https://example.test/i/1.pdf",
    subtotal=Decimal("100.00"), total=Decimal("100.00"),
    total_paid=Decimal("0.00"), balance_due=Decimal("100.00"),
    items=(InvoiceItem(name="ورق حائط كلاسيك", quantity=Decimal("2"),
                       unit_price=Decimal("50.00"), total=Decimal("100.00")),),
)
Path("/tmp/out.pdf").write_bytes(render_invoice_pdf(invoice))
PY
```

`python -m sender preview` renders the same PDF but sends the bytes to Meta (or
discards them in `link` mode) — it never leaves a file behind, which is why the
snippet above writes one.

Then the four checks, all offline:

```bash
qpdf --check /tmp/out.pdf          # structure: no syntax or stream errors
pdffonts /tmp/out.pdf              # both fonts, the Arabic one embedded and subset
pdftotext -layout /tmp/out.pdf -   # text extraction, for the copy-and-paste check
pdftoppm -r 100 -png /tmp/out.pdf /tmp/page   # raster, to see what a customer sees
```

`pdffonts` is the one to read closely: the Arabic font must show `emb yes`,
`sub yes` and `uni yes` (the last is the `/ToUnicode` map — without it the text
cannot be copied out of the PDF). `pdftotext` output is expected to contain
Arabic **presentation forms** (ﻓﺎﺗﻮﺭﺓ, not فَاتُورَة) because that is what is
actually drawn; that is the correct result, not a bug.

## Upload protocol

`sender/infrastructure/whatsapp/media.py` uploads with a single
`POST /{version}/{phone-number-id}/media` request carrying
`multipart/form-data`: the required `messaging_product=whatsapp` field and the
PDF as the `file` part (filename included, `Content-Type: application/pdf`).
The response is `{"id": "<media_id>"}`; a response without an `id` is a hard
error — there is nothing to reference.

(The Graph API's two-phase *resumable* upload with `file_offset` is a separate
protocol for template-header assets and profile pictures; it is not the
send-time media endpoint, and its `h` handle is not a media id.)

Uploaded ids are cached per invoice *and* per PDF digest, so previewing and then
sending the same invoice uploads once, and an invoice whose totals changed
uploads again instead of reusing a stale id.

> **Note on `caption`.** Meta's reference documentation states that document
> captions are **not supported** for the document header parameter. A configured
> `INVOICE_ATTACHMENT_CAPTION` is therefore dropped with a WARNING instead of
> being sent, so a misconfiguration cannot reject the send.

## Failure behaviour

An attachment problem is never allowed to take a notification down.

- **No usable URL in `link` mode** → the header component is dropped, a WARNING
  is logged, and the message still sends.
- **Upload fails** → same. The exception is caught at the builder boundary.
- **Meta then rejects the header-less send** with `132012`; `sender send` already
  retries once with the clean builder, which has no header.

Inside `poll`, an upload failure raises `WhatsAppApiError`, so the poller applies
its normal classification: transient failures (network, `429`, `5xx`) are retried
with backoff and never lost, permanent ones are recorded as `abandoned`.

## Verifying it without sending

```bash
python -m sender preview --invoice-id 1                    # renders + uploads, never sends
python -m sender preview --invoice-id 1 --attachment link  # no upload
python -m sender preview --invoice-id 1 --invoice-stub    # offline: stub invoice, no upload
python -m sender send --invoice-id 1 --dry-run
```

`preview` builds the real payload, so in `upload` mode it does upload the PDF to
get a real `media_id` — the payload has to reference a genuine id to be worth
inspecting. It never sends a WhatsApp message. `--attachment link` skips the
upload; add `--stub` to skip Daftra as well, which is the only combination that
makes no network call at all.

`--stub` runs stay offline by default: an unspecified mode becomes `link`. An
explicit `--attachment upload` is honored, and warns.
