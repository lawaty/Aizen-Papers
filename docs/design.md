# Design decisions

**Breadcrumb:** [Home](index.md) / [Design](design.md)

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

`InvoiceTemplateBuilder` is a small strategy: the shared base owns the 4-variable
formatting, and two concrete builders differ only in the payload's `components`
(`LegacyInvoiceTemplateBuilder` = header DOCUMENT + body, `CleanTextTemplateBuilder`
= body only). The chosen builder is the single place that owns the contract:

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

Which concrete builder runs is decided by `TemplateRegistry`
(`infrastructure/whatsapp/template_registry.py`), which queries the Graph API for
the template structure/status and tracks the **last approved revision's
structure**: while the reviewed `aizen_invoice` is `PENDING` the account still
serves the old approved document-header revision, so the legacy builder is used;
once the clean revision is `APPROVED`, sends switch to the clean builder and the
legacy one is marked deprecated. A later edit back into `PENDING` keeps the clean
builder, because the clean revision is what Meta serves then. The state is cached
(`template_state.json`) with a TTL; `PENDING` is always re-fetched on a send, and
a failed check falls back to the cache, then to the legacy builder. If a send is
rejected with a 132000-series parameter mismatch (meaning Meta did not send it),
the CLI force-refreshes the state and retries once with the other builder.

## 4. Phone normalization is a domain rule

Egyptian business logic: customers usually store local numbers like
`01027693262`. Meta requires E.164 (`201027693262`). The rule lives in
`domain/phones.py` and is applied consistently by both the builder (payload `to`
field) and the sender (defense-in-depth). `DEFAULT_COUNTRY_CODE=20` is
configurable via env. See the [domain](layers/domain.md) page.

## 5. Safe by default

- **`--dry-run` / `WHATSAPP_DRY_RUN`**: build and print the payload, never call
  Meta.
- **Lazy WhatsApp credentials**: `show`/`list` require only a configured Daftra auth mode.
- **Secrets**: never hardcoded; come from env; hidden in `repr`;
  `.env`/`test curl.txt` are gitignored.
- **Money**: `Decimal` throughout — never floats for currency.
- **Immutable models**: frozen dataclasses, so payloads can't be mutated midway.

## 6. Meta constraints baked in

- Business-initiated messages *must* use a template; a freeform plain-text path
  exists for layout testing while template approval is pending (see `--freeform`).
  While the template is genuinely unusable (`132000`-series rejection), the
  poller falls back to a free-form message so the pipeline is still exercised —
  a bridge, not a delivery mode: Meta only delivers free-form inside the 24-hour
  customer service window (`131047` outside it), and the invoice is never
  abandoned, only retried, until the template contract works
  (`docs/guide/template-contract.md`).
- In test mode, recipients are limited to the **≤5 verified phone numbers**; the
  recipient's number must appear in test numbers or `recipient_type` fails.
- The language code must match the template's approved language (`en`); the
  currently served revision also requires a header DOCUMENT parameter sourced
  from `invoice.public_url` (legacy builder, removed in the reviewed revision).
- Retries are restricted to HTTP 429 (rate limit), which is the one transient,
  safe-to-retry class of failure.

## 7. Minimalism

Standard library + two tiny dependencies (`requests`, `python-dotenv`), Python
≥3.10, no async, no framework. The system is a ~5,500-line package on purpose: a
small moving surface is easier to audit, test, and throw away.

"Small" is a statement about dependencies and blast radius, not a target to win
by deleting tests. Reading Arabic out of a font is ~900 lines of
[`truetype.py`](../sender/infrastructure/truetype.py),
[`arabic.py`](../sender/infrastructure/arabic.py) and the Arabic half of
[`pdf.py`](../sender/infrastructure/pdf.py), and it is the one place where
writing it by hand was the *only* option — see decision 8.

## 8. Why render the PDF ourselves?

Daftra v2 has no PDF export endpoint, and the only file URL it exposes,
`invoice_pdf_url`, is **session-gated** — it redirects to the login page without
an authenticated browser session. That closes both easy paths: Meta cannot fetch
it (in `link` mode Meta downloads the URL itself), and neither can the sender
(the API key is not a browser session).

So the sender writes the PDF from the invoice data it already has and hands the
bytes to Meta, producing a real `media_id`. Three consequences worth stating:

- **A PDF library would have been the easy answer, and was rejected.** The
  writer is ~720 lines of standard library, keeping the two-dependency
  footprint in decision 7. That is a deliberate trade: hand-rolled PDF is more
  code to own, and it is the part of this change most likely to need revisiting.
- **The standard-14 fonts are Latin-1 only, so the invoice carries an embedded
  Arabic font instead.** A viewer does no font lookup and no shaping: it draws
  the glyph ids it is given. So the sender resolves each Arabic character to its
  contextual form itself, writes those code points' glyph ids into an
  `Identity-H` `CIDFontType2` with a `FontFile2` stream, and reverses the runs.
  The font is a 87 KB subset of Noto Naskh Arabic, committed under the OFL next
  to the writer ([provenance and coverage](../sender/infrastructure/fonts/README.md)).
  Latin text — digits, dates, invoice numbers, currency codes — still goes to
  standard-14 Helvetica, which is why the subset contains almost no ASCII.
