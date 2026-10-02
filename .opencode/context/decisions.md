# Decisions

**The canonical decision log for this project is
[`docs/design.md`](../../docs/design.md)** — 12 numbered rationales (layered DDD,
external truth stays external, template builder, phone normalization, safe by
default, Meta constraints, minimalism, hand-rolled PDF, attachment-as-port, the
poller, the second payments pipeline, the third customers pipeline). Read it
before proposing an architectural change, and add to it rather than duplicating
here.

This file records only decisions **not** already in `docs/design.md`.

---

## 1. Manual FileZilla deployment, no CI/CD

There is no pipeline, no container, no build step for the app itself. `sender/`
is uploaded to a shared host and updated by hand; `vendor/`, `.env`,
`poll_state.json`, and `template_state.json` stay **server-side**.

*Why:* the host is a FileZilla/cPanel box with no shell package manager. The
repository deliberately models the app as a plain source tree that runs in place.

*Consequence:* the machine-specific state files must never be uploaded from
development — doing so marks real invoices as already handled and silently
suppresses deliveries.

The host itself:

- SSH alias **`AizenPaper`** (port `21098`, user `aizewlkt`); port 22 is blocked.
  SSH prints a harmless post-quantum key-exchange WARNING on stderr — filter with
  `grep -v "WARNING: connection\|store now\|openssh.com/pq"`.
- The only usable interpreter is **`/opt/hc_python/bin/python3.12`** (3.12.14).
  The host default `/bin/python3` is 3.6.8 and **cannot run this app**: the code
  needs >= 3.10 (`from __future__ import annotations` plus PEP 604 `str | None`
  annotations). Every operator command and the cron line must use the full path.

> Confidence: high. The live cron line is committed in the `tools/run_poll.sh`
> header; machine-local detail stays in the gitignored `HANDOFF-deploy.md` (never
> commit it).

---

## 2. `vendor/` is a generated, gitignored artifact

The two runtime dependencies are vendored as pure Python so the app runs with
**zero pip** on a host that has no package manager. `tools/build_vendor.py`
regenerates the tree; it is explicitly "not source" per `.gitignore`.

*Why:* it keeps the standard-library-first footprint (design decision 7) while
still running on a host that cannot install anything.

*Consequence:* adding a runtime dependency requires a matching entry in
`tools/build_vendor.py: PACKAGES`, or production fails at import with a
`ModuleNotFoundError` that local development never reproduces. The font subset
under `sender/infrastructure/fonts/` is the deliberate exception: it *is* tracked,
because it is licence-bound content, not a dependency.

---

## 3. A repository context map exists

`.opencode/context/` is maintained by the `context-manager` agent and is a
navigation index, not documentation. It deliberately points at `docs/` rather
than restating it.

*Why:* `docs/` is thorough (11 pages) but is written for humans reading it in
order; an agent needs "which files do I open first" in one screen.

*Rule:* one fact has one home. If a fact is already in `docs/design.md` or
`docs/architecture.md`, this map links to it.

---

## 4. Live-test opt-in is a security boundary, not a convenience

`tests/conftest.py` skips the live WhatsApp tests unless `WHATSAPP_LIVE_TESTS` is
truthy *and* the credentials and recipient are real rather than
placeholder-looking. > Confidence: high — the docstring states the reason
directly: a checkout that happens to carry credentials would otherwise message a
real customer on every `pytest` run.

---

## 5. Per-day poll logs come from a committed shell wrapper, not an in-app handler

`tools/run_poll.sh` wraps `python -m sender poll` for cron. It resolves the
project root from its own location, `cd`s in, appends each run as a delimited
section to `logs/YYYY-MM-DD.log`, prunes files older than 30 days, and forwards
args and the exit code verbatim.

*Why not a logging handler in the app:* the per-app `--once` summary is
`print`ed to stdout (`cli.py:584`), which a handler never sees, and the app's log
format carries no timestamps (`cli.py:669`). A shell wrapper captures both, and
keeps retention out of the app.

*Consequence:* `$PYTHON` must be set to a 3.10+ interpreter (the script defaults
to `python3`, which is 3.6.8 on the production host and fails). A `RUN START`
without a `RUN END` is how a killed run is recognised — do not "tidy" the
delimiters. Earlier guidance to truncate the single `poll.log` by hand is now
obsolete: the wrapper rotates daily with 30-day retention.

---

## 6. The send cap is a third limit, separate from the paging bounds

`limit`/`max_pages` bound what the poller can **see**; `max_sends_per_run`
(default 10, `POLL_MAX_SENDS_PER_RUN`) bounds how many messages one cycle may put
in front of Meta. Budgeted across all apps and consumed at the send site, so a
failed send still costs budget.

