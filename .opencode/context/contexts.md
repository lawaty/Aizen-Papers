# Subsystems

Nine subsystems plus the host-side ops wrappers. Per layer detail, read
`docs/layers/*.md`; this file is the "which files do I open" index.

---

## 1. CLI & composition root — `sender/presentation/`

**Purpose:** the only place that imports every layer and wires adapters into use
cases. Parses args, builds the object graph, prints output.

**Start here**
- `presentation/cli.py` — `build_service()` is the wiring seam; `_run_poll()` is the poll entry.
- `presentation/stubs.py` — the offline `StubInvoiceSource` used by `--stub` and by tests.
- `docs/layers/presentation.md` — the command/flag table.

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

---

## 3. Template contract & builder auto-switch — `domain/templates.py` + `infrastructure/whatsapp/template_registry.py`

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

## 4. PDF + Arabic rendering — `infrastructure/pdf.py`, `arabic.py`, `truetype.py`, `png.py`, `logo.py`

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

## 5. Daftra adapter — `infrastructure/daftra/`

**Purpose:** reads invoices from the Daftra ERP REST v2 API.

**Start here**
- `infrastructure/daftra/client.py` — `DaftraClient`; authenticates with the `apikey` header.
- `infrastructure/daftra/mapper.py` — `DaftraInvoiceMapper`; the only place that knows the wire format.
- `docs/layers/infrastructure.md`.

**Boundaries:** implements `InvoiceSource`. The mapper is deliberately isolated so
a Daftra schema change is a one-file diff. `show --raw` exists to inspect the live
wire format — use it before changing the mapper.

**Must not change:** the mapping is unidirectional; do not copy Daftra's business
rules inward. The internal model holds only what outbound notifications need.

---

## 6. WhatsApp adapter — `infrastructure/whatsapp/`

**Purpose:** the Meta Cloud API edge — sends messages, uploads media, resolves
template status, and receives delivery webhooks.

**Start here**
- `infrastructure/whatsapp/client.py` — `WhatsAppClient.send`; 429 retry honoring `Retry-After`.
- `infrastructure/whatsapp/media.py` — `MetaMediaUploader`; the two-phase media upload.
- `infrastructure/whatsapp/webhook_server.py` — the delivery-status receiver.
- `infrastructure/attachments.py` — `UploadedMediaProvider` (render + upload, behind the port).
- `docs/guide/delivery-status.md` — why HTTP 200 is not delivery; error codes.

**Boundaries:** implements `MessageSender` / `MediaUploader`.

**Must not change:** retries are restricted to HTTP 429; widening them is a
behavioural decision, not a bug fix. A `200` from the webhook receiver means
accepted, not delivered.

---

## 7. Config & state persistence — `infrastructure/config.py`, `state.py`, `clock.py`

**Purpose:** turns `.env` into a frozen `Settings`, and persists the poller's
per-app bookkeeping to a JSON file.

**Start here**
- `infrastructure/config.py` — `Settings`, `DaftraApp`, `from_env`; the `_apps_from_env` slot logic.
- `infrastructure/state.py` — `JsonPollStateStore` (atomic pid-qualified writes) and `PollStateLock`.
- `infrastructure/clock.py` — `SystemClock`, injected so the poll loop is testable.

**Boundaries:** state is keyed **per app name**, and the name is derived from the
account subdomain so a tenant's history is stable across restarts.

**Must not change:** a corrupt or missing state file must log a warning and start
empty — the poller never crashes on its own bookkeeping. `--dry-run` must not
create the file. The `flock` is held for the whole real run.

---

## 8. Offline rehearsal — `presentation/stubs.py` + `--stub`

**Purpose:** runs the entire flow with no network, for rehearsals and for the
poller tests.

**Start here**
- `presentation/stubs.py` — `StubInvoiceSource`, `default_stub_invoices()`.
- `stub_invoices.json` — the persistent fixture; `stub-add` appends to it.
- `presentation/cli.py` — `_stub_source()`; stub mode must stay offline.

**Boundaries:** a stub run never resolves the builder via the Graph API, and
upload-mode attachments are refused unless asked for by name.

---

## 9. No-pip bootstrap — `sender/__init__.py`, `tools/build_vendor.py`

**Purpose:** lets the app run on a shared host with no package manager by copying
the pure-Python deps into `vendor/` and appending it to `sys.path`.

**Start here**
- `sender/__init__.py` — `_bootstrap_vendor()`; appends, never prepends, so a real venv always wins.
- `tools/build_vendor.py` — regenerates `vendor/` from `.venv`; strips `.so`/tests/`__pycache__`.
- `pytest.ini` — `testpaths = tests` exists so the vendored suites are not collected.

**Boundaries:** `vendor/` is **gitignored and not source**. Adding a runtime
dependency means adding it to `tools/build_vendor.py: PACKAGES` too, or prod
breaks silently.

**Must not change:** the append (not insert at position 0) semantics — a normal
developer checkout must behave exactly as before.

---

## 10. Host-side ops wrappers — `tools/`

**Purpose:** the shell scripts the *host* runs around the app. Never imported by
`sender/`; they must not accumulate application logic.

**Start here**
- `tools/run_poll.sh` — the cron wrapper for `poll`; it owns CWD, the interpreter
  (`$PYTHON`), per-day logging and retention. Its header comment holds the live
  cron line and an offline rehearsal recipe.
- `tools/build_vendor.py` — the no-pip bootstrap generator (see §9).

**Boundaries:** the wrapper resolves the project root from its own location (no
hardcoded path) and forwards args and exit code verbatim to
`python -m sender poll`. Runtime trace in `workflows.md` § 2.
