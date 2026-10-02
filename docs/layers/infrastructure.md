# Layer — infrastructure

**Breadcrumb:** [Home](../index.md) / [Architecture](../architecture.md) / [Layers](.) / [Infrastructure](infrastructure.md)

---

## Responsibility

Everything that touches the outside world: HTTP calls, config parsing, JSON
handling. Adapts external reality to the domain's ports — nothing here decides
*business* rules, only *how* data moves.

## Contents (`sender/infrastructure/`)

### `daftra/client.py` — `DaftraClient`

Implements `domain.ports.InvoiceSource`. Owns the shared `requests.Session`, the
`apikey` authentication header, and one private HTTP helper used by all
endpoints. Every request is authorized with the Daftra API key sent as the
`apikey` header — the simplest supported method:

- `get_invoice(id)` → maps raw JSON through the mapper into an `Invoice`
- `get_raw_invoice(id)` → returns the unmapped JSON (raw debugging)
- `list_invoices(limit=10, page=1, filters=…)` → `GET /invoices.json`

The client removes stale `apikey` and `Authorization` headers from an injected
session before applying the key, so a reused session can never leak a stale
header alongside the key.

Behaviors: validates invoice ids are digits, raises
`DaftraApiError(code, message, body)` on network errors, non-2xx responses, or an
envelope whose `result` is not `successful`/`success`/absent.

### `daftra/mapper.py` — `DaftraInvoiceMapper`

The translation layer. Reads the Daftra envelope
(`{ result, code, data: { Invoice: { ..., Client, InvoiceItem[] } } }`) and the concrete
v2 field names (e.g. `summary_total`, `summary_paid`, `summary_unpaid`,
`payment_status`, `Client.phone1/phone2`, `InvoiceItem.item/quantity/unit_price`)
into the normalized `Invoice`/`InvoiceItem` model. It maps the two URLs onto
separate fields: `invoice_html_url` → `Invoice.public_url` (the human-facing page)
and `invoice_pdf_url` → `Invoice.pdf_url` (the actual PDF). `Client` and `InvoiceItem` are
nested *inside* the `Invoice` object by the live API; the mapper also accepts them
sitting beside `Invoice`, which is the shape the offline stub used to emit. Guards
against missing or malformed payloads, repairs booleans-as-ints, keeps a `0` invoice
number instead of silently dropping it, and parses money into `Decimal`, tolerating
`NaN` and decimal commas. Every filled client phone field is collected, not just
the first, so an invoice reaches a customer on both numbers when both are set;
duplicates are collapsed after normalization, and a filled-but-unparseable field
is dropped with a WARNING naming it rather than failing the whole document.
Customer phones are normalized with the deployment's
`DEFAULT_COUNTRY_CODE` (the mapper takes it as a constructor argument, default
`"20"`), so the mapper and the poller normalize in one and the same pass instead
of two passes that can disagree.

**The two endpoints do not carry the same fields, and that is load-bearing.**
`GET /invoices/{id}.json` returns `InvoiceItem[]`; `GET /invoices.json` (the
listing the poller pages over) returns **no `InvoiceItem` key at all**, so every
listed row maps to `items=()`. The mapper cannot tell an item-less invoice from
an un-fetched one, which is why the poller re-fetches the detail before it
renders (see [`application.md`](application.md)) and why `infrastructure/pdf.py`
warns on an invoice that has a total but no rows. A listing that starts
including items would make that fetch redundant, not wrong.

### `whatsapp/client.py` — `WhatsAppClient`

Implements `domain.ports.MessageSender`. Posts the JSON payload to
`https://graph.facebook.com/{api_version}/{phone_number_id}/messages` with a
Bearer token. Re-normalizes `to` before sending (defense-in-depth). Retries up to
`max_retries` only on HTTP **429**, honoring the `Retry-After` header (bounded
backoff otherwise), then fails with `WhatsAppApiError` — surfaced error messages
include the Meta `error.message`, `code`, and `error_data.details` for easier
triage.

### `whatsapp/media.py` — `MetaMediaUploader`

Implements `domain.ports.MediaUploader`. Uploads the rendered PDF with a single
`POST https://graph.facebook.com/{api_version}/{phone_number_id}/media` request
carrying `multipart/form-data`: the required `messaging_product=whatsapp` field
and the PDF as the `file` part (filename included, `Content-Type:
application/pdf`). The response is `{"id": "<media_id>"}`; a response without an
`id` is a hard error — there is nothing to reference. (The Graph API's two-phase
*resumable* upload with `file_offset` is a separate protocol for template-header
assets and profile pictures, not the send-time media endpoint.) Caches ids by
`cache_key` so a repeated send of an unchanged invoice does not upload twice.
Uses the same session/timeout/retry conventions as the send client, and raises
`WhatsAppApiError` so the poller classifies an upload failure exactly like a
send failure.

### `whatsapp/errors.py` — `to_api_error`

The one place that unpacks Meta's Graph API error envelope into a
`WhatsAppApiError` (message, `code`, `error_data.details`, `fbtrace_id`). Shared
by the send client and the media uploader, so every Meta endpoint reports and
classifies failures identically.

### `pdf.py` — `render_invoice_pdf`

Writes the invoice PDF with the standard library only — no PDF library, per
[design.md](../design.md) #7. Emits a correct cross-reference table and
paginates when the line items are long, repeating the column headings on each
page. Two fonts: standard-14 `Helvetica` with `/WinAnsiEncoding` for Latin text
and digits (nothing to embed), and an embedded `Identity-H` `CIDFontType2` built
from the committed font subset for Arabic, with the `/W` widths and `/ToUnicode`
map the drawn glyphs need. Text goes through shape → reorder → glyph-id mapping
first, so Arabic is drawn in its contextual forms, joined, and in visual order;
the table is laid out right-to-left from the right margin. Text neither font can
draw is dropped with a WARNING naming the field, and a value left empty renders
as `-`. Money is never truncated; a too-wide name is shortened with `...`.

