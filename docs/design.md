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

## 11. Why a second pipeline for payments?

Payments are announced under a second approved Meta template,
`aizen_new_payment`, from `python -m sender poll-payments`. Five decisions shape
it, and every one of them is a case where the obvious thing was the wrong thing.

### Why a separate pipeline rather than one poller for both documents?

Separate state file, separate `flock`, separate send cap, separate cron line.
`InvoicePoller` and `PaymentPoller` are two processes that never coordinate.

The forcing reason is the id space: payment ids and invoice ids overlap in the
same account, so one shared "seen" set would have a payment confirmation retire an
invoice notification with the same number — or the reverse — and neither would
ever be retried. The second reason is starvation: with one shared
`POLL_MAX_SENDS_PER_RUN`, a payment backlog would eat the invoice pipeline's
budget on every tick, and the invoice message is the one the business is run on.
One cron line per pipeline is the cost, and `tools/run_poll.sh` takes
`POLL_SUBCOMMAND` so both share one wrapper and one log format.

### Why extract a shared engine instead of writing a second poller?

The invariants in §10 — the first run seeds without sending, a dry run writes
nothing, a retryable failure is never abandoned — are the reason a customer never
silently misses a notification. A second poller would have been a second
implementation of all three, and therefore a second chance to get one wrong.

So the cycle lives once in `DocumentPoller`, and a pipeline supplies only what is
genuinely per-document: which source call lists and fetches, whether a listing
row is too thin to send from, how a document maps onto a report row, and its
wording. `InvoicePoller`'s public constructor is inherited unchanged, so its
entire existing test suite passes without modification — which is the evidence
that the refactor changed no behaviour. When the payments pipeline was added, the
invariants did not get re-implemented; they got a second caller.

### Why does every new payment cost two Daftra requests?

A payment record names no payer: `client_id` is usually null and the contact
fields are empty, because in Daftra the payer exists only on the invoice the
payment settles. Reaching the customer therefore means reading that invoice too.

That join is done in the adapter (`DaftraClient.get_payment`), not in the
poller. The port states what a caller needs — a payment whose customer is
resolved — and the application layer never learns Daftra's join order. It is
bounded by the send cap, exactly like the invoice pipeline's detail fetch: the
cap is checked before the send site, so a backlog costs listing pages, not one
detail call per row on the page.

A payment whose invoice has been deleted raises a 404, which is permanent, so it
is abandoned once and left visible in `poll-status --payments` rather than
retried forever against a record that will never resolve.

### Why completed payments only?

The template says the customer's balance was updated, and only a completed
payment does that; announcing a pending or failed one would be a lie to a
customer. `POLL_PAYMENTS_STATUS` defaults to `1` and is applied **server-side**,
so a page of the listing is never spent on payments that will not be announced —
which matters because a page is the unit the poller's paging walk reasons about.

Blank is a real, deliberate value meaning "no filter", and it is distinct from
the variable being absent. A typo raises instead of defaulting, because the
silent failure here is the worst kind: narrowing to a status Daftra does not have
would make the pipeline find nothing and report nothing.

### Why no template registry on the payments path?

`TemplateRegistry` exists to choose between the two *invoice* builders, by looking
at whether the live `aizen_invoice` revision has a DOCUMENT header. The payment
template has one approved shape — BODY and FOOTER, no header — so there is one
legal payload, no builder to choose, and no reason to spend a Graph API round-trip
per run learning something already known. There is consequently no document to
attach either, so the payments pipeline never renders or uploads a PDF.

If the template is unusable, Meta rejects the send with a `132000`-series code and
the poller's existing handling applies unchanged: the free-form text fallback if
it is enabled, otherwise the payment stays **pending** on the template error and
comes back once the template is fixed. Never abandoned, never consumed.

## 12. Why a third pipeline for new customers?

New customers are announced under `aizen_new_customer`, from
`python -m sender poll-customers`. Everything in §11 still holds — separate state,
separate `flock`, separate cap, separate cron line — and the reason the pipeline
exists at all is different enough to be worth stating separately.

### Why is this the *smallest* pipeline, not the biggest?

It is the smallest of the three subclasses by a wide margin: `CustomerPoller`
overrides five things and inherits everything else. `InvoicePoller` and
`PaymentPoller` each had to answer a question the engine could not answer for
them — invoices need their line items fetched, payments need a second request to
find the payer at all. A Daftra client row carries its own name and phone, so the
engine's inherited "no reachable phone" rule is already exactly right: a normal
listing row costs zero extra requests, and only a row missing a phone triggers a
detail fetch.

That is the argument for the shared engine restated in its strongest form. The
third pipeline did not need one new behaviour in `DocumentPoller` — not a hook, not
a flag, not a branch. What it needed was for the pipeline-specific differences to
have been pushed into the subclasses where they belong, so a pipeline with *no*
difference needed no engine at all.

### Why does the client listing ask for an explicit sort?

