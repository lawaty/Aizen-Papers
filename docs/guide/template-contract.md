# WhatsApp template contract — `aizen_invoice`

**Breadcrumb:** [Home](../index.md) / [Guide](.) / [Template contract](template-contract.md)

---

This page is the ground truth for how our payload must look so Meta accepts the
business-initiated message. It mirrors `TEMPLATE_BODY` inside
`sender/domain/templates.py` — **keep the two in sync**.

## Builder auto-switching

There are (initially) **two** template builders implementing the same
`InvoiceTemplateBuilder` strategy (`sender/domain/templates.py`):

| Builder | Payload | When it is used |
|---|---|---|
| `LegacyInvoiceTemplateBuilder` | header DOCUMENT + body (see below) | **Now**, while the reviewed template is `PENDING` — Meta still serves the old approved document-header version |
| `CleanTextTemplateBuilder` | body only (no media header, no PDF needed) | Once the reviewed template is `APPROVED` |

Selection is automatic: `TemplateRegistry`
(`sender/infrastructure/whatsapp/template_registry.py`) queries the Graph API for
the template status, caches the result to `template_state.json` (gitignored)
and returns the matching builder. While `PENDING` the check is repeated on every
send; once approved (within the TTL, see `WHATSAPP_TEMPLATE_TTL`) it switches to
the clean builder and marks the legacy one deprecated. Failures fall back to the
cached state, or to the legacy builder when there is no cache.

Control:

```bash
python -m sender template-status                 # cached/current state
python -m sender template-status --force         # force a live refresh
python -m sender template-watch --interval 60 --timeout 3600   # poll until approved, exit 0
python -m sender template-drop-legacy            # confirm legacy is deprecated (once approved)
python -m sender send --invoice-id 1 --stub --builder new      # pin a builder manually
```

| Env var | Default | Meaning |
|---|---|---|
| `WHATSAPP_TEMPLATE_BUILDER` | `auto` | `auto` / `legacy` / `new` |
| `WHATSAPP_TEMPLATE_CACHE` | `template_state.json` | cache file (gitignored) |
| `WHATSAPP_TEMPLATE_TTL` | `300` | seconds before re-checking the status |

## While the template is pending approval (free-form fallback)

