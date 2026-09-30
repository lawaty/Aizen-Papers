# Decisions

**The canonical decision log for this project is
[`docs/design.md`](../../docs/design.md)** — 10 numbered rationales (layered DDD,
external truth stays external, template builder, phone normalization, safe by
default, Meta constraints, minimalism, hand-rolled PDF, attachment-as-port, the
poller). Read it before proposing an architectural change, and add to it rather
than duplicating here.

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

---

## Known documentation drift (found 2026-09-30 and 2026-10-01, not yet fixed)

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
- `docs/guide/rate-limits.md` and `docs/guide/reporting.md` are cited from
  `poller.py` and `infrastructure/reporting.py` **but do not exist**.
- The 7 new env knobs (`POLL_MAX_SENDS_PER_RUN`, `REPORT_*`) are absent from
  `.env.example` and `docs/guide/getting-started.md`.
- `reports/` and `reports.stub/` are untracked but **not** in `.gitignore`, unlike
  every other runtime artifact; they hold customer PII.
- The send-report subsystem has no `tests/test_reporting.py`; rendering,
  retention and the `.htaccess` are unverified.
