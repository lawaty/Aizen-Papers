# Subsystems

Twelve subsystems plus the host-side ops wrappers. Per layer detail, read
`docs/layers/*.md`; this file is the "which files do I open" index.

---

## 1. CLI & composition root — `sender/presentation/`

**Purpose:** the only place that imports every layer and wires adapters into use
cases. Parses args, builds the object graph, prints output.

**Start here**
- `presentation/cli.py` — `build_service()` / `build_payment_service()` are the two
  wiring seams; `_run_poll()` / `_run_poll_payments()` are the poll entries, both
  delegating to the shared `_run_poll_common`.
- `presentation/stubs.py` — the offline sources and the `StubMessageSender` (§ 10).

**Boundaries:** may import all layers. Nothing imports it except `__main__.py`.
Commands that only read (`show`, `list`, `payments`, `show-payment`) deliberately
skip template resolution so they work without WhatsApp credentials.

**Must not change silently:** adding an adapter import here is expected; adding a
*business* rule here is not. `cli.py` is already 1,454 lines — the largest file in
the repo — so new command logic belongs in `application/`, new parsing stays here.

---

## 2. Poll loop — `sender/application/poller.py`

**Purpose:** turns a manual one-shot `send` into a watcher. Per cycle, per app, in
configuration order: list recent documents, decide what is new via the state store,
send each new one, classify failures.

**The cycle lives in a generic engine, not in `InvoicePoller`.**
`DocumentPoller[TDoc, TSource]` (1,123 lines) holds the whole loop; `InvoicePoller`,
`PaymentPoller` (§ 3) and `CustomerPoller` are thin siblings that override only
the per-document hooks `_list_documents` / `_fetch_document` / `_label` /
`_needs_detail` / `_after_detail_fetch` / `_make_outcome`, plus `KIND` and
`CAP_ENV_VAR`. A
subclass that reimplemented any policy decision there would be a second,
less-tested copy of the notification guarantee — `docs/design.md` § 11 records
why the engine was extracted. The evidence that the extraction changed no
behaviour is that `InvoicePoller`'s constructor is inherited unchanged and the
pre-existing suite passes unedited.

**Start here**
- `application/poller.py` — `DocumentPoller.run_once` / `_poll_app` / `_handle_document`.
- `presentation/cli.py` — `_run_poll_common`, the signal/lock/exit-code shell both
  poll commands run under, shared verbatim so the two cannot drift.
- `domain/ports.py` — the `PollStateStore` and `Clock` protocols it is written against.
- `infrastructure/state.py` — `JsonPollStateStore`; what `seen`/`pending`/`abandoned` mean on disk.
- `tests/test_poller.py` — the behavioural spec for the shared engine.

**Boundaries:** depends on `domain` only. Consecutive apps never run concurrently
and never share state.

**Must not change:** the three safety invariants — the first run per app **seeds
without sending**; `--dry-run` writes **nothing** to the state; retryable failures
are **never** abandoned. They are the whole reason a notification flow cannot
silently drop an invoice. They are written into `DocumentPoller`'s own docstring,
so they apply to every subclass. Rationale in `docs/design.md` §10.

**Re-fetching the detail is the normal path, not the exception** (`_needs_detail`:
missing phone **or** missing items — see § 7). A failed detail fetch **fails the
send**; an invoice with no items even after the detail is **still sent** with a
WARNING, since that is Daftra's own answer. Authority:
[`docs/layers/application.md`](../../docs/layers/application.md); rationale in
[`decisions.md`](decisions.md) § 8.

**Per-cycle send cap (`max_sends_per_run`, default 10, `POLL_MAX_SENDS_PER_RUN`,
`--max-sends`; `0` disables).** A throttle on requests to Meta, **distinct from**
`limit`/`max_pages`, which bound what the poller can *see*. It is budgeted across
**all apps** per cycle and consumed at the send site (`_budget_send`), so a failed
send still costs budget. Two invariants make it safe:

- A capped invoice is left **unseen**, never marked seen or abandoned —
  otherwise the throttle would convert undelivered invoices into permanently
  skipped ones. It surfaces as `deferred_by_cap` in the summary plus a WARNING
  naming `POLL_MAX_SENDS_PER_RUN`.
