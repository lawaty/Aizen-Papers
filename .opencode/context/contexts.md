# Subsystems

Ten subsystems plus the host-side ops wrappers. Per layer detail, read
`docs/layers/*.md`; this file is the "which files do I open" index.

---

## 1. CLI & composition root — `sender/presentation/`

**Purpose:** the only place that imports every layer and wires adapters into use
cases. Parses args, builds the object graph, prints output.

**Start here**
- `presentation/cli.py` — `build_service()` is the wiring seam; `_run_poll()` is the poll entry.
- `presentation/stubs.py` — the offline `StubInvoiceSource` used by `--stub` and by tests.

**Boundaries:** may import all layers. Nothing imports it except `__main__.py`.
Commands that only read (`show`, `list`) deliberately skip template resolution so
they work without WhatsApp credentials.

**Must not change silently:** adding an adapter import here is expected; adding a
*business* rule here is not. `cli.py` is already 757 lines — new command logic
belongs in `application/`, new parsing stays here.

---

## 2. Poll loop — `sender/application/poller.py`

**Purpose:** turns a manual one-shot `send` into a watcher. Per cycle, per app, in
configuration order: list recent invoices, decide what is new via the state store,
send each new one, classify failures.

**Start here**
- `application/poller.py` — `InvoicePoller.run_once` / `_poll_app` / `_handle_invoice`.
- `domain/ports.py` — the `PollStateStore` and `Clock` protocols it is written against.
- `infrastructure/state.py` — `JsonPollStateStore`; what `seen`/`pending`/`abandoned` mean on disk.
- `tests/test_poller.py` — the behavioural spec (largest test file).

**Boundaries:** depends on `domain` only. Consecutive apps never run concurrently
and never share state.

**Must not change:** the three safety invariants — the first run per app **seeds
without sending**; `--dry-run` writes **nothing** to the state; retryable failures
are **never** abandoned. They are the whole reason a payment-reminder flow cannot
silently drop an invoice. Rationale in `docs/design.md` §10.

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
  there would strand that backlog forever.

**Payload-level Meta error codes are classified, not just HTTP status.** See
`_RETRYABLE_API_CODES` / `_PERMANENT_API_CODES` in `poller.py`: `130429`/`131056`
are backpressure (retry); `131047`/`131048`/`131049` are quality signals treated
as permanent, because retrying them feeds the behaviour Meta is penalising. They
arrive as HTTP 400, so status alone misreads them.

---

## 3. Send report — `domain/reporting.py`, `infrastructure/reporting.py`, `report` command

**Purpose:** an operator-facing audit trail of WhatsApp send attempts, as browsable
HTML pages per day. Fed by the poll loop; the JSONL log is the record and the HTML
is a rendering of it.

**Start here**
- `domain/reporting.py` — pure HTML + JSON serialization, no I/O (mirrors `domain/templates.py`); every interpolated value goes through `html.escape(quote=True)`.
- `infrastructure/reporting.py` — `JsonlSendOutcomeRecorder` (append-only sink) and `ReportStore` (load/render/prune); owns paths, retention and the generated `.htaccess`.
- `domain/models.py` — `SendOutcome` + the `SENT`/`FAILED`/`ABANDONED` status constants.
- `domain/ports.py` — the `SendOutcomeRecorder` protocol the poller is written against.
- `presentation/cli.py` — `_run_report`, `_report_recorder`, `_refresh_report_pages`.

**Boundaries:** written *after* the run, outside the poll-state lock. The recorder
is a **separate port from `PollStateStore`** — different retention, different
failure contract. Only real send attempts are recorded: never a dry run, a
no-phone skip, or a pre-send build/listing failure.

**Must not change:** the recorder is **best-effort and swallows write failures**,
deliberately inverting the state store's contract — losing a state write means
re-sending, losing a report row must never fail a cycle that already reached
customers. Every interpolated value is `html.escape(quote=True)`d. Phone
obfuscation is on by default (the pages are meant for a web root). `render()` is
idempotent, so a manual run racing a cycle is harmless.

> Confidence: high (read in full 2026-10-01). **The subsystem has no tests** — no
> `tests/test_reporting.py`; only the poller-side plumbing is covered. Rendering
> and retention are unverified.

---

## 4. Template contract & builder auto-switch — `domain/templates.py` + `infrastructure/whatsapp/template_registry.py`

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
directly. Registry is infrastructure because it queries the Graph API.

**Must not change:** the parameter **order** and the date (`DD/MM/YYYY`) / money
(`1,500.00`, no currency symbol) formatting. A mismatch surfaces only as a
runtime `132000`-series rejection from Meta, not as a test failure.

---

## 5. PDF + Arabic rendering — `infrastructure/pdf.py`, `arabic.py`, `truetype.py`, `png.py`, `logo.py`

