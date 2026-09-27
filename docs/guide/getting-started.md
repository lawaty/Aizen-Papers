# Getting started

**Breadcrumb:** [Home](../index.md) / [Guide](.) / [Getting started](getting-started.md)

---

## Prerequisites

- Python **≥ 3.10** (developed against 3.14).
- A **Daftra** account with an API key (Settings → API → API Key) that has the
  required permissions enabled.
- A **Meta WhatsApp Business Cloud** app with:
  - an app `access_token`,
  - a registered `phone_number_id`,
  - the approved template `aizen_invoice`,
  - in test mode: the recipient's number added to the app's **verified numbers**.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Running the test suite needs pytest as well: `pip install -r requirements-dev.txt`.
The live WhatsApp tests stay skipped unless you opt in with `WHATSAPP_LIVE_TESTS=1`
and a `WHATSAPP_VERIFIED_RECIPIENT` (see `tests/test_whatsapp_live.py`).

## Configure

Copy `.env.example` to `.env` and fill in the values:

| Variable | Required | Purpose |
|---|---|---|
| `DAFTRA_API_KEY` | yes | Daftra API key for app 1 (never committed) |
| `DAFTRA_BASE_URL` | no (default `https://api.daftra.com/api2`) | App 1 API root, normally `https://<account>.daftra.com/api2` |
| `DAFTRA_TIMEOUT` | no (default `15`) | Daftra HTTP timeout (s) |
| `DAFTRA2_BASE_URL` | no | App 2 API root (second tenant); needs `DAFTRA2_API_KEY` too |
| `DAFTRA2_API_KEY` | no | App 2 Daftra API key |
| `DAFTRA2_TIMEOUT` | no (default `15`) | App 2 HTTP timeout (s) |
| `DAFTRA_APP2_NAME` | no | App 2 state/log name (default: derived from the subdomain) |
| `POLL_INTERVAL` | no (default `60`) | seconds between poll cycles |
| `POLL_STATE_PATH` | no (default `poll_state.json`) | poll state file (gitignored) |
| `POLL_LIMIT` | no (default `10`) | list page size per cycle |
| `POLL_MAX_SEEN` | no (default `500`) | most recent seen ids kept per app in the state file |
| `POLL_MAX_BACKOFF` | no (default `3600`) | cap (s) on the retryable-failure backoff |
| `POLL_MAX_PAGES` | no (default `5`) | max list pages fetched per cycle when the page is saturated |
| `STUB_INVOICES_PATH` | no (default `stub_invoices.json`) | offline stub fixture file (gitignored) |
| `POLL_STUB_STATE_PATH` | no (default `poll_state.stub.json`) | offline stub poll state file (gitignored) |
| `WHATSAPP_ACCESS_TOKEN` | `poll`, `send`, `preview`, `webhook-subscribe` | Meta app access token (never committed) |
| `WHATSAPP_PHONE_NUMBER_ID` | `poll`, `send`, `preview`, `webhook-subscribe` | the registered phone number id |
| `WHATSAPP_API_VERSION` | no (default `v25.0`) | Graph API version |
| `WHATSAPP_TEMPLATE_NAME` | no (default `aizen_invoice`) | approved template name |
| `WHATSAPP_TEMPLATE_LANG` | no (default `en`) | template language code |
| `WHATSAPP_FREEFORM_FALLBACK` | no (default `true`) | while the template is unusable (`132000`-series), send a free-form message instead (reusing the already-uploaded PDF); Meta only delivers those inside the 24-hour customer service window — set `off` to keep template sends only ([details](template-contract.md#while-the-template-is-pending-approval-free-form-fallback)) |
| `WHATSAPP_TIMEOUT` | no (default `15`) | Meta HTTP timeout (s) |
| `WHATSAPP_MAX_RETRY_WAIT` | no (default `30`) | cap (s) on the 429 `Retry-After` wait inside one send attempt; an unbounded wait would hold the poll lock for a long `--once` run |
| `WA_OWN_NUMBER` | no | own business E.164; blocks self-sends locally |
| `WHATSAPP_WABA_ID` | `webhook-subscribe`, `template-*` | WhatsApp Business Account id |
| `WHATSAPP_VERIFY_TOKEN` | `webhook-serve` | webhook callback verification token |
| `WHATSAPP_TEMPLATE_BUILDER` | no (default `auto`) | `auto` / `legacy` / `new`; any other value is rejected with a clear error (previously it silently fell back to `auto`) |
| `WHATSAPP_TEMPLATE_CACHE` | no (default `template_state.json`) | template-state cache file (gitignored) |
| `WHATSAPP_TEMPLATE_TTL` | no (default `300`) | seconds before the template status is re-checked |
| `INVOICE_ATTACHMENT` | no (default `upload`) | `upload` / `link` / `none` — how the invoice PDF is attached |
| `INVOICE_ATTACHMENT_CAPTION` | no (default empty) | optional document caption; leave empty, Meta does not document caption support for document headers |
| `WHATSAPP_DRY_RUN` | no (default `false`) | never really send |
| `DEFAULT_COUNTRY_CODE` | no (default `20`) | for local→E.164 phone conversion |
| `LOG_LEVEL` | no (default `INFO`) | logging level |

`INVOICE_ATTACHMENT` defaults to `upload`: the sender renders the invoice PDF
itself and uploads it to Meta, because Daftra has no PDF export and its own
`invoice_pdf_url` is behind a login. It needs no extra configuration. See
[Invoice PDF](invoice-pdf.md) for the modes and the trade-offs. `send`,
`preview`, and `poll` also accept `--attachment {upload,link,none}` to override
it for a single run.

Daftra authentication uses the API key sent as the `apikey` header — the
simplest supported method. Set `DAFTRA_API_KEY` to a key that has the required
permissions enabled in the Daftra web UI (Settings → API → API Key); a key with
no effective permissions returns `401 Unauthorized`. Set `DAFTRA_BASE_URL` to
your account-specific API root such as
`https://<account>.daftra.com/api2`; the retained fallback default may not
belong to your tenant.

**Two Daftra apps (tenants).** App 1 uses the unprefixed vars above. A second
app uses the numbered slot `DAFTRA2_*`; a slot with no `*_BASE_URL` or no
`*_API_KEY` is simply skipped, so a single-app deployment needs no extra vars.
Slots 2–9 are scanned, so a gap (e.g. `DAFTRA2_*` unset but `DAFTRA3_*` set)
does **not** drop the later app — it logs a warning about the gap. Two apps
that end up with the same name are warned about, because they would share poll
state. Each app's base URL must pass the same https +
`.daftra.com`/`.daftara.com` validation. The non-`poll` commands always talk to
app 1 (the first/only configured app).

## Commands

```bash
# Live payload for an invoice — sends nothing
python -m sender preview --invoice-id 26

# Show the normalized internal invoice
python -m sender show --invoice-id 26

# Show the raw Daftra JSON (schema changed? debug with this)
python -m sender show --invoice-id 26 --raw

# List recent invoices
python -m sender list --limit 10

# Send for real (or --dry-run to print payload instead of sending)
python -m sender send --invoice-id 26
python -m sender send --invoice-id 26 --dry-run

# Override the recipient (default: the customer phone on the invoice)
python -m sender send --invoice-id 26 --to 01027693262

# Template auto-switching (builder strategy)
python -m sender template-status                                    # cached/current state
python -m sender template-status --force                            # force a live refresh
python -m sender template-watch --interval 60 --timeout 3600        # poll until approved, exit 0
python -m sender send --invoice-id 1 --stub --builder new           # pin a builder manually
```

The builder is chosen automatically: while `aizen_invoice` is `PENDING` review
the legacy document-header builder is used; once it is `APPROVED` the sender
switches to the clean body-only builder and marks the legacy one deprecated (see
[Template contract](template-contract.md) for the full switch behavior).

### Polling for new invoices

`poll` watches every configured Daftra app and sends each *new* invoice to its
customer:

```bash
# Run forever, one cycle every 60s (default)
python -m sender poll

# One cycle, per-app summary on stdout, exit 0 (for cron/systemd)
python -m sender poll --once

# Bounded runs
python -m sender poll --max-cycles 5
python -m sender poll --timeout 3600
python -m sender poll --interval 120 --limit 25

# Rehearse without sending (also honors WHATSAPP_DRY_RUN)
python -m sender poll --once --dry-run

# Offline rehearsal with the stub source (never touches the real state file)
python -m sender poll --once --stub --dry-run
python -m sender poll --once --stub --dry-run --send-existing

# Inspect / reset the poll state (reset requires --yes and is per app)
python -m sender poll-status
python -m sender poll-status --stub
python -m sender poll-reset --app aizenpaper --yes
python -m sender poll-reset --app aizenpaper --invoice-id 123 --yes
```

### Running it from cron (every 5 minutes)

The one-shot form exists for exactly this: `poll --once` runs a single cycle,
prints a per-app summary to stdout, and exits — `0` on success, `1` if the run
failed or was interrupted mid-cycle. A cron entry that sends every new invoice
within five minutes looks like this:

```cron
*/5 * * * * cd "/home/lawaty/Projects/Daftra Project" && .venv/bin/python -m sender poll --once --timeout 240 >> poll.log 2>&1
```

- **Use the venv's python, not a bare `pytest`/`python` on PATH.** A pyenv/PATH
  shim can break under cron, where the environment is minimal (verified: the
  PATH shim fails with exit 127).
- **`--timeout 240` bounds a single run.** No HTTP call can hang (every call has
  a timeout, `Retry-After` is capped by `WHATSAPP_MAX_RETRY_WAIT`), but a run
  that legitimately takes longer than five minutes would otherwise overlap the
  next tick — which is safe (the loser exits 1, no double-send) but skips a
  cycle. The timeout stops the cycle at 4 minutes instead. If you want an
  external guarantee too, wrap the command in `flock -n`.
- **Redirect stdout/stderr to a log.** `--once` prints a summary to stdout every
  run; without a redirect cron emails you every five minutes. The first run of
  each app **seeds** without sending (see the warning above), so the first few
  summaries read `seeded N existing invoice(s) without sending`.
- **The lock protects you.** Each run takes an exclusive lock on
  `poll_state.json.lock`; if two runs overlap, the second exits `1` with
  `another poller is already running` — so an overlap is *noticed*, not silently
  double-sent.
- **A systemd timer** is equivalent: a `.service` unit running
  `ExecStart=/path/to/.venv/bin/python -m sender poll --once` with
  `OnCalendar=*:0/5` in the matching `.timer` unit, plus
  `StandardOutput=append:/path/to/poll.log`.

> **⚠️ First-run safety — read this before your first real `poll`.**
> The very first poll of an app sees *every historical invoice* as "new". To
> avoid blasting your whole customer base, the default first run **seeds** the
> state with the currently existing invoices and sends nothing (it logs exactly
> what it did). Only pass `--send-existing` when you deliberately want that
> first run to send the existing invoices too. After the first run, only
> genuinely new invoices are sent.

Per-cycle progress goes through `logging` (stderr); `--once` prints a concise
per-app, per-invoice summary to stdout. Failures are classified:

- **Retryable** (network/timeout, HTTP 429, HTTP 5xx) are never given up. They
  are recorded as `pending` in the state and retried with a bounded exponential
  backoff (capped by `POLL_MAX_BACKOFF`); the summary shows them as `pending`
  while they wait and `failed` on the cycle they are attempted.
- **Permanent** (validation, missing `public_url`, self-send, any other
  non-transient error) are given up on the first attempt: the invoice is marked
  seen and recorded as `abandoned` in the state so `poll-status` shows it and
  `poll-reset --invoice-id` can re-drive it.
- **Template contract failures** (the `132000`-series, e.g. `132001` "no
  approved template for the language `en`") are the exception: an operator can fix
  them, so the invoice is never abandoned. With
  `WHATSAPP_FREEFORM_FALLBACK` on (the default) it is delivered as a free-form
  message — reusing the PDF the rejected send already uploaded — and counted as
  `fallback_sends`; if that fails too, or the fallback is off, the invoice stays
  `pending` on the *template* error. See
  [Template contract](template-contract.md#while-the-template-is-pending-approval-free-form-fallback).

An invoice with no usable phone is skipped with a WARNING and marked seen so it
is never retried. If one app fails (network, bad key, …) the other apps still
run in the same cycle; the process exits non-zero only when *every* app failed.
SIGINT/SIGTERM finish/abort the current send cleanly, log a final summary, and
exit `0` in daemon mode — but a `--once` run killed mid-cycle exits `1`, so a
cron scheduler can tell "completed" from "killed".

> **⚠️ `--dry-run` is non-mutating.** `poll --dry-run` (and
> `WHATSAPP_DRY_RUN=true`) builds the payloads, logs what would be sent, and
> writes **nothing** to the poll state — no invoice is marked seen, no
> `last_poll_at` is recorded. A real run afterwards sends everything the dry
> run rehearsed. This matches `send --dry-run`.

> **⚠️ Concurrency.** Two pollers must not run against the same state file at
> the same time (e.g. a stray manual run overlapping a cron/systemd `--once`
> run): they could double-send. The real `poll` path takes an advisory lock
> (`poll_state.json.lock`); a second poller fails fast with a clear error. The
> `--stub` path is a single-user demo and does not lock.

> **⚠️ Saturated listings.** If more invoices are created between two cycles
> than `--limit`, the poller pages forward to catch up (up to `POLL_MAX_PAGES`
> pages) and logs a WARNING when the listing comes back full. If you regularly
> see that warning, raise `--limit`.

### Offline mode with the Daftra stub

Without a live Daftra key/connection, every command accepts `--stub` and reads
from the built-in sample invoices (INV-001, INV-002, INV-003):

```bash
# Send a stub invoice (customer phone defaults to the stub's number)
python -m sender send --invoice-id 1 --stub

# Send a stub invoice to a specific number
python -m sender send --invoice-id 3 --stub --to 01027693262

# Preview / show / list the same stub data
python -m sender preview --invoice-id 1 --stub
python -m sender show --invoice-id 1 --stub --raw
python -m sender list --stub
```

`poll --stub` is a **persistent offline demo** of the real objective — "a new
invoice appears → it gets sent". It uses two dedicated files (gitignored,
separate from the real `poll_state.json`): `stub_invoices.json` (the fixture
queue) and `poll_state.stub.json` (the stub poll state). The one-shot `--stub`
commands read the fixture when it exists, so they see the same data:

```bash
# 1. First run seeds the stub state (sends nothing)
python -m sender poll --once --stub

# 2. A new invoice appears in the fixture
python -m sender stub-add

# 3. The next run detects only the new invoice and sends it
python -m sender poll --once --stub --dry-run

# Inspect / reset the stub demo state
python -m sender poll-status --stub
python -m sender poll-reset --app stub --yes --stub
```

Stub mode never touches the network: the template builder is pinned to
`legacy` (or `--builder new`) instead of resolving via the Graph API.

### Delivery-status webhook

Per-message delivery is invisible to the send API (a `200` + `wamid` only means
"accepted"), so delivery is observed with a webhook:

```bash
# 1. Start the receiver, expose it with a tunnel, e.g.:
python -m sender webhook-serve --port 8080 --events-file webhook_events.jsonl
#    ...in another terminal:  ngrok http 8080   (or cloudflared tunnel --url http://localhost:8080)

# 2. In the Meta App Dashboard, paste the https tunnel URL + WHATSAPP_VERIFY_TOKEN
#    and click Verify & Save (the receiver echoes hub.challenge).

# 3. Enable the subscription once (needs WHATSAPP_WABA_ID set):
python -m sender webhook-subscribe

# Status events (sent/delivered/read/failed) are appended to the events file as JSONL.
```

Exit code is `0` on success, `1` on a handled error (message on stderr).

## Safety check before your first real send

1. `python -m sender preview --invoice-id <id>` — confirm the 4 parameters and
   the `to` E.164 number in the printed JSON.
2. `python -m sender send --invoice-id <id> --dry-run` — same, but with the send
   path.
3. Only then drop `--dry-run` for one **verified** test number.

## Related

- [Template contract](template-contract.md) — what the 4 parameters map to.
- [Delivery status](delivery-status.md) — why `200` isn't delivery, and the webhook.
- [Architecture](../architecture.md) — how the pieces fit.
- [Domain layer](../layers/domain.md) — normalization & formatting rules.