### `arabic.py` — shaping and bidi

Everything Arabic that a PDF viewer will not do. Resolves each character to its
contextual form (initial / medial / final / isolated) from its neighbours'
joining behaviour, collapses lam-alef pairs to their ligature, strips harakat
that the font has no precomposed glyph for, and reports Arabic that has no
presentation form (Persian پ چ گ) as unshaped rather than pretending. Then a
simplified bidi pass: runs are classified RTL or LTR, merged, and reversed into
visual order, with brackets mirrored and a Latin or digit run inside an Arabic
value keeping its own order. Presentation forms are derived from the Unicode
character *names*, so the module carries no hardcoded shaping tables.

### `truetype.py` — the font reader

A pure-stdlib `sfnt` reader, so embedding a font costs no dependency. Parses the
table directory and the `head`, `hhea`, `hmtx`, `maxp` and `cmap` tables (formats
4 and 12), and answers the only three questions the writer asks: is there a glyph
for this code point, which glyph id is it, and how wide is it. Font bytes are
read once and cached.

### `attachments.py` — `UploadedMediaProvider`

Implements `domain.ports.InvoiceAttachmentProvider`. The default strategy: render
the PDF, upload it, and return `{"id": …, "filename": …}`. Its cache key combines
the invoice identity with the digest of the rendered PDF, so an invoice whose
totals changed is uploaded again instead of reusing a stale id. Returns `None`
(after a WARNING) when the render or upload yields nothing, which the builder
turns into a header-less send.

### `config.py` — `Settings`

Frozen dataclass read from env (`.env`). Normalizes floats, country code, and
the dry-run flag — every knob is centralized here. Secrets (`daftra_api_key`,
`wa_access_token`, and each `DaftraApp.api_key`) are `field(repr=False)`. Daftra
requires `DAFTRA_API_KEY` when a command needs the invoice source. WhatsApp
credentials are required **lazily**: `from_env(require_whatsapp=…)` only demands
them when a command actually needs the sender. All knobs have sane defaults
(Daftra base URL, template name `aizen_invoice`, lang `en`, country `20`, Graph
`v25.0`).

`Settings.apps` is a tuple of `DaftraApp` (frozen dataclass: `name`, `base_url`,
`api_key`, `timeout`) — one per tenant. App 1 is built from the unprefixed
`DAFTRA_API_KEY`/`DAFTRA_BASE_URL`/`DAFTRA_TIMEOUT`; extra apps come from
numbered slots (`DAFTRA2_BASE_URL` + `DAFTRA2_API_KEY`, `DAFTRA3_*`, …). Each
app's name is derived from its account subdomain. Slots 2–9 are scanned; a slot
missing either var is skipped (with a warning), and a gap (an empty slot before
a configured one) is warned about instead of silently dropping the later app.
Two apps that end up with the same name are warned about because they would
share poll state. Every app's base URL passes the same `_validate_daftra_url`
https + `.daftra.com`/`.daftara.com` check. The non-poll commands use
`Settings.primary_app` (the first/only app). `from_env` also accepts
`require_apps=True` so `poll` demands at least one fully configured app.

### `state.py` — `JsonPollStateStore` / `InMemoryPollStateStore` / `PollStateLock`

Implements `domain.ports.PollStateStore`. A JSON file (`poll_state.json` by
default) recording, **per app name**:

- `seen` — invoice ids already handled (sent, skipped, or permanently abandoned);
- `pending` — invoice ids with a retryable failure (network/429/5xx) that will
  be retried; each entry carries `count`, `action`, `error`, `last_attempt_at`,
  and `next_attempt_at` (bounded backoff);
- `abandoned` — invoice ids given up after a permanent failure; each entry
  carries `count`, `action`, `error`, and `last_attempt_at`;
- `last_poll_at` — wall-clock timestamp of the last completed cycle.

Written atomically via a **pid-qualified sibling temp file** +
`os.replace` (see `util.write_json_atomic`), so a crash mid-write cannot corrupt
the file and concurrent writers cannot clobber each other's temp files. A
corrupt or missing file logs a warning and starts empty. The seen set and the
pending/abandoned maps are bounded to the most recent `POLL_MAX_SEEN` entries so
a long-running poller does not grow the file forever. `batch()` defers saves to
one atomic write per poll cycle (a crash inside the batch re-sends, never
drops). `InMemoryPollStateStore` keeps the same bookkeeping in memory only.

`PollStateLock` is an advisory `flock` on a sibling `<state>.lock` file. The CLI
holds it for the whole real `poll` run so two overlapping pollers cannot
double-send; a second poller fails fast with a clear error. `flock` is released
by the OS on process exit, so a crashed poller cannot leave a stale lock.

### `clock.py` — `SystemClock`

Implements `domain.ports.Clock` (`monotonic`, `now`, `sleep`) with the real
`time` module. Injected into the poller so tests can substitute a fake clock and
never actually sleep.

### `util.py`

`json_or_none(response)` — parse a `Response` body as JSON without crashing;
shared by Daftra and WhatsApp code paths.

## Why adapters and config are both here

`presentation` may import these freely (it is the composition root), but
`application` must not. If Daftra ever changes its API shape, you fix
`daftra/mapper.py`; if Meta deprecates a Graph version, you bump
`infrastructure/whatsapp` + `Settings` — the application layer never moves.

## Back / drill down

- [Up: Architecture](../architecture.md)
- [Up: Documents](../index.md)
- [Previous layer: application](application.md)
- [Next layer: presentation](presentation.md)