- `_list_candidates(draining=...)` keeps paging past an all-seen page while the
  previous cycle deferred work. Capped cycles send the *newest* invoices, so the
  next cycle can meet a fully-seen page with deeper backlog behind it; stopping
  there would strand that backlog forever. That signal is a **persisted per-app
  flag** (`PollStateStore.is_draining` / `set_draining` → `poll_state.json`), not
  an instance attribute, because production runs one `poll --once` process per
  cron tick. `_poll_app` clears it only *after* a successful listing, so a
  transient Daftra error cannot discard it and strand the backlog.
- The free-form fallback re-checks the cap before its own POST: the template
  rejection it recovers from already spent a unit, so an unguarded retry would
  be the (cap+1)-th message in front of Meta. Capped, it defers and leaves the
  invoice pending on the template error.
- The cap also bounds **Daftra read** traffic: the per-invoice detail fetch sits
  inside `_handle_document`, which is only reached once `_send_cap_reached()` has
  passed. Extra reads scale with invoices *sent*, never with the page size.

**Payload-level Meta error codes are classified, not just HTTP status.** See
`_RETRYABLE_API_CODES` / `_PERMANENT_API_CODES` in `poller.py`: `130429`/`131056`
are backpressure (retry); `131047`/`131048`/`131049` are quality signals treated
as permanent, because retrying them feeds the behaviour Meta is penalising. They
arrive as HTTP 400, so status alone misreads them.

---

## 3. Payments pipeline — the invoice pipeline's twin

**Purpose:** confirms each newly recorded Daftra payment to the customer under
`aizen_new_payment` (language **`ar_EG`**). A **parallel pipeline**, not a mode of
§ 2: its own command (`poll-payments`), state file, `flock`, send cap, cron line,
fixture and env namespace (`POLL_PAYMENTS_*`). Read it as "everything § 2 says,
with `payment` where `invoice` appears". Rationale: `docs/design.md` § 11.

**Start here**
- `docs/guide/payments.md` — **the authority**: commands, template contract, env
  knobs, offline rehearsal, failure behaviour. Read it before touching the code.
- `application/poller.py` — `PaymentPoller`, ~35 lines; compare it against
  `InvoicePoller` to see exactly what a pipeline owes.
- `domain/templates.py` — `PaymentTemplateBuilder`: 4 positional parameters,
  `ar_EG`, **no header component**.
- `infrastructure/daftra/client.py` — `DaftraClient.get_payment`, the two-call join; the wire shape lives in `DaftraPaymentMapper` (§ 7).
- `tests/test_payment_poller.py` — the behavioural spec; `test_payment_payload_contract.py`
  pins the template parameter order the way `test_daftra_payment_mapper.py` pins the wire mapping.

**Boundaries:** everything is inherited, not reimplemented — the same
`DocumentPoller`, the same `JsonPollStateStore`/`PollStateLock` classes over a
different path, the same retry classification, the same free-form fallback, the
same HTML report (§ 4). It does **not** consult `TemplateRegistry` (§ 5): the
payment template has one approved shape, so there is no builder to choose and no
document to attach. Consequently there is no PDF, no upload, and `poll-payments`
takes **no** `--builder` and **no** `--attachment`. The one-shot commands
(`payments`, `show-payment`, `send-payment`) run through
`application/services.py:PaymentNotificationService`.

**Must not change:** the four body parameters' **order** and formatting — a
mismatch fails nowhere in this repo, only as a `132000`-series rejection for a
real customer (§ 5). Each new payment costs **two** Daftra requests, because a
payment row names no payer; that join is done in the adapter so `application`
never learns Daftra's join order. `POLL_PAYMENTS_STATUS` is applied
**server-side**, so a listing page is never spent on payments that will not be
announced. A payment whose linked invoice 404s (deleted) is abandoned once and
stays visible in `poll-status --payments`, re-drivable with
`poll-reset --payments --payment-id`.

**The two pipelines never coordinate.** Payment ids and invoice ids **overlap in
the same account**, so a shared "seen" set would let one retire the other.

---

## 4. Send report — `domain/reporting.py`, `infrastructure/reporting.py`, `report` command

**Purpose:** an operator-facing audit trail of WhatsApp send attempts, as browsable
HTML pages per day. Fed by the poll loop; the JSONL log is the record and the HTML
is a rendering of it.