**Purpose:** renders the invoice PDF from data already in hand, because Daftra has
no PDF export and its `invoice_pdf_url` is session-gated. A PDF viewer performs no
shaping, so the sender resolves Arabic contextual forms and run order itself and
reads the embedded font directly.

**Start here**
- `infrastructure/pdf.py` — the writer (1,231 lines; the largest file in the repo).
- `infrastructure/truetype.py` — glyph-id lookup and `Identity-H`/`FontFile2` emission.
- `infrastructure/arabic.py` — contextual forms and run reversal.
- `infrastructure/fonts/README.md` — why a 87 KB Noto Naskh Arabic subset is committed (OFL).
- `docs/guide/invoice-pdf.md` — layout, palette, and the offline verification recipe.

**Boundaries:** isolated leaf code, no project imports. Latin text still goes to
standard-14 Helvetica; the subset contains almost no ASCII.

**Must not change:** the font subset is committed and licence-bound; do not
regenerate it casually. Undrawable characters are dropped with a WARNING naming
the field — Arabic itself must never warn. Correctness is pinned by
`tests/test_pdf.py`, `test_arabic.py`, `test_truetype.py`, `test_logo.py`
(compare glyph ids, not rendered pixels).

---

## 6. Daftra adapter — `infrastructure/daftra/`

**Purpose:** reads invoices from the Daftra ERP REST v2 API.

**Start here**
- `infrastructure/daftra/client.py` — `DaftraClient`; authenticates with the `apikey` header.
- `infrastructure/daftra/mapper.py` — `DaftraInvoiceMapper`; the only place that knows the wire format.

**Boundaries:** implements `InvoiceSource`. The mapper is deliberately isolated so
a Daftra schema change is a one-file diff. `show --raw` exists to inspect the live
wire format — use it before changing the mapper.

**Must not change:** the mapping is unidirectional; do not copy Daftra's business
rules inward. The internal model holds only what outbound notifications need.

---

## 7. WhatsApp adapter — `infrastructure/whatsapp/`

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

## 8. Config & state persistence — `infrastructure/config.py`, `state.py`, `clock.py`

**Purpose:** turns `.env` into a frozen `Settings`, and persists the poller's
per-app bookkeeping to a JSON file.

**Start here**
- `infrastructure/config.py` — `Settings`, `DaftraApp`, `from_env`; the `_apps_from_env` slot logic. Also holds the poll-cap and `REPORT_*` knobs; see §2 and §3 for what they govern.
- `infrastructure/state.py` — `JsonPollStateStore` (atomic pid-qualified writes) and `PollStateLock`.

**Boundaries:** state is keyed **per app name**, and the name is derived from the
account subdomain so a tenant's history is stable across restarts.

**Must not change:** a corrupt or missing state file must log a warning and start
empty — the poller never crashes on its own bookkeeping. `--dry-run` must not
create the file. The `flock` is held for the whole real run.

---

## 9. Offline rehearsal — `presentation/stubs.py` + `--stub`

**Purpose:** runs the entire flow with no network, for rehearsals and for the
poller tests.

**Start here**
- `presentation/stubs.py` — `StubInvoiceSource`, `default_stub_invoices()`.
- `stub_invoices.json` — the persistent fixture; `stub-add` appends to it.
- `presentation/cli.py` — `_stub_source()`; stub mode must stay offline.

**Boundaries:** a stub run never resolves the builder via the Graph API, and
upload-mode attachments are refused unless asked for by name.

---

## 10. Host-side ops wrappers — `tools/`

**Purpose:** the shell scripts the *host* runs around the app. Never imported by
`sender/`; they must not accumulate application logic.

**Start here**
- `tools/run_poll.sh` — the cron wrapper for `poll`; its header comment holds the
  live cron line and an offline rehearsal recipe.
- `tools/build_vendor.py` — the no-pip bootstrap generator (see §11).

**Boundaries:** the wrapper resolves the project root from its own location (no
hardcoded path) and forwards args and exit code verbatim to
`python -m sender poll`. Runtime trace in `workflows.md` § 2; why it is a shell
script and not a logging handler is in `decisions.md` § 5.

---

## 11. No-pip bootstrap — `sender/__init__.py`, `tools/build_vendor.py`

**Purpose:** runs on a shared host with no package manager by appending a
generated copy of the pure-Python deps to `sys.path`. Full rationale and
consequences: [`decisions.md` § 2](decisions.md); runtime trace:
[`workflows.md` § 5](workflows.md).

**Start here**
- `sender/__init__.py` — `_bootstrap_vendor()`; appends, never prepends, so a real venv always wins.
- `tools/build_vendor.py` — regenerates `vendor/` from `.venv`; strips `.so`/tests/`__pycache__`. Adding a runtime dependency means adding it to `PACKAGES` here too, or prod breaks silently.
- `pytest.ini` — `testpaths = tests` exists so the vendored suites are not collected.
