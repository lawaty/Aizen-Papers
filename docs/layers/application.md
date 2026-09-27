# Layer — application

**Breadcrumb:** [Home](../index.md) / [Architecture](../architecture.md) / [Layers](.) / [Application](application.md)

---

## Responsibility

The **use cases** — the "what the system does" without any concern for *how*
(which HTTP client, which API fields). Two classes live here:
`InvoiceNotificationService` (the one-shot send pipeline) and `InvoicePoller`
(the continuous watch-and-send loop).

## Contents (`sender/application/services.py`)

`InvoiceNotificationService` is constructed with three collaborators — all
injected, nothing looked up:

```python
InvoiceNotificationService(source, sender, builder)
```

| Collaborator | Role | Type |
|---|---|---|
| `source` | fetches invoices | `domain.ports.InvoiceSource` |
| `sender` | delivers messages | `domain.ports.MessageSender \| None` |
| `builder` | turns `Invoice` into a payload | `InvoiceTemplateBuilder` |

Because it depends only on `domain`, it never imports `infrastructure` — swap in a
stub source or sender for testing with no magic.

## Contents (`sender/application/poller.py`)

`InvoicePoller` is the polling use case. It is constructed with the apps to
watch, the sender, the builder, and the two injected ports the loop needs from
the outside world:

```python
InvoicePoller(apps, sender, builder, state, clock, *, interval, limit, dry_run, freeform_fallback, ...)
```

| Collaborator | Role | Type |
|---|---|---|
| `apps` | the Daftra tenants to poll, in order | `Sequence[PollApp]` (`name` + `InvoiceSource`) |
| `sender` | delivers messages | `domain.ports.MessageSender \| None` |
| `builder` | turns `Invoice` into a payload — and, on a template failure, into the free-form fallback | `InvoiceTemplateBuilder` |
| `state` | per-app "seen"/"pending"/"abandoned" bookkeeping | `domain.ports.PollStateStore` |
| `clock` | monotonic/wall time + sleep | `domain.ports.Clock` |
| `freeform_fallback` | fall back to a free-form message when the template is rejected (`WHATSAPP_FREEFORM_FALLBACK`) | `bool` (default `True`) |

`PollApp` is a tiny frozen dataclass pairing a stable app name with its
`InvoiceSource`; the CLI builds one per configured `DaftraApp`.

## Use cases exposed

| Method | Purpose |
|---|---|
| `get_invoice(id)` | normalize a single invoice |
| `get_raw_invoice(id)` | pass through the unmapped Daftra JSON (debugging) |
| `list_invoices(limit)` | list recent invoices |
| `build_message(invoice, to_phone)` | produce the payload dict for a given recipient |
| `preview_invoice(id, to?, freeform?)` | invoice + resolved recipient + payload, **no send**; `freeform=True` builds a plain-text payload |
| `send_invoice(id, to?, dry_run?)` | the full send path |
| `send_freeform(id, to?, dry_run?)` | the full send path using plain text instead of template |

`InvoicePoller` exposes:

| Method | Purpose |
|---|---|
| `run()` | run cycles until `--once`/`--max-cycles`/`--timeout`/a stop signal; returns the last cycle's summary |
| `run_once()` | one cycle over every app, in order; returns `{"apps": [...], "all_failed": bool}` |

## Sequencing rules owned here

- **Recipient resolution**: `--to` wins; otherwise the customer's own phone from
  the invoice — but only if it passed normalization. Missing/unsubscribe-able
  phone → clear `ValueError` telling the caller to pass `--to`.
- **`dry_run`**: if set, build everything, return `{"dry_run": True, ...}` and
  **never** call the sender.
- **Guard**: if `sender is None` (no WhatsApp credentials) the send path fails
  fast with a `RuntimeError` explaining the missing config.
- **Return shape**: `send_invoice` returns a dict carrying `invoice`, `payload`,
  `to`, and (when sent) the sender's `response` — so the CLI never re-fetches or
  re-builds.

The poller owns the poll-specific rules:

- **New = not in the per-app state store.** The list response is used only to
  detect candidate ids; if a candidate row already carries a usable phone it is
  used directly, otherwise `get_invoice(id)` fetches the authoritative detail.
  The list-vs-detail choice is purely about the phone: the template payload is
  identical either way (the list row already carries `no`, `date`,
  `currency_code`, the summary totals, `invoice_html_url`/`invoice_pdf_url`,
  and `Client`), so a list row can never produce a payload missing a field the
  active builder requires.
- **Recorded as seen only after the send attempt**, so a crash or failure never
  silently drops an invoice.
- **Failure classification** (see `_is_retryable`): retryable failures
  (network/timeout, HTTP 429, HTTP 5xx) are recorded as `pending` in the state
  and retried with a bounded exponential backoff — they are **never** given up.
  Permanent failures (validation, missing `public_url`, self-send) are given up
  on the first attempt, marked seen, and recorded as `abandoned` so an operator
  can see and re-drive them.
- **Template contract failures** (the `132000`-series, `domain.errors.
  is_template_error`) are the exception, because an operator — not a retry — is
  what fixes them. With `freeform_fallback` on, the invoice goes out as a
  free-form message: the already-uploaded header document is reused by its media
  id (no re-render, no second upload) when the rejected payload carried one,
  otherwise the builder's own text payload (`send --freeform`'s) is sent. The
  invoice is then marked seen and counted in `fallback_sends`. If the fallback
  send fails too, or the flag is off, the invoice is recorded as `pending` on the
  **template** error — never `abandoned`, never seen, so a bridge cannot consume
  an invoice. A `--dry-run` never reaches this path: it does not call the sender.
- **No usable phone → skip, log a WARNING with the invoice number, mark seen** —
  never retried forever, never sent to an unresolved number.
- **First run (no state for an app) seeds the state and sends nothing** unless
  `send_existing` is set — the highest-risk behaviour in the feature.
- **Error isolation**: one app raising (network, 401, …) is logged and the next
  app still runs; the cycle is `all_failed` only if every app failed.
- **Catch-up paging**: when a list page comes back full, the poller pages
  forward (bounded by `max_pages`) until it reaches already-seen territory, so
  a burst of new invoices between cycles cannot silently fall off the page. A
  saturated listing logs a WARNING suggesting a higher `--limit`.
- **Dry run is non-mutating**: payloads are built and logged, but **nothing** is
  written to the state — no invoice is marked seen, no `last_poll_at` is
  recorded. A real run afterwards sends everything the dry run rehearsed.

Business rules (template contract, phone normalization, money/date formatting)
belong to the domain; the services only *compose* them in the right order. That
keeps the orchestrators thin and the abstractions cheap to substitute.

## Back / drill down

- [Up: Architecture](../architecture.md)
- [Up: Documents](../index.md)
- [Previous layer: domain](domain.md)
- [Next layer: infrastructure](infrastructure.md)