**Start here**
- `domain/reporting.py` — pure HTML + JSON serialization, no I/O (mirrors `domain/templates.py`); every interpolated value goes through `html.escape(quote=True)`, except the recipients page's embedded JSON, which uses `_json_for_script`.
- `infrastructure/reporting.py` — `JsonlSendOutcomeRecorder` (append-only sink) and `ReportStore` (load/render/prune); owns paths, retention and the generated `.htaccess`.
- `domain/models.py` — `SendOutcome` + the `SENT`/`FAILED`/`ABANDONED` status constants.
- `domain/ports.py` — the `SendOutcomeRecorder` protocol the poller is written against.
- `presentation/cli.py` — `_run_report`, `_report_recorder`, `_refresh_report_pages`.
- `tests/test_reporting.py` — the masking spec (`obfuscate_phone`), the `.htaccess` merge contract, and the recipients-page contract (masking on/off, hostile names, the JS status filter).

**Boundaries:** written *after* the run, outside the poll-state lock. The recorder
is a **separate port from `PollStateStore`** — different retention, different
failure contract. Only real send attempts are recorded: never a dry run, a
no-phone skip, or a pre-send build/listing failure.

**One report serves every pipeline** (§ 2 and § 3, plus the customer/welcome
one — `application/poller.py:CustomerPoller`, `cli.py poll-customers`,
`docs/guide/customers.md`; that pipeline has no section of its own here yet),
because it is one audit trail of everything this sender put in front of a
customer. `SendOutcome.kind` carries `KIND_INVOICE`/`KIND_PAYMENT`/`KIND_CUSTOMER`
(the constants live in `domain/models.py`) and renders as a badge column, so one
day can hold several kinds. `invoice_id`/`invoice_number`/`total`/`issue_date`
are overloaded rather than duplicated: the **JSON field** names keep their historic
invoice wording so rows already on disk stay readable, while the **visible**
headers were neutralized to Number / Date / Amount and the page title is derived
from the kinds actually on that day. Do not "restore" invoice wording on screen —
a day holding one payment and two invoices would then mislabel two of its three
rows. `kind` **defaults to invoice** on read, so JSONL written before the payments
pipeline existed still loads and labels correctly — do not make it required.

**`recipients.html` is a third generated page**, written by the same
`ReportStore.render()` call as `index.html`. It answers "what did we send *this
person*", the half of the question the date pages cannot. `render_recipients`
embeds **every** logged outcome from **all** retained days — `_all_entries()`
reads the JSONL back rather than trusting memory, because a cron `--once` run
shares no process state — and filters client-side in JS, since the pages are
static files behind basic auth with no server to ask. Two consequences: it embeds
phone numbers, so `REPORT_OBFUSCE_PHONE=false` writes full numbers into a file
that is likely web-served; and its escaping is done **twice**, by
`_json_for_script` in Python for the embedded data and by `esc()` in `_SEARCH_JS`
for the DOM writes, so a field added to that row builder has to go through both.
`prune()` deletes only `<date>.jsonl` + `<date>.html`; `index.html` and
`recipients.html` are regenerated every render, never pruned.

**Must not change:** the recorder is **best-effort and swallows write failures**,
deliberately inverting the state store's contract — losing a state write means
re-sending, losing a report row must never fail a cycle that already reached
customers. Every interpolated value is `html.escape(quote=True)`d — the recipients
page's embedded JSON excepted, since HTML-escaping it would corrupt it
(`_json_for_script`). `render()` is idempotent, so a manual run racing a cycle is
harmless.

**The report root's `.htaccess` is merged, never overwritten.** `merge_htaccess`
rewrites only the region between `# BEGIN/END Aizen invoice sender generated
block`; everything outside it is operator-owned and must survive. The report
directory sits in a web root behind a hand-written basic-auth block, and the cron
cycle re-renders every five minutes — a plain whole-file write would silently
delete that auth config. Restoring it is also why the `RedirectMatch 404` pattern
is anchored `^/(.*/)?data/` instead of `^/data/`: it matches the request path, so
it only works if it accepts the directory prefix. `_LEGACY_HTACCESS` reconstructs
the pre-marker file by string-subtracting the current comment text out of
`_GENERATED_HTACCESS`, so editing that comment silently breaks the upgrade path
and duplicates every directive on first render.