- **The consequence of hand-rolling is that shaping bugs are ours to find.**
  There is no library to blame and no viewer to fall back on, so the correctness
  of the result is pinned by tests against the font's own glyph ids
  ([`tests/test_arabic.py`](../tests/test_arabic.py),
  [`tests/test_truetype.py`](../tests/test_truetype.py),
  [`tests/test_pdf.py`](../tests/test_pdf.py)) and by the offline checks in
  [`docs/guide/invoice-pdf.md`](guide/invoice-pdf.md). Characters neither font
  can draw are still dropped with a WARNING naming the field and the lost code
  points; Arabic itself never warns.

## 9. Why is the attachment a port and not a flag?

`upload` and `link` differ in *who owns the bytes*, which is an infrastructure
question, so the choice is a domain port (`InvoiceAttachmentProvider`) with one
pure implementation (`HostedLinkProvider`) and one that renders and uploads
(`UploadedMediaProvider`). The template builder depends on the port, never on
Meta, and a mode that cannot produce a document degrades to a header-less send
instead of failing the notification — the poller must never die on an
attachment.

## 10. Why a poller?

The `poll` command turns the manual one-shot `send` into a continuously running
watcher. Four decisions shape it:

### Why poll at all?

Daftra has no push webhook for new invoices, so the only way to notice a new
invoice is to ask. Polling every `POLL_INTERVAL` seconds is the simplest
reliable mechanism: no server, no callback registration, no new dependency. The
same loop runs as a one-shot (`--once`) under cron/systemd, or as a long-lived
process with `--max-cycles`/`--timeout` bounds.

### Why per-app state?

Each Daftra tenant has its own invoice ids and its own customers. Sharing one
"seen" set between two tenants would make app 2 skip invoices that app 1 already
handled (or worse, re-send app 1's invoices). The state file is therefore keyed
**per app name**, and the app name is stable (derived from the account subdomain,
e.g. `aizenpaper`), so a tenant's history survives restarts and is never confused
with another tenant's.

### Why does the first run not send?

The very first poll of an app would otherwise see *every historical invoice* as
"new" and blast the whole customer base. That is the highest-risk behaviour in
the feature, so the default is the opposite: the first run **seeds** the state
with the currently existing invoices and sends nothing, logging exactly what it
did. `--send-existing` is the explicit, deliberate opt-in for a first run that
really does send them.

### Why sequential, not concurrent?

Two tenants share one WhatsApp sender and one state file. Running them
sequentially in configuration order keeps the cycle deterministic, keeps the
state writes serialized (no lock needed), and makes error isolation obvious: if
app 1 raises, it is logged and app 2 still runs in the same cycle. The exit code
after a cycle is non-zero only if *every* app failed. Concurrency would buy
nothing here — the bottleneck is the human-rate WhatsApp send, not the two list
calls.

### Why do retryable failures never give up?

The worst outcome for a payment-reminder flow is a **silently dropped invoice**.
So failures are classified: network/timeout, HTTP 429, and HTTP 5xx are
**retryable** — they are recorded as `pending` in the state and retried with a
bounded exponential backoff (`min(interval * 2^(count-1), POLL_MAX_BACKOFF)`),
never given up. Permanent failures (validation, missing `public_url`, template
rejection, self-send) can never succeed on retry, so they are given up on the
first attempt, marked seen, and recorded as `abandoned` — visible in
`poll-status` and re-drivable via `poll-reset --invoice-id`. A retryable failure
can therefore never become a silent permanent loss.

### Why is `--dry-run` non-mutating?

A rehearsal that changes real bookkeeping is a footgun: `poll --dry-run` used to
mark invoices seen, so the following real run sent nothing. Now `--dry-run` (and
`WHATSAPP_DRY_RUN=true`) builds and logs the payloads and writes **nothing** to
the state — no invoice is marked seen, no `last_poll_at` is recorded, and the state
file is not even created. The cycle summary counts these as `would_send`, never
as `sent`, so a rehearsal can never be mistaken for a delivery. A real run
afterwards sends everything the dry run rehearsed. This matches `send --dry-run`.

### Why a lock file?

Two overlapping pollers (a stray manual run over a cron/systemd `--once` run)
could both read the same state, both send the same invoice, and both write the
state. The real `poll` path therefore takes an advisory `flock` on
`poll_state.json.lock` for the whole run; a second poller fails fast with a
clear error. `flock` is released by the OS on process exit, so a crashed poller
cannot leave a stale lock. The `--stub` demo path is single-user and does not
lock.

### Why catch-up paging instead of a "newest-id watermark"?

A pure sliding window of seen ids misses invoices when more than `--limit` are
created between two cycles. A "newest-id watermark" in the state would let the
poller *detect* that it is behind, but it cannot *send* the missed invoices —
they are not on the first page. The poller therefore pages forward (bounded by
`POLL_MAX_PAGES`) until it reaches already-seen territory, which actually
delivers the missed invoices, and logs a WARNING when the listing comes back
full so the operator can raise `--limit`. A watermark alone would only turn a
silent loss into a detected loss; paging turns it into a delivered one.

## Back to

- [Overview](index.md)
- [Architecture](architecture.md)
- [Next: getting started](guide/getting-started.md)
- [Invoice PDF attachment](guide/invoice-pdf.md)