*Why:* paging can surface up to `limit * max_pages` invoices, so a page size alone
is not a bound on outbound volume. The default sits far below Meta's published
throughput and exists mainly to stop a pathological backlog from blasting one
number.

*Consequence:* a capped invoice is left **unseen**, and the next cycle pages past
all-seen pages while draining. Do not "simplify" this by marking capped invoices
seen, or by treating a fully-seen page as end-of-backlog unconditionally — either
turns a throttle into silent data loss. `0` disables the cap and must not be read
as a budget of zero.

---

## 7. The send report is an audit log with an inverted failure contract

JSONL per day under `REPORT_DATA_DIR` is the record; the HTML pages are a
**derived** rendering, regenerated by `python -m sender report`. Hence
`SendOutcomeRecorder` is a port separate from `PollStateStore`, and its
implementation deliberately **swallows write failures** — the opposite of the
state store's contract.

*Why:* losing a state write means re-sending an invoice; losing a report row must
never fail a cycle that already reached customers. Keeping the record separate
also gives the report its own retention instead of the state file's
`poll_max_seen` bound, and keeps a machine-specific, gitignored artifact from
doubling as an operator-facing one.

*Consequence:* renderer bugs are recoverable by re-running `report`, and
regeneration is idempotent so a manual run racing a cycle is harmless. The
generated `.htaccess` blocks `*.jsonl` and listings — that is not access control;
the pages still belong behind `.htpasswd` or outside the docroot.

The second consequence of *one* audit trail is that the renderer must stay
**kind-neutral**: every pipeline lands in the same day pages, so a column header
cannot claim "Invoice #" and each row says which kind it is (`SendOutcome.kind`
badge). Adding a pipeline therefore obliges no schema change — but reverting the
neutral headers to invoice wording is what would make a mixed day lie.

---

## 8. A customer-facing document is never built from a partially-populated row

Daftra's listing endpoint returns no `InvoiceItem` (see `contexts.md` § 7). The
poller re-fetches the invoice detail whenever a candidate row cannot answer both
"where is this going" and "what is on the invoice", and **a failed detail fetch
fails the send** rather than proceeding with what it has. The PDF writer
independently warns on a totalled invoice with no rows.

*Why:* this was a deliberate change, not a bug fix. The poller originally treated
the list row as authoritative because the message was text only and the two
payloads were identical. That equivalence broke the moment the template grew a
header document — the document is this sender's *own rendered PDF* — so the same
data gap that produced an identical text message began producing a PDF whose
items table tells the customer their invoice is empty. It generalizes: a source
row's completeness requirement is a function of what the payload does with it, so
adding a document to the template obliges the poller to fetch more.

*Consequence:* do not relax `_needs_detail` back to a phone check, and do not
"optimize" it into an origin check ("did this come from a listing?"). Skipping
the fetch is the one change that makes the sender state something false. The cost
is bounded: the fetch sits inside `_handle_document`, which the send cap already
gated, so Daftra read traffic scales with invoices sent, not with the page size.
Note this decision is **not** in `docs/design.md` — the layer walkthroughs
describe the rule but the log records no rationale.

---

## 9. Stubbing the source and stubbing the sender are different flags

`--invoice-stub` / `--payment-stub` replace the **Daftra source** with a fixture.
`--meta-stub` replaces the **Meta sender** with `StubMessageSender`, which captures
payloads instead of sending them. They are independent, and only the second one
makes a rehearsal safe.

*Why:* a stubbed source says nothing about the sender, so `--payment-stub` (or
the older `--stub`) alone reads fixture data and then **really messages whoever
the fixture names**. The obvious single `--stub` flag makes "offline" mean two
different things depending on which half you look at, and the failure mode is a
real customer receiving a fixture's message. Splitting the flags makes the safe
rehearsal an explicit, visible choice: `--payment-stub --meta-stub --dry-run`.

*Consequence:* do not fold the sender stub back into a source stub, and do not
treat `--meta-stub` as implying the others. It is also what makes a rehearsal run
on a machine with no credentials at all — `cli.py` gates `need_whatsapp` on
`not meta_stub`. The rationale is stated only in
[`docs/guide/payments.md`](../../docs/guide/payments.md) § *Rehearsing offline*,
not in `docs/design.md`.

---

## 10. The Daftra mapper warns far more than it raises, and leaves tax unmapped on purpose

Tax, discount and deposit are **deliberately not modelled** — `Invoice` has no
field for them and the PDF totals block has no row — but their presence is
**warned** about (`_warn_unmapped_money`). Everywhere else the rule has the same
shape: an **absent** value warns and renders a default; a **present but
unparseable** money value still raises.