**`obfuscate_phone` must never yield a dialable number.** The invariant is
"no output the reader could text", not "mask the middle": any input of 6
characters or fewer is replaced entirely with `•`s, because keeping a 4-digit
suffix of a 5-digit number leaks the whole thing. Pinned by
`tests/test_reporting.py` (added 2026-10-01).

> Confidence: high (read in full 2026-10-02). `render_recipients` is covered —
> masking on/off, a hostile customer name, empty history, the store writing the
> page, and the JS filter matching the lowercase status constants. **Still
> unverified** — `render_date_page`, the JSONL round-trip and
> `ReportStore.prune()` have no direct coverage. Note its `_LEGACY_HTACCESS` test
> builds its input from the same constant it asserts on, so it cannot catch a
> regression in that reconstruction.

---

## 5. Template contract & builder auto-switch — `domain/templates.py` + `infrastructure/whatsapp/template_registry.py`

**Purpose:** encodes the approved `aizen_invoice` template — the 4 ordered
variables, their formatting, and the header-document component — and decides
which concrete builder (legacy `DOCUMENT` header vs clean text) the live template
revision requires.

**Start here**
- `domain/templates.py` — `InvoiceTemplateBuilder` and the two concrete builders; `TEMPLATE_BODY` sits next to the parameter logic on purpose.
- `infrastructure/whatsapp/template_registry.py` — `choose_builder()`; tracks the last **approved** revision's structure.
- `domain/attachments.py` — the pure `HostedLinkProvider` / `NoAttachmentProvider` providers.
- `docs/guide/template-contract.md` — the variable ↔ parameter mapping and Meta's rejection rules.

**Boundaries:** the builder depends on the *attachment port*, never on Meta
directly. Registry is infrastructure because it queries the Graph API. The
registry exists to choose between the two **invoice** builders only; the payment
template has one approved shape and skips it entirely (§ 3).

**Must not change:** the parameter **order** and the date (`DD/MM/YYYY`) / money
(`1,500.00`, no currency symbol) formatting. A mismatch surfaces only as a
runtime `132000`-series rejection from Meta, not as a test failure.

---

## 6. PDF + Arabic rendering — `infrastructure/pdf.py`, `arabic.py`, `truetype.py`, `png.py`, `logo.py`

**Purpose:** renders the invoice PDF from data already in hand, because Daftra has
no PDF export and its `invoice_pdf_url` is session-gated. A PDF viewer performs no
shaping, so the sender resolves Arabic contextual forms and run order itself and
reads the embedded font directly.

**Start here**
- `infrastructure/pdf.py` — the writer (1,261 lines).
- `infrastructure/truetype.py` — glyph-id lookup and `Identity-H`/`FontFile2` emission.
- `infrastructure/arabic.py` — contextual forms and run reversal.
- `infrastructure/fonts/README.md` — why a 87 KB Noto Naskh Arabic subset is committed (OFL).
- `docs/guide/invoice-pdf.md` — layout, palette, and the offline verification recipe.

**Boundaries:** isolated leaf code, no project imports. Latin text still goes to
standard-14 Helvetica; the subset contains almost no ASCII.

**Must not change:** the font subset is committed and licence-bound; do not
regenerate it casually. Undrawable characters are dropped with a WARNING naming
the field — Arabic itself must never warn. **Numeric fields overflow rather than
ellipsise**: an over-wide amount *or quantity* is drawn in full, because a
truncated figure is a wrong figure; only item *names* go through `_fit` and get
`...`. A **totalled** invoice with no line
items also warns (`_warn_empty_items`): it is the signature of a row that was
never re-fetched from the detail endpoint (§ 7), and the rendered table would
otherwise tell a customer their invoice has no products. The check requires a
non-zero total on purpose — an invoice with nothing to charge for is a legitimate
empty state and must stay quiet. Correctness is pinned by `tests/test_pdf.py`,
`test_arabic.py`, `test_truetype.py`, `test_logo.py` (compare glyph ids, not
rendered pixels).

---

## 7. Daftra adapter — `infrastructure/daftra/`

