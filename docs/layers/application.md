# Layer — application

**Breadcrumb:** [Home](../../index.md) / [Architecture](../architecture.md) / [Layers](.) / [Application](application.md)

---

## Responsibility

The **use cases** — the "what the system does" without any concern for *how*
(which HTTP client, which API fields). One class, `InvoiceNotificationService`,
acts as the orchestration core of the whole tool.

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

Business rules (template contract, phone normalization, money/date formatting)
belong to the domain; the service only *composes* them in the right order. That
keeps the orchestrator thin and the abstractions cheap to substitute.

## Back / drill down

- [Up: Architecture](../architecture.md)
- [Up: Documents](../../index.md)
- [Previous layer: domain](domain.md)
- [Next layer: infrastructure](infrastructure.md)