*Why:* a `ValueError` escaping the mapper reaches the poller, which classifies it
**permanent** and would retire a real invoice the first time a tenant sends a shape
it has never seen — over a field that does not even reach the customer's document.
Daftra also uses `null` for "not applicable" money, so raising on absence would fire
on ordinary invoices. Silence is not acceptable either: the mapper is the only
layer that ever sees the raw payload, so money present in it and absent from the
PDF with nobody told is unrecoverable. The warning is a tripwire that fires the day
a tenant enables VAT.

*Consequence:* do not map tax/discount/deposit "while you're there" — that is a
feature built against a schema no account currently sends. Do not silence
`_warn_unmapped_money`, and do not replace `_is_money_present` with a truthiness
test (Daftra sends `deposit: "0"`, so a bare test warns on the whole ledger and
trains the operator to ignore it). Pinned by `tests/test_daftra_mapper.py`.

---

## 11. A document is addressed to a set of numbers, and a partial delivery is finished rather than repeated

Daftra keeps two phone fields on a client and either may be filled, so every
document carries `customer_phones: tuple[str, ...]` and a send fans out to each
distinct normalized number, in the model's order.

*Why:* a customer who gave the business two numbers asked to be reachable on
both, and a single-field model quietly dropped one. The hard half is not the
fan-out but the **retry**: once a document can be half-delivered, "retry the
document" stops being safe, because it messages again the number that already
got it — a duplicate invoice is worse than a late one. So the recipients reached
are persisted per app (the `delivered` map in the state file, cleared by
`mark_seen`) and a later cycle sends only what is outstanding. It is on disk
rather than in memory for the same reason as `draining`: production runs one
`poll --once` process per cron tick.

*Consequence:* `mark_seen` moved out of the send into `_handle_document` and now
fires only when **every** number was served. Do not push it back down into the
per-recipient path, or a document is retired after its first number. Write the
`delivered` record only for a **retryable** failure or a cap deferral — a
permanent failure has already retired the document, and a record after that is
stale state. A permanent failure on one number gives the whole document up, which
is a deliberate choice: the operator has to fix the record, not half-mess a
customer. Because one POST costs one unit of the per-run send cap, the cap can
now bite *between* two recipients; that must defer, never count as a failure.
`--to` remains a single-recipient override — honouring the operator's explicit
number literally is the point of it.

*Not in `docs/design.md`.* The rules themselves live in
[`docs/layers/application.md`](../../docs/layers/application.md) § *Sequencing
rules owned here* and `docs/guide/customers.md` § *Phone*; the behavioural spec is
`tests/test_multi_recipient.py`. See `contexts.md` § 2 and § 9.

---

## Known documentation drift (found 2026-09-30, rechecked 2026-10-01, 2026-10-02 and 2026-10-03)

These contradict current production state; source and `HANDOFF-deploy.md` win.

- `.env.example` and `docs/guide/template-contract.md` still describe the
  `DOCUMENT` revision as "under review" and ship
  `WHATSAPP_FREEFORM_FALLBACK=true`; the live template is approved and the
  production `.env` uses `false`.
- The `vendor/` bootstrap and `tools/build_vendor.py` are documented nowhere in
  `docs/` — only in the gitignored handoff note and this map.
- `docs/guide/getting-started.md` § *Running it from cron* still shows the raw
  `>> poll.log 2>&1` form and a systemd `StandardOutput=append:/path/to/poll.log`
  unit. That predates `tools/run_poll.sh`.
- The layer dependency rule is stated in the docs but has **no automated
  import-lint test**, so it is enforced by review alone.
- `docs/design.md:106` still says "Retries are restricted to HTTP 429 (rate
  limit), which is the one transient". `poller._is_retryable` now also retries on
  payload codes `4`/`80007`/`130429`/`131056`, which arrive as HTTP 400, and
  treats `131047`/`131048`/`131049` as permanently non-retryable quality signals.
  The decision log needs a clause; the source is authoritative.
- `poller.py:159` still cites `docs/guide/rate-limits.md`, which **does not
  exist**. (`docs/guide/reporting.md`, cited the same way from
  `infrastructure/reporting.py`, now does exist.)
- `docs/layers/infrastructure.md` § mapper still describes only the **invoice**
  path: `DaftraPaymentMapper` / `DaftraCustomerMapper` and every tripwire
  (`_warn_unmapped_money`, the absent-item and unparseable-date warnings, the
  exponent guard) are undocumented there. Read the mapper source and
  `contexts.md` § 7, not that section. See § 10.
- The `POLL_MAX_SENDS_PER_RUN` and `REPORT_*` knobs are now in `.env.example`
  but still absent from `docs/guide/getting-started.md` § *Configure*.