**Purpose:** reads invoices (§ 2), payments (§ 3) and customers from the Daftra
ERP REST v2 API. Three resources, **two files**.

**Start here**
- `infrastructure/daftra/client.py` — `DaftraClient`; authenticates with the `apikey` header; `get_*` / `list_*` per resource.
- `infrastructure/daftra/mapper.py` — the only place that knows the wire format: `DaftraInvoiceMapper` (invoices), `DaftraPaymentMapper`, `DaftraCustomerMapper`.

**Boundaries:** implements `InvoiceSource` / `PaymentSource` / `CustomerSource`
(`domain/ports.py`). The mapper is deliberately isolated so a Daftra schema
change is a one-file diff. `show --raw` exists to inspect the live wire format —
use it before changing the mapper.

**Must not change:** the mapping is unidirectional; do not copy Daftra's business
rules inward. The internal model holds only what outbound notifications need.

**One base, three mappers, on purpose.** `DaftraPaymentMapper` and
`DaftraCustomerMapper` **subclass** `DaftraInvoiceMapper` to inherit the *rules* —
money parsing, Daftra's several date spellings, "prefer business name else
first+last", phone normalization — because a second copy is a second place to get
a customer's amount or number wrong. Only the envelope differs: Daftra wraps the
three resources three ways (`data: [{Invoice:…}]`, `data: {InvoicePayment:…}`,
`data: [{Client:…}]`), and each `_*_node` accepts all of them plus a flat record,
so a fixture's nesting can never break the mapper. A fourth mapper should subclass too.

**One unnormalizable listing row is skipped, warned, and left unseen.**
`to_invoices` / `to_payments` / `to_customers` each catch `ValueError` per row and
continue — one odd row must not take a tenant's cycle down, and leaving it unseen
means the next cycle retries it until the source data is fixed. This is what makes
the mapper's *own* raise-vs-warn choices below load-bearing: a `ValueError` that
escapes to the poller is classified **permanent** (§ 2) and would retire a real
invoice on the first sight of a shape it has never seen.

**The mapper is the last place raw truth is visible, so its silences are
tripwires.** Money Daftra charges but the model does not carry
(`summary_tax1..3`, `summary_discount`, `deposit`, item `tax1`/`tax2`/`discount`)
is deliberately unmapped *and* warned about (`_warn_unmapped_money`, key list in
`UNMAPPED_MONEY_KEYS`) — tax and discount are in neither `Invoice` nor the PDF
totals block. Zero-detection goes through `_is_money_present`, never truthiness,
because Daftra sends `deposit: "0"` and a bare test would warn on the whole ledger.
Same asymmetry on the item and date paths: **absent** item money or date warns and
renders its default / `N/A`; **present-but-unparseable** money still raises via
`_money`. Exponent notation is rejected before the digit-stripping sanitizer,
which would otherwise turn `1e3` into `13`. Endpoints are documented per pipeline
in `docs/guide/payments.md` / `docs/guide/customers.md`; `docs/layers/infrastructure.md`
§ mapper predates both mappers and every tripwire here. Rationale:
[`decisions.md`](decisions.md) § 10.

**The two endpoints do not carry the same fields, and that is load-bearing.**
`GET /invoices/{id}.json` returns `InvoiceItem[]`; the listing the poller pages
over (`GET /invoices.json`) returns **no `InvoiceItem` key at all**, so every
listed row maps to `items=()`. The mapper therefore cannot tell a genuinely
item-less invoice from an un-fetched one, and two other subsystems compensate:
the poller re-fetches the detail before building (§ 2) and the PDF writer warns
when a totalled invoice has no rows (§ 6). A listing that starts embedding items
would make the extra fetch redundant, not wrong. Authority:
`docs/layers/infrastructure.md` § mapper.

---

## 8. WhatsApp adapter — `infrastructure/whatsapp/`

**Purpose:** the Meta Cloud API edge — sends messages, uploads media, resolves
template status, and receives delivery webhooks.

**Start here**
- `infrastructure/whatsapp/client.py` — `WhatsAppClient.send`; 429 retry honoring `Retry-After`.
- `infrastructure/whatsapp/media.py` — `MetaMediaUploader`; the two-phase media upload.
- `infrastructure/attachments.py` — `UploadedMediaProvider` (render + upload, behind the port).
- `docs/guide/delivery-status.md` — why HTTP 200 is not delivery; error codes.

