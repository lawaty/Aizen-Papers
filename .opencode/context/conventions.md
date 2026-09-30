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
- **Known bug (flagged 2026-10-01, do not fix in passing):** `poll --stub` still
  demands live credentials. `cli.py:661` (`need_whatsapp`) lacks the
  `not args.stub` guard that lines 662-663 carry, so an offline stub rehearsal
  needs `WHATSAPP_ACCESS_TOKEN` + `WHATSAPP_PHONE_NUMBER_ID` in the environment
  (enforced at `config.py:232-239`). Offline rehearsals should not require them;
  if you touch that line, fix it deliberately and note it here.
- Reference for every variable: `docs/guide/getting-started.md` § Configure and
  `.env.example`. The report knobs (`REPORT_ENABLED`, `REPORT_DIR`,
  `REPORT_DATA_DIR`, `REPORT_STUB_DIR`, `REPORT_RETENTION_DAYS`,
  `REPORT_OBFUSCE_PHONE`) and `POLL_MAX_SENDS_PER_RUN` exist in `config.py` but
  are **not yet in `.env.example`** or `docs/`.

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
- `poll_state.json`, `template_state.json`, `.env`, `stub_invoices.json` are
  gitignored runtime artifacts. They are machine-specific: copying one onto
  another machine silently marks invoices as already handled.
- `reports/` and `reports.stub/` hold **customer PII** (names, invoice totals,
  recipient numbers) and belong on that list — but are **not in `.gitignore`
  yet**, unlike every other artifact here. Treat them as machine-local.
- Remote is SSH-only (`git@github.com:lawaty/Aizen-Papers.git`); HTTPS auth
  hangs. Branch `main`.