Daftra's `/clients.json` does not come back newest-first. Its default order is
stable but arbitrary — a live account returned ids `[5,1,2,6,4,3]` — and only
`sort=created&direction=desc` yields true newest-first. (`sort=created` alone
gives *oldest* first; `order=desc` is silently ignored; every date-filter spelling
except `created_from` is silently ignored too, answering `200` with the unfiltered
set.)

This is the single highest-risk line in the pipeline, and it is a silent failure.
The engine walks pages forward and stops at the first record it has already seen,
which is only correct if newer records come first. Under an arbitrary order the
walk stops early on a saturated page and never announces a new customer sitting
further back — no exception, no warning, a pipeline that looks perfectly healthy
and has quietly stopped working. There is no way to detect this from the output.

So the parameters are pinned by a test that asserts the exact request dict, and
`list_customers` carries a comment explaining why each one is load-bearing. The
general lesson is worth more than the endpoint: **an API that ignores what you ask
it for is more dangerous than one that rejects it**, because a typo becomes a silent
wrong answer instead of a loud failure.

### Why is there no `created_from` watermark?

The endpoint accepts one, so the obvious design is "remember the newest `created`
and ask for everything after it". It was rejected for four reasons:

- the boundary is **date-granular and inclusive** — `created_from=2026-05-02 12:25:31`
  still returns a client created at `12:25:30`, because the time component is
  truncated. A timestamp watermark cannot be expressed, only a date, so every run
  re-scans a day;
- the accounts hold **one to six clients**. There is nothing to save;
- a watermark would mean extending the shared `PollStateStore` protocol and its
  JSON format — a behaviour change to infrastructure shared by three pipelines, for
  no gain;
- and the misspelling risk above applies to the filter as much as to the sort. One
  more silent-wrong-answer surface, in exchange for nothing.

The seen-set *is* the watermark, and it is exact rather than approximate. "New
customer" therefore means "never seen by this pipeline", which is what the first-run
seeding already makes correct: existing customers are recorded as handled without
being messaged, and only genuinely new ones get a welcome.

### Why is the free-form fallback off here, when it is on everywhere else?

`WHATSAPP_FREEFORM_FALLBACK` defaults to `true` for invoices and payments, and this
pipeline overrides it to `false` with its own `WHATSAPP_CUSTOMER_FREEFORM_FALLBACK`.

The reason is that a welcome goes to a **brand-new number**, which is by definition
outside WhatsApp's 24-hour customer-service window — and that window is the only
place free-form text is deliverable at all. A fallback here can never rescue a
send. It can only spend a doomed second request on every template error and bury the
real error under a second one. (The invoices pipeline's default is on for a very
different reason: its template has no approved English translation yet, so without a
fallback *every* send fails. That is a bridge, not a permanent mode.)

With it off, the engine's existing behaviour is exactly right: a `132000`-series
rejection leaves the customer **pending** on the template error. Fix the template,
and the welcomes flow on a later cycle. Nothing is consumed, nothing is lost.

### Why is nothing filtered on `type`, `suspend` or `is_offline`?

Daftra's `type` takes values 1/2/3 and nothing documents what they mean. Filtering
on a guess would be the worst kind of bug: real customers silently never welcomed,
marked seen, invisible in every log. The pipeline carries the fields for display
and acts on none of them.

This is the deliberate counterpart to §11's `POLL_PAYMENTS_STATUS=1`. There, the
filter is justified because the *template text* requires it — "your balance was
updated" is false for a pending payment, and the choice is auditable against the
message being sent. Here no wording constrains the set, so there is nothing to
justify a filter against. If the business later wants to exclude suspended clients,
that needs `suspend`'s real semantics confirmed against Daftra's documentation
first.

### What a reviewer must confirm before this runs in production

`aizen_new_customer` is **MARKETING** category, not UTILITY like the other two.
That is not a labelling detail — it changes what Meta requires of the sender:

- **Opt-in evidence.** Marketing messages require demonstrable consent. Someone has
  to be able to say *how* each new customer opted in (a signup checkbox, terms
  acceptance). If that answer does not exist, this pipeline should not be enabled,
  and no amount of code correctness makes it compliant.
- **Unregistered numbers.** A brand-new number may not be on WhatsApp yet. Those are
  rejected permanently, abandoned once, and left visible in
  `poll-status --customers`. The engine handles this correctly; the business should
  expect to see those rows.
- **First-run seeding means existing customers are not welcomed.** By design. If the
  business *wants* a one-off welcome to current customers, that is `--send-existing`
  on the first run — a deliberate, visible decision rather than a default.

## Back to

- [Overview](index.md)
- [Architecture](architecture.md)
- [Next: getting started](guide/getting-started.md)
- [Invoice PDF attachment](guide/invoice-pdf.md)
- [Payments pipeline](guide/payments.md)
- [Customers pipeline](guide/customers.md)