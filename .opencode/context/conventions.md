# Conventions

Repository-specific rules. Anything already covered by `docs/design.md` is linked
there rather than restated.

## Code style

- **Standard library first.** Two runtime dependencies only (`requests`,
  `python-dotenv`); see `docs/design.md` decision 7. Adding a dependency is a
  real decision, not a convenience.
- **`from __future__ import annotations`** at the top of every module; PEP 604
  unions (`int | str`) and modern typing.
- **Money is `Decimal`, never `float`.**
- **Models are frozen dataclasses** so payloads cannot be mutated mid-pipeline.
- **A document carries `customer_phones: tuple[str, ...]`, never a single
  phone.** Daftra keeps two phone fields on a client and either may be filled, so
  the tuple's order is the Daftra field preference (`phone2`, `phone1`, `mobile`,
  `phone`), a one-value tuple is the ordinary case, and `()` means there is
  nowhere to send. **Never index `[0]` unconditionally** — senders iterate and
  display code must handle the empty case (the poller's "skip and mark seen"
  behaviour hangs off that). De-duplicate **after** normalization, since `010…`
  and `+2010…` are one person. `--to` is an explicit single-recipient override
  and stays one. `SendOutcome.customer_phone` is deliberately still singular — the
  report records one row per POST (`contexts.md` § 4). Field semantics:
  [`docs/guide/customers.md`](../../docs/guide/customers.md) § *Phone*;
  rationale: [`decisions.md`](decisions.md) § 11.
- Long explanatory comments are welcome and idiomatic here — the codebase
  documents *why* inline, not just what. Match that density when editing.
- `logging` for operational messages in `application/`/`infrastructure/`;
  `print` only in `presentation` output paths.

## Error handling

- Typed hierarchy in `sender/domain/errors.py`: `ApiError` → `DaftraApiError`,
  `WhatsAppApiError`. Network failures, non-2xx, and envelope-level
  `result: failed` all surface as these.
- **Retry only HTTP 429** (and, inside the poller, network/timeout and 5xx).
  Everything else is treated as permanent — see the retryable/permanent split in
  `poller.py` and `docs/design.md`.
- `presentation/cli.py` catches `ApiError`/`ValueError`/`RuntimeError`, prints
  one line to stderr, and exits `1`.
- Never let one tenant's failure stop the other tenants: the poller isolates
  per-app errors on purpose (`InvoicePoller.run_once`).

## Configuration

- **All external knobs come from `.env`**, read via `load_dotenv()` and
  normalized by `infrastructure/config.py` into a frozen `Settings` dataclass.
  No new config file format, no new source; add a field there.
- Secrets are excluded from `Settings.__repr__`. Never hardcode or log a key.
- Per-tenant config uses the unprefixed vars for app 1 and `DAFTRA2_*` /
  `DAFTRA3_*` slots for extra apps; **each app name is derived from its account
  subdomain**, so the name is stable and owns its state.
- WhatsApp credentials are **lazily required** — only `send`/`preview`/`poll`
  demand them, so `show`/`list` work with just the Daftra key. Preserve this.
- **An offline rehearsal needs `--meta-stub`, not just a stub source**
  (`--invoice-stub` / `--payment-stub`). `cli.py` gates `need_whatsapp` on
  `not meta_stub`, so `--meta-stub` alone makes a run work with no token at all.
  Rationale in [`decisions.md`](decisions.md) § 9. *(An earlier note here claimed
  `poll --stub` still demanded live credentials; that was fixed with the flag.)*
- The payments pipeline reads its own knobs (`POLL_PAYMENTS_*`,
  `WHATSAPP_PAYMENT_TEMPLATE_*`) through the same `Settings` and deliberately
  shares `POLL_INTERVAL`/`POLL_MAX_PAGES`/`POLL_MAX_BACKOFF`/`POLL_MAX_SEEN`/
  `WHATSAPP_FREEFORM_FALLBACK`/`REPORT_*` with the invoice one. Full table:
  [`docs/guide/payments.md`](../../docs/guide/payments.md) § Configuration.
