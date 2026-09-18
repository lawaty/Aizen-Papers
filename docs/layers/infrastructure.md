# Layer — infrastructure

**Breadcrumb:** [Home](../../index.md) / [Architecture](../architecture.md) / [Layers](.) / [Infrastructure](infrastructure.md)

---

## Responsibility

Everything that touches the outside world: HTTP calls, config parsing, JSON
handling. Adapts external reality to the domain's ports — nothing here decides
*business* rules, only *how* data moves.

## Contents (`sender/infrastructure/`)

### `daftra/client.py` — `DaftraClient`

Implements `domain.ports.InvoiceSource`. Owns the shared `requests.Session`, the
auth header (`apikey` by default, or `Authorization: Bearer` when a bearer token
is passed), and one private HTTP helper used by all endpoints:

- `get_invoice(id)` → maps raw JSON through the mapper into an `Invoice`
- `get_raw_invoice(id)` → returns the unmapped JSON (raw debugging)
- `list_invoices(limit=10, page=1, filters=…)` → `GET /invoices.json`

Behaviors: validates invoice ids are digits, raises
`DaftraApiError(code, message, body)` on network errors, non-2xx responses, or an
envelope whose `result` is not `successful`/`success`/absent.

### `daftra/mapper.py` — `DaftraInvoiceMapper`

The translation layer. Reads the Daftra envelope
(`{ result, code, data: { Invoice, Client, InvoiceItem[] } }`) and the concrete
v2 field names (e.g. `summary_total`, `summary_paid`, `summary_unpaid`,
`payment_status`, `Client.phone1/phone2`, `InvoiceItem.item/quantity/unit_price`)
into the normalized `Invoice`/`InvoiceItem` model. Guards against missing or
malformed payloads, repairs booleans-as-ints, keeps a `0` invoice number instead
of silently dropping it, and parses money into `Decimal`, tolerating `NaN` and
decimal commas.

### `whatsapp/client.py` — `WhatsAppClient`

Implements `domain.ports.MessageSender`. Posts the JSON payload to
`https://graph.facebook.com/{api_version}/{phone_number_id}/messages` with a
Bearer token. Re-normalizes `to` before sending (defense-in-depth). Retries up to
`max_retries` only on HTTP **429**, honoring the `Retry-After` header (bounded
backoff otherwise), then fails with `WhatsAppApiError` — surfaced error messages
include the Meta `error.message`, `code`, and `error_data.details` for easier
triage.

### `config.py` — `Settings`

Frozen dataclass read from env (`.env`). Normalizes floats, country code, and
the dry-run flag — every knob is centralized here. Secrets
(`daftra_api_key`, `wa_access_token`) are `field(repr=False)`. WhatsApp
credentials are required **lazily**: `from_env(require_whatsapp=…)` only demands
them when a command actually needs the sender. All knobs have sane defaults
(Daftra base URL, template name `aizen_invoice`, lang `ar`, country `20`,
Graph `v25.0`).

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
- [Up: Documents](../../index.md)
- [Previous layer: application](application.md)
- [Next layer: presentation](presentation.md)