# Layer — domain

**Breadcrumb:** [Home](../../index.md) / [Architecture](../architecture.md) / [Layers](.) / [Domain](domain.md)

---

## Responsibility

Pure business logic with **zero outbound imports** — no requests, no argparse, no
env. Only the Python standard library. This is where the two rules the business
actually cares about live: the WhatsApp template contract and phone normalization.

## Contents (`sender/domain/`)

| Module | What it holds |
|---|---|
| `models.py` | `Invoice`, `InvoiceItem` — frozen dataclasses (immutable) |
| `templates.py` | `InvoiceTemplateBuilder` + the literal approved `TEMPLATE_BODY` |
| `phones.py` | `normalize_phone()` — local → E.164 conversion & validation |
| `ports.py` | `InvoiceSource`, `MessageSender` — protocol interfaces |
| `errors.py` | exception hierarchy (`ApiError` → `DaftraApiError`, `WhatsAppApiError`) |

## The domain rules in one place

### 1. The template contract — `templates.py`

`TEMPLATE_BODY` documents the approved `en_EG` template with its 4 placeholders.
`InvoiceTemplateBuilder.build(invoice, to_phone)` produces the full Meta payload,
ordering the parameters **{{1}} name, {{2}} number, {{3}} date, {{4}} total**
exactly as the body expects, and `parameters(invoice)` is the ordered list — kept
as a separate method so it can be asserted directly in tests.

Formatting live here:

- `_format_date` → `DD/MM/YYYY` (or `N/A`),
- `_money` → `f"{total:,.2f}"` — **no** currency symbol,
- `_sanitize` → collapse whitespace, cap at 512 chars, `-` fallback.

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
  `list_invoices(limit) → list[Invoice]`.
- `MessageSender`: `send(payload: dict) → dict`.

### 4. Models — `models.py`

`Invoice` is the normalized internal shape (money as `Decimal`, `date` objects,
explicit `status` string, customer phone) — deliberately *not* a mirror of
Daftra's schema. `InvoiceItem` holds line-item view-model data.

### 5. Errors — `errors.py`

`ApiError` (status, message, optional body) with two specializations so callers
can catch precisely. HTTP/library details never leak as raw exceptions.

## Why this boundary matters

- The template contract can change in one place, visibly diffed.
- `normalize_phone` is applied both by the builder and defensively by the sender —
  the rule lives once in the domain instead of being duplicated per adapter.
- A unit test of this layer needs no network, no mocks, no env.

## Back / drill down

- [Up: Architecture](../architecture.md)
- [Up: Documents](../../index.md)
- [Next layer: application](application.md)
- [Related: infrastructure](infrastructure.md), [presentation](presentation.md)