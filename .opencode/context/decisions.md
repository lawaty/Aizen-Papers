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