- Reference for every variable: `docs/guide/getting-started.md` § Configure and
  `.env.example` (which now carries the poll-cap, `REPORT_*` and `POLL_PAYMENTS_*`
  knobs).

## Ports and adapters

- The application layer depends on protocols in `sender/domain/ports.py` only.
  Adding a capability to the outside world means adding a `Protocol` there and
  an implementation in `infrastructure/`, never importing an adapter into
  `application/`.
- Attachment mode (`upload` / `link` / `none`) is a **domain port**
  (`InvoiceAttachmentProvider`), not a CLI flag threaded through the layers.

## Testing

- `pytest` with `testpaths = tests` in `pytest.ini` — scoped explicitly so the
  vendored deps' own suites are not collected.
- Shared doubles live in `tests/fakes.py` (`CapturingSender`, `FailingSender`,
  `FakeSession`, `FakeResponse`); **prefer extending these over new ad-hoc
  fakes**. `FakeSession` records `calls`, `urls`, and `requests_log`.
- **Live tests are opt-in.** `tests/conftest.py` skips them unless
  `WHATSAPP_LIVE_TESTS` is truthy *and* credentials/recipient are real (not
  placeholder-looking). Never weaken that gate — a checkout that happens to carry
  credentials would otherwise message real customers on every `pytest` run.
- Tests assert against ports with fakes; they may import `infrastructure`
  directly for adapter-level tests.
- `InvoicePoller`'s send cap defaults to `10`, so a poller test that expects
  more than 10 sends — or that asserts paging/saturation across two cycles —
  must pass `max_sends_per_run=0` explicitly. A burst split across cycles is the
  usual false failure. The default lives on `DocumentPoller`, so `PaymentPoller`
  inherits it identically.
- Each pipeline's behaviour is pinned by a parallel set of test modules —
  `test_poller.py`/`test_payment_poller.py`, `test_reporting.py`/`test_payment_reporting.py`,
  `test_daftra_payment_mapper.py` for the payments wire mapping, and
  `test_payment_payload_contract.py` / `test_payment_freeform_fallback.py` for
  what Meta would actually receive. **A change to one pipeline should be checked
  against its twin's suite too** — the shared engine means a behaviour change
  usually lands on both.
- `tests/test_multi_recipient.py` is the cross-pipeline spec for fan-out and
  partial delivery (all three pipelines, plus the manual-send paths). Its
  `FailingOnNumberSender` fails **by recipient**, which is the only way to
  express "the second number was rejected" as distinct from "the document was
  rejected" — an invoice-level sender cannot tell them apart.
- Run with `.venv/bin/python -m pytest -q`.

## Files and docs

- `docs/` is a breadcrumb-linked tree; each page ends with drill-down links.
  New pages follow the same shape.
- `vendor/` and `HANDOFF-*.md` are **gitignored** (generated / machine-local).
  `sender/infrastructure/fonts/` **is** tracked (the font subset and its OFL
  licence must ship).
- `logs/` is **gitignored except the tracked `logs/.gitkeep`** — daily poll logs
  carry customer invoice ids, so the placeholder keeps the directory in a fresh
  clone without shipping content. `poll.log` is gitignored as the legacy
  pre-`run_poll.sh` log.
- `poll_state.json`, `poll_payments_state.json`, `template_state.json`, `.env`,
  `stub_invoices.json`, `stub_payments.json` are gitignored runtime artifacts.
  They are machine-specific: copying one onto another machine silently marks
  documents as already handled.
- `reports/` and `reports.stub/` hold **customer PII** (names, amounts,
  recipient numbers) and are now gitignored too, like every other runtime
  artifact here. Treat them as machine-local regardless.
- Remote is SSH-only (`git@github.com:lawaty/Aizen-Papers.git`); HTTPS auth
  hangs. Branch `main`.