**Boundaries:** implements `MessageSender` / `MediaUploader`.

**Must not change:** retries are restricted to HTTP 429; widening them is a
behavioural decision, not a bug fix. A `200` from the webhook receiver means
accepted, not delivered.

---

## 9. Config & state persistence — `infrastructure/config.py`, `state.py`, `clock.py`

**Purpose:** turns `.env` into a frozen `Settings`, and persists the poller's
per-app bookkeeping to a JSON file.

**Start here**
- `infrastructure/config.py` — `Settings`, `DaftraApp`, `from_env`; the `_apps_from_env` slot logic. Also holds the poll-cap and `REPORT_*` knobs; see §2 and §4 for what they govern.
- `infrastructure/state.py` — `JsonPollStateStore` (atomic pid-qualified writes) and `PollStateLock`.

**Boundaries:** state is keyed **per app name**, and the name is derived from the
account subdomain so a tenant's history is stable across restarts.

**Must not change:** a corrupt or missing state file must log a warning and start
empty — the poller never crashes on its own bookkeeping. `--dry-run` must not
create the file. The `flock` is held for the whole real run. The payments pipeline
reuses the same store and lock classes over **different paths**
(`POLL_PAYMENTS_STATE_PATH`); nothing is shared *between* the two files.

---

## 10. Offline rehearsal

**Purpose:** runs the entire flow with no network, for rehearsals and for the
poller tests.

**Start here**
- `presentation/stubs.py` — `StubInvoiceSource` + `StubPaymentSource` (each with
  `stub_invoices.json` / `stub_payments.json` and `stub-add` / `stub-payment-add`),
  and `StubMessageSender`, the sender double behind `--meta-stub`.
- `presentation/cli.py` — `_stub_source()`, `_stub_payment_source()`, `_meta_sender()`.

**The three stubs are independent flags, and only one of them stops a real
message.** `--invoice-stub` / `--payment-stub` replace the *source* (Daftra);
`--meta-stub` replaces the *sender* (Meta). Stubbing a source says nothing about
the sender, so `--payment-stub` alone still messages whoever the fixture names.
**A safe rehearsal needs `--meta-stub`.** It is separate on purpose — see
[`decisions.md`](decisions.md).

**Boundaries:** a stub run never resolves the builder via the Graph API, and
upload-mode attachments are refused unless asked for by name. With `--meta-stub`
the WhatsApp credentials are not even required (`need_whatsapp` is gated on
`not meta_stub`), so a rehearsal works on a machine that has no token at all.

---

## 11. Host-side ops wrappers — `tools/`

**Purpose:** the shell scripts the *host* runs around the app. Never imported by
`sender/`; they must not accumulate application logic.

**Start here**
- `tools/run_poll.sh` — the cron wrapper, serving **both** poll commands; its
  header comment holds the live cron lines and an offline rehearsal recipe.
- `tools/build_vendor.py` — the no-pip bootstrap generator (see §12).

**Boundaries:** the wrapper resolves the project root from its own location (no
hardcoded path) and forwards args and exit code verbatim to
`python -m sender $POLL_SUBCOMMAND` (default `poll`; set `POLL_SUBCOMMAND=poll-payments`
for the payments pipeline). Runtime trace in `workflows.md` § 2; why it is a shell
script and not a logging handler is in `decisions.md` § 5.

---

## 12. No-pip bootstrap — `sender/__init__.py`, `tools/build_vendor.py`

**Purpose:** runs on a shared host with no package manager by appending a
generated copy of the pure-Python deps to `sys.path`. Full rationale and
consequences: [`decisions.md` § 2](decisions.md); runtime trace:
[`workflows.md` § 5](workflows.md).

**Start here**
- `sender/__init__.py` — `_bootstrap_vendor()`; appends, never prepends, so a real venv always wins.
- `tools/build_vendor.py` — regenerates `vendor/` from `.venv`; strips `.so`/tests/`__pycache__`. Adding a runtime dependency means adding it to `PACKAGES` here too, or prod breaks silently.
- `pytest.ini` — `testpaths = tests` exists so the vendored suites are not collected.
