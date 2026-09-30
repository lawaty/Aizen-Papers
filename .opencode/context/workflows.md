# Runtime workflows

The full sequence diagrams live in
[`docs/architecture.md`](../../docs/architecture.md) § *Runtime flow*. This file
gives the trace order and the traps.

---

## 1. `python -m sender send --invoice-id N`

`__main__.py` → `cli.main` → `build_service()` → `InvoiceNotificationService.send_invoice`

1. `DaftraClient.get_invoice` → `GET /invoices/{id}.json` (`apikey` header).
2. `DaftraInvoiceMapper.to_invoice` → normalized `Invoice`.
3. Builder resolves the attachment port — on the legacy builder in `upload` mode
   this **renders a PDF and uploads it to Meta even for `preview`/`--dry-run`**,
   because the payload must carry a real `media_id`.
4. `WhatsAppClient.send` → `POST /{phone_number_id}/messages` → `wamid`.

`--dry-run` stops before step 4. `show` / `list` never build a message and never
resolve the builder, so they work with only the Daftra key.

**Traps:** a `132000`-series rejection triggers a forced template-state refresh
and **one** retry with the other builder (`cli._send_template_with_retry`); a
`--freeform` send is a bridge, not a delivery mode (Meta only delivers free-form
inside the 24-hour service window).

---

## 2. `python -m sender poll` (the production path, cron `--once --timeout 240`)

`cli._run_poll` takes an advisory `flock` on `poll_state.json.lock` → constructs
`InvoicePoller` (one `PollApp` per tenant, in config order) → `run_once()`:

per app →
1. `_list_candidates` — list page 1, **page forward** while the page is saturated
   (bounded by `max_pages`) until already-seen territory, so a burst cannot fall
   off the page.
2. If the app is unknown to the state store: **seed all existing ids, send
   nothing** (unless `--send-existing`).
3. Per new invoice: if the list row has no phone, re-fetch the invoice for the
   authoritative one. No usable phone → mark seen, never retried.
4. Build → send → **mark seen only after the send attempt**.
5. Classify: network/429/5xx → `pending` with bounded exponential backoff
   (`min(interval * 2^(count-1), max_backoff)`), never given up. Everything else
   → `abandoned`, visible in `poll-status`, re-drivable via
   `poll-reset --invoice-id`.

then `set_last_poll_at`. One app raising never skips the rest. Exit code is
non-zero only if *every* app failed.

In production the entry point sits one level up: cron calls the committed
wrapper `tools/run_poll.sh --once --timeout 240`, which resolves the project
root from its own location, `cd`s in (state and log paths are CWD-relative),
and appends each run to `logs/YYYY-MM-DD.log` as `RUN START` / `COMMAND` /
combined stdout+stderr / `RUN END ... exit=<n> elapsed=<s>`. A `RUN START` with
no matching `RUN END` means the run was killed, not finished. The same script
prunes daily logs older than 30 days, so there is no second cron entry to
maintain. `$PYTHON` must name a 3.10+ interpreter — the wrapper's `python3`
default does not work on the production host (see `decisions.md`).

**Traps:** the project-dir `cd` is load-bearing. A second concurrent poller
exits 1 by design. The first run per app seeds silently, so a fresh prod state
file shows `sent 0`.

---

## 3. Template revision switching

Only on `send`/`preview`/`poll`: `TemplateRegistry` reads cached
`template_state.json` (TTL-bounded) and queries the Graph API, tracking the last
**approved** revision's structure. `PENDING` is always re-fetched before a send;
a failed check falls back to cache, then to the legacy builder. While the
reviewed `aizen_invoice` revision is under review the account still serves the
old `DOCUMENT`-header revision, so `LegacyInvoiceTemplateBuilder` stays active
and PDFs are uploaded. Once the clean revision is approved, sends switch to
`CleanTextTemplateBuilder` and nothing is rendered or uploaded.

Inspect with `template-status --force`; wait for approval with
`template-watch`; retire the legacy path with `template-drop-legacy`.

---

## 4. PDF render → upload → attach

`infrastructure/pdf.py` writes the PDF (Arabic shaped by `arabic.py`, glyph ids
by `truetype.py`, logo by `png.py`/`logo.py`) → `MetaMediaUploader` performs the
two-phase upload → the returned `media_id` becomes the template header's
`document` object.

`upload` = bytes owned by us; `link` = `HostedLinkProvider` builds a document from
`invoice.public_url` (pure domain, no network); `none` drops the header component
rather than failing the notification. A mode that cannot produce a document
degrades to a header-less send — **the poller must never die on an attachment**.

Verify a render without sending: `docs/guide/invoice-pdf.md` § *Checking a PDF by
hand*.

---

## 5. Vendored-dependency bootstrap (deployment, not development)

`sender/__init__.py` appends a sibling `vendor/` to `sys.path` at import time if
present. Regenerate locally with `python tools/build_vendor.py` (defaults to
`./.venv`); it copies only Python source and strips `.so`, tests, and
`__pycache__` so the tree is interpreter-agnostic.

Deployment is manual (FileZilla upload + crontab), with no CI pipeline. The live
cron line is committed in the `tools/run_poll.sh` header comment; machine-local
host details (SSH alias, interpreter, project path, upload rules) are in the
gitignored `HANDOFF-deploy.md`, with the durable subset in
[`decisions.md`](decisions.md).
