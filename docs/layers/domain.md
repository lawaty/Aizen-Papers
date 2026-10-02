# Layer — domain

**Breadcrumb:** [Home](../index.md) / [Architecture](../architecture.md) / [Layers](.) / [Domain](domain.md)

---

## Responsibility

Pure business logic with **zero outbound imports** — no requests, no argparse, no
env. Only the Python standard library. This is where the two rules the business
actually cares about live: the WhatsApp template contract and phone normalization.

## Contents (`sender/domain/`)

| Module | What it holds |
|---|---|
| `models.py` | `Invoice`, `InvoiceItem` — frozen dataclasses (immutable) |
| `templates.py` | `InvoiceTemplateBuilder` strategy: `LegacyInvoiceTemplateBuilder` (document header) + `CleanTextTemplateBuilder` (body-only), sharing `TEMPLATE_BODY`, `parameters()`, and the freeform `build_text` |
| `attachments.py` | the invoice-PDF attachment contract: `document_filename()`, `pdf_link()`, and `HostedLinkProvider` (pure, no I/O) |
| `phones.py` | `normalize_phone()` — local → E.164 conversion & validation |
| `ports.py` | `InvoiceSource`, `MessageSender`, `MediaUploader`, `InvoiceAttachmentProvider`, `PollStateStore`, `Clock` — protocol interfaces |
| `errors.py` | exception hierarchy (`ApiError` → `DaftraApiError`, `WhatsAppApiError`) |

## The domain rules in one place

### 1. The template contract — `templates.py`

`TEMPLATE_BODY` documents the approved `en` template with its 4 placeholders.
The base `InvoiceTemplateBuilder` provides the shared parameter formatting and
raising `NotImplementedError` from `build()`; the two concrete strategies are:
`LegacyInvoiceTemplateBuilder` (header DOCUMENT + body — matches the currently
served, pre-review revision) and `CleanTextTemplateBuilder` (body only — used
after the reviewed template is approved). Each `build(invoice, to_phone)`
produces the full Meta payload, ordering the parameters **{{1}} name, {{2}}
number, {{3}} date, {{4}} total** exactly as the body expects, and
`parameters(invoice)` is the ordered list — kept as a separate method so it can
be asserted directly in tests. The active strategy is chosen by
`TemplateRegistry` (infrastructure) based on the template's Graph API status.

Formatting live here:

- `_format_date` → `DD/MM/YYYY` (or `N/A`),
- `_money` → `f"{total:,.2f}"` — **no** currency symbol,
- `_sanitize` → collapse whitespace, cap at 512 chars, `-` fallback. The
  bidirectional isolation pair is added *after* sanitizing and its two characters
  come out of the same 512-char budget (see the template contract).

### 2. Phone normalization — `phones.py`

```python
normalize_phone("01027693262", "20")      # → "201027693262"
normalize_phone("201027693262", "20")     # → "201027693262"  (idempotent)
```

Rules: strip separators, drop a leading `0` when the rest starts with the country
code region, prepend `DEFAULT_COUNTRY_CODE` if missing, validate 8–15 digits.
Rejects invalid input with a `ValueError`.

### 3. Ports — `ports.py`

Structural typing via `Protocol`, so adapters in `infrastructure` satisfy them
implicitly:

- `InvoiceSource`: `get_invoice(id) → Invoice`, `get_raw_invoice(id) → dict`,
  `list_invoices(limit, page) → list[Invoice]`.
- `MessageSender`: `send(payload: dict) → dict`.
- `MediaUploader`: `upload_pdf(pdf: bytes, filename: str, *, cache_key=None) → str`
  — hands the rendered PDF to the channel and returns an opaque media id.
  `cache_key` lets an adapter skip a re-upload of a byte-identical file.
  Implemented by `infrastructure/whatsapp/media.py`.
- `InvoiceAttachmentProvider`: `build(invoice) → dict | None` plus a `mode`
  attribute — returns the template header's `document` object, either
  `{"id": …}` (uploaded) or `{"link": …}` (hosted), optionally with `filename`
  (captions are not supported for document headers). `None` means "no attachment
  available". Implemented by `attachments.HostedLinkProvider` (domain) and
  `infrastructure/attachments.UploadedMediaProvider` (render + upload).
- `PollStateStore`: per-app record of which invoices were already handled —
  `has_app`, `app_names`, `seen`, `seen_ids`, `mark_seen`, `mark_many_seen`,
  `last_poll_at`, `set_last_poll_at`, `pending`, `abandoned`, `record_pending`,
  `record_abandoned`, `reset_app`, `clear_invoice`, `batch`. Implemented by
  `infrastructure/state.py` (JSON file or in-memory for tests).
- `Clock`: `monotonic()`, `now()`, `sleep(seconds)` — injected so the poll loop
  is testable without real time. Implemented by `infrastructure/clock.py`.

### 4. Models — `models.py`

`Invoice` is the normalized internal shape (money as `Decimal`, `date` objects,
explicit `status` string, customer phone) — deliberately *not* a mirror of
Daftra's schema. `InvoiceItem` holds line-item view-model data.

The two `description` fields are free text the seller typed, and they are
deliberately distinct: `Invoice.description` is Daftra's `notes` and belongs to
the document, while `InvoiceItem.description` belongs to one line. Both are
optional — `""` is the ordinary case and means "nothing was written", never "the
data was lost". `InvoiceItem.description` is declared **last** in the field order
because callers construct items positionally; inserting it next to `name` would
silently shift the three figures one place along. See
[Invoice PDF](../guide/invoice-pdf.md) for where each is drawn.

### 5. The attachment contract — `attachments.py`

`Invoice.pdf_url` is the invoice's real PDF (`invoice_pdf_url` in the Daftra
payload); `Invoice.public_url` is the human-facing page. `pdf_link()` picks the
PDF and falls back to the public URL only for sources that expose no PDF URL at
all. `document_filename()` sanitizes the invoice number into a single path
segment, because the number comes from an external ERP and lands in a
`MediaObject`.

`HostedLinkProvider` is the `link` half of the strategy and is pure domain — no
I/O, no HTTP. Its counterpart `UploadedMediaProvider` lives in
`infrastructure` because it renders a PDF and talks to Meta. See
[Invoice PDF](../guide/invoice-pdf.md).

### 6. Errors — `errors.py`

`ApiError` (status, message, optional body) with two specializations so callers
can catch precisely. HTTP/library details never leak as raw exceptions.

## Why this boundary matters

- The template contract can change in one place, visibly diffed.
- `normalize_phone` is applied both by the builder and defensively by the sender —
  the rule lives once in the domain instead of being duplicated per adapter.
- A unit test of this layer needs no network, no mocks, no env.

## Back / drill down

- [Up: Architecture](../architecture.md)
- [Up: Documents](../index.md)
- [Next layer: application](application.md)
- [Related: infrastructure](infrastructure.md), [presentation](presentation.md)