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
3. Per new invoice: **re-fetch the detail** when the row lacks the phone *or* the
   items. The listing carries no `InvoiceItem` at all, so this is the normal path,
   not the exception — see `contexts.md` § 7. The cap was checked before this, so
   the extra read costs one request per invoice actually being sent. No usable
   phone → mark seen, never retried. A failed detail fetch **fails the send**: no
   PDF was built, so the invoice stays `pending` rather than going out wrong.
4. Build → send → **mark seen only after the send attempt**. The send budget is
   claimed at the send site, so a failed send still consumes it.
5. Classify: network/429/5xx → `pending` with bounded exponential backoff
   (`min(interval * 2^(count-1), max_backoff)`), never given up. Everything else
   → `abandoned`, visible in `poll-status`, re-drivable via
   `poll-reset --invoice-id`. Payload-level Meta codes override the HTTP status
   (`_RETRYABLE_API_CODES` / `_PERMANENT_API_CODES`).

then `set_last_poll_at`. One app raising never skips the rest. Exit code is
non-zero only if *every* app failed.

After the run, and **outside the poll-state lock**, `cli._refresh_report_pages`
regenerates the HTML report for the dates touched and applies retention — see
§ 6. It is best-effort and cannot change the exit code.

If the cycle hits `POLL_MAX_SENDS_PER_RUN` (default 10, across all apps), the
overflow invoices are deliberately left **unseen** and counted as
`deferred_by_cap`; the next cycle drains them, paging past all-seen pages while
draining. A WARNING naming the env var is logged.

In production the entry point sits one level up: cron calls the committed
wrapper `tools/run_poll.sh --once --timeout 240`, which resolves the project
root from its own location, `cd`s in (state and log paths are CWD-relative),
and appends each run to `logs/YYYY-MM-DD.log` as `RUN START` / `COMMAND` /
combined stdout+stderr / `RUN END ... exit=<n> elapsed=<s>`. A `RUN START` with
no matching `RUN END` means the run was killed, not finished. The same script
prunes daily logs older than 30 days, so there is no second cron entry to
maintain. `$PYTHON` must name a 3.10+ interpreter — the wrapper's `python3`
default does not work on the production host (see `decisions.md`).

**`python -m sender poll-payments` is this same trace**, through `cli._run_poll_payments`
and the shared `cli._run_poll_common` (signal handling, the whole-run lock, the
summary, and the post-lock report refresh are one implementation for both, so the
two cannot drift). Three things differ and only three: the state path and its
lock, the send-cap knob (`POLL_PAYMENTS_MAX_SENDS_PER_RUN`), and the extra
Daftra hop — a payment row names no payer, so `DaftraClient.get_payment` reads
the linked invoice too, inside the adapter, only for payments about to be sent.
Everything in steps 1–5 above, including seeding, backoff, the deferred-by-cap
rule and the free-form fallback, is inherited. There is a **second cron entry**
setting `POLL_SUBCOMMAND=poll-payments`; the wrapper, the log format and the
lock discipline are shared, the state and the budget are not. See
[`contexts.md`](contexts.md) § 3.

**Traps:** the project-dir `cd` is load-bearing. A second concurrent poller
exits 1 by design — but each pipeline has its **own** lock, so they do not block
each other. The first run per app seeds silently, so a fresh prod state
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

---

## 6. Send report (`poll` side effect, plus `python -m sender report`)

per send attempt → `JsonlSendOutcomeRecorder.record` appends one JSON object to
`REPORT_DATA_DIR/<host-local-date>.jsonl` (`O_APPEND`, one write per line) → at
end of run `ReportStore.render()` rewrites `<date>.html` for every date it touched,
plus `index.html` and `recipients.html` (both rebuilt from **all** retained days,
not just the touched ones), via `write_text_atomic`; then `ReportStore.prune()`
deletes anything past `REPORT_RETENTION_DAYS` (`0` disables) — dated JSONL and
dated pages only, never the index. Every pipeline records into
**one** set of files, tagged by `SendOutcome.kind` (§ `contexts.md` 4).

The **JSONL is the record; the HTML is derived.** A renderer bug is therefore
fixable by re-running `python -m sender report` (with `--date` for one day, or
`--stub` for the offline directory) without losing history, and a failed
regeneration cannot destroy yesterday's page.

Dates are bucketed by **host-local** time, not UTC, matching how the poll summary
prints timestamps.

**Traps:** the report directory is meant to be browsable from a web root, so
`REPORT_OBFUSCE_PHONE` defaults on and a generated `.htaccess` blocks `*.jsonl`
and directory listing — that is not access control. The pages themselves still
want `.htpasswd` or to live outside the docroot. `reports/` and `reports.stub/`
hold customer PII and are now gitignored, but treat them as machine-local
regardless.