**The account this was built against has no approved `aizen_invoice` template in
the `en` translation at all**, so every real template send comes back with
`132001` (*template does not exist for the language `en`*). The 24-hour window
below is the hard limit that bridge lives under: Meta only delivers **free-form
(non-template) messages inside the 24-hour customer service window** — outside it
the fallback is rejected with `131047` too (see the
[error table](delivery-status.md#error-codes-to-know)). The fallback gets the
pipeline exercised end to end until the template is approved; it is not a
long-term delivery mode.

**Trigger.** Any send rejected with a `132000`-series code
(`domain.errors.is_template_error`: `132000 <= code < 133000`) — `132001` (no
usable template/translation), `132000` (parameter mismatch), `132012` (missing
header document). The poller then:

- **Reuses the header document that was already uploaded.** The rejected send
  referenced a real `media_id` — the PDF was rendered and uploaded *before* Meta
  looked at the template, and that asset is still live. The fallback sends a
  plain `type: document` message pointing at the same id: no re-render, no second
  upload, no caption (caption support on a free-form document is unverified, the
  same reason `INVOICE_ATTACHMENT_CAPTION` is dropped from a template header).
  A `link` document is *not* reused — Meta would have to fetch it, and Daftra's
  own PDF url is session-gated.
- **Otherwise sends plain text** — the exact payload `send --freeform` produces
  (same Arabic body, one builder, `TEMPLATE_BODY`).
- **Marks the invoice seen and counts it** as `fallback_sends` in the poll
  summary, with a WARNING naming the failing code.
- **Never abandons the invoice.** If the fallback itself fails (e.g. `131047`
  outside the window) or the fallback is switched off, the invoice stays
  `pending` with the *template* error recorded — the operator-actionable one —
  so it goes out once the template is fixed. A bridge must never consume
  invoices.
- **Never fires in `--dry-run`**: a dry run does not call the sender at all.

**Kill switch.** `WHATSAPP_FREEFORM_FALLBACK=off` (`0`/`false`/`no`; unset means
on) restores the old behaviour: the invoice is kept `pending` on the template
error, never sent free-form.

```bash
python -m sender send --invoice-id 1 --freeform        # same text, by hand
WHATSAPP_FREEFORM_FALLBACK=off python -m sender poll --once   # templates only
```

**Remediation (the actual fix).** In Meta Business Manager → WhatsApp Manager →
Message templates, create `aizen_invoice` with **4 body placeholders** matching
the variable ↔ parameter mapping below and a translation whose **language code
matches `WHATSAPP_TEMPLATE_LANG` exactly** (`en` here — a translation under any
other code is why `132001` comes back), then get it approved.
`python -m sender template-status --force` confirms it; the fallback stops firing
on its own from the next send, because nothing is rejected with a `132000`-series
code any more.

## The approved body

Language: `en` (English). Body text (as registered in Meta's template manager):

```
مَرْحَبًا {{1}}، 👋

نُحيطُكم عِلمًا بأنَّه تمَّ إصدار فاتورة جديدة من Aizen Paper.

📄 رقم الفاتورة: {{2}}
📅 تاريخ الإصدار: {{3}}
💰 إجمالي الفاتورة: {{4}} ج.م

شُكرًا لثقتكم الغالية، ونَسعد دائمًا باستمرار تعاونكم معنا. 🤝

Aizen Paper
✨ ثِقتكم مَحلُّ تقديرنا دائمًا.
```

Exactly **4 placeholders**, in this order.

## Variable ↔ parameter mapping

| Placeholder | Source (`Invoice` field) | Formatting | Example |
|---|---|---|---|
| `{{1}}` | `customer_name` | whitespace-collapsed, ≤512 chars, `-` if missing | `Ahmed Hassan` |
| `{{2}}` | `number` | as-is, sanitized | `INV-001` |
| `{{3}}` | `issue_date` | `DD/MM/YYYY` (or `N/A`) | `01/09/2026` |
| `{{4}}` | `total` | `f"{total:,.2f}"`, **no currency** | `1,500.00` |

Note: `{{4}}` must **not** include the currency — `ج.م` is hardcoded in the body.
Including it produces `1,500.00 ج.م ج.م`.

## Bidi isolation of parameters

Every parameter value is wrapped in a **Unicode bidi isolate** pair inside
`parameters()` (`sender/domain/templates.py`) so a numeric/latin run cannot be
reordered by the paragraph's RTL base direction:

| Value | Opener | Codepoint |
|---|---|---|
| `customer_name` | FSI (first-strong isolate, adapts to Arabic or Latin) | `U+2068` |
| `number`, `issue_date`, `total` | LRI (left-to-right isolate) | `U+2066` |
| all | PDI (pop directional isolate, closer) | `U+2069` |

Example: `INV-001` is sent as `LRI INV-001 PDI` (`\u2066INV-001\u2069`). The marks
are invisible (`Cf` category) and are not treated as whitespace. `parameters()`
sanitizes and truncates the value **first** and adds the pair afterwards,
reserving those two characters *inside* the 512-character cap — wrapping first
spent the budget on marks the value does not own and could cut the closing PDI
off a long name, leaving the rest of the message unterminated.
The phone (ICU) renders these exactly as the plain value;
the DOM-based WhatsApp Web renderer treats each isolated value as a self-contained
island (the same technique W3C recommends and WooCommerce used to fix reversed
RTL prices) so digits keep their order on every client.

## Header document (legacy builder only)

The **legacy** template carries a **header component with a DOCUMENT variable** —
it is **required** on every send of the current (old approved) revision. Missing
it fails with `132012` (`header: Format mismatch, expected DOCUMENT`).

The document itself is produced by the configured **attachment provider** (see
[Invoice PDF](invoice-pdf.md) for the full guide). The filename is always
`<invoice.number>.pdf` (e.g. `INV-001.pdf`), sanitized to a single path segment.
Which document fields appear depends on the mode:

| Mode | `document` object | Needs |
|---|---|---|
| `upload` (default) | `{"id": "<media_id>", "filename": "…"}` | nothing — the PDF is rendered and uploaded here |
| `link` | `{"link": "<public url>", "filename": "…"}` | a PDF reachable without a login |
| `none` | *no header component* | — |

In `link` mode the URL is `Invoice.pdf_url` (`invoice_pdf_url` in the Daftra
payload) with `Invoice.public_url` as a fallback for sources that expose no PDF
URL at all. With **no** provider configured the builder keeps the historic
behaviour and raises `ValueError` *before* any HTTP call when neither URL is set.

If a provider is configured but cannot produce a document — no URL, or the
upload failed — the header component is **dropped** and a WARNING is logged, so a
media outage degrades one notification instead of failing the poll cycle. Meta
then rejects that send with `132012`, which `sender send` already retries once
with the clean builder.

The **clean** builder omits the header entirely — the payload's `components` list
holds only the body. No PDF is attached.

## What the send payload looks like

Legacy builder in the default `upload` mode (current, while PENDING):

```json
{
  "messaging_product": "whatsapp",
  "recipient_type": "individual",
  "to": "201027693262",
  "type": "template",
  "template": {
    "name": "aizen_invoice",
    "language": { "code": "en" },
    "components": [
      {
        "type": "header",
        "parameters": [
          {
            "type": "document",
            "document": {
              "id": "1586283890123456",
              "filename": "INV-001.pdf"
            }
          }
        ]
      },
      { "type": "body", "parameters": [
        { "type": "text", "text": "\u2068Ahmed Hassan\u2069" },
        { "type": "text", "text": "\u2066INV-001\u2069" },
        { "type": "text", "text": "\u206601/09/2026\u2069" },
        { "type": "text", "text": "\u20661,500.00\u2069" }
      ]}
    ]
  }
}
```

Legacy builder in `link` mode — the `document` object is the `link` form:

```json
"document": { "link": "https://your-host/invoices/INV-001.pdf", "filename": "INV-001.pdf" }
```

Clean builder (once the review is approved) — identical except `components` is:

```json
[
  { "type": "body", "parameters": [
    { "type": "text", "text": "\u2068Ahmed Hassan\u2069" },
    { "type": "text", "text": "\u2066INV-001\u2069" },
    { "type": "text", "text": "\u206601/09/2026\u2069" },
    { "type": "text", "text": "\u20661,500.00\u2069" }
  ]}
]
```

`python -m sender preview --invoice-id <id>` prints exactly this JSON.

## Rules that cause Meta to reject a send

- **Placeholder count mismatch** — more or fewer than 4 body parameters.
- **Wrong order** — params must match `{{1}}`→`{{4}}` left to right.
- **Wrong language code** — must be `en` (what the template is registered as).
- **Missing header document** — mandatory for the legacy revision; omitting it
  returns `132012`. (Does not apply to the clean builder, which has no header.)
- **Unreachable document link** — in `link` mode Meta fetches the URL itself. A
  login-gated URL such as Daftra's own `invoice_pdf_url` fails here; use the
  default `upload` mode instead.
- **Non-E.164 recipient** — `to` must be international (`2010…`, not `010…`).
- **Unsanitized text** — parameters must not contain newlines/tabs/4+ spaces and
  must be ≤512 chars. Invisible bidi marks (LRM, RLM, LRI/FSI/PDI) are `Cf`
  characters, are **not** in Meta's deny-list, and are sent as-is.

These surface as `132000`-series error codes from the Cloud API. The builder's
sanitization and normalization exist to prevent as many as possible *before* the
HTTP call.

## Related

- [Getting started](getting-started.md)
- [Design decision #3 (why a builder)](../design.md)
- [Domain layer — templates](../layers/domain.md)