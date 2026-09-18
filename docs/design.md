# Design decisions

**Breadcrumb:** [Home](../index.md) / [Design](design.md)

---

This page records *why* the code is shaped the way it is, for future readers (and
future selves).

## 1. Super-simplified DDD, layered

A full-blown domain-driven design (aggregates, repositories, events, CQRS…) would
be overkill for a 2-adapter, 4-command CLI. We kept only the parts that pay for
themselves:

- a **pure domain** that encodes the WhatsApp template contract and the phone
  normalization rule — the two business rules that matter most;
- **ports** (protocols) between the use case and the outside world, so the HTTP
  adapters can be swapped or stubbed without touching application logic;
- a **composition root** in `presentation` that does the only manual wiring.

This is the "super-simplified" variant: single package, no split repositories, no
service locator, no events.

## 2. External truth stays external

The Daftra invoice is the **source of truth**; we never copy its unchanging
business rules into our code. Daftra's schemas could change tomorrow — that is why
the `DaftraInvoiceMapper` is an isolated, thin translation layer between the
wobbly wire format and the stable internal `Invoice` model. `show --raw` exists
exactly to inspect the live wire format when troubleshooting.

Internally we intentionally model only what **outbound notifications need**
(`Invoice`, `InvoiceItem`, phone, totals, date, status label) — there is no
attempt to mirror Daftra's entire ERP.

## 3. Why a template builder for an existing template?

The WhatsApp template `aizen_invoice` is **already approved and owned by Meta** —
this code never creates templates. But Meta's API requires that every
business-initiated send *parametrize* the template: give `name`, `language.code`,
and an ordered `components[].parameters[]` array that fills `{{1}}`–`{{4}}`.

`InvoiceTemplateBuilder` is the single place that owns that contract:

- maps `Invoice` → the 4 parameters **in the exact order** the approved body
  expects (customer name, invoice number, date, total);
- formats values the way the template requires (date `DD/MM/YYYY`, money like
  `1,500.00` with no currency symbol, since `ج.م` is hardcoded in the body);
- sanitizes text (collapse whitespace/newlines, cap at 512 chars, fallback `-`),
  because Meta rejects raw multi-line or oversized text params;
- documents the body next to the parameter logic (`TEMPLATE_BODY` at the top of
  the class), so a template re-approval or placeholder reordering is visible as a
  one-file diff — a mismatch otherwise surfaces only as a runtime `132000`-series
  error from Meta.

## 4. Phone normalization is a domain rule

Egyptian business logic: customers usually store local numbers like
`01027693262`. Meta requires E.164 (`201027693262`). The rule lives in
`domain/phones.py` and is applied consistently by both the builder (payload `to`
field) and the sender (defense-in-depth). `DEFAULT_COUNTRY_CODE=20` is
configurable via env. See the [domain](layers/domain.md) page.

## 5. Safe by default

- **`--dry-run` / `WHATSAPP_DRY_RUN`**: build and print the payload, never call
  Meta.
- **Lazy WhatsApp credentials**: `show`/`list` require only a Daftra key.
- **Secrets**: never hardcoded; come from env; hidden in `repr`;
  `.env`/`test curl.txt` are gitignored.
- **Money**: `Decimal` throughout — never floats for currency.
- **Immutable models**: frozen dataclasses, so payloads can't be mutated midway.

## 6. Meta constraints baked in

- Business-initiated messages *must* use a template; a freeform plain-text path
  exists for layout testing while template approval is pending (see `--freeform`).
- In test mode, recipients are limited to the **≤5 verified phone numbers**; the
  recipient's number must appear in test numbers or `recipient_type` fails.
- The language code must match the template's approved language (`en_EG`).
- Retries are restricted to HTTP 429 (rate limit), which is the one transient,
  safe-to-retry class of failure.

## 7. Minimalism

Standard library + two tiny dependencies (`requests`, `python-dotenv`), Python
≥3.10, no async, no framework. The whole system is a ~600-line package on purpose:
a small moving surface is easier to audit, test, and throw away.

## Back to

- [Overview](../index.md)
- [Architecture](architecture.md)
- [Next: getting started](guide/getting-started.md)