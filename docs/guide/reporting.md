# Send reports

**Breadcrumb:** [Home](../index.md) / [Guide](.) / [Send reports](reporting.md)

---

## What it is

Every **actual send attempt** the poller makes is recorded, and the poll cycle
re-renders an HTML view of that history. There are two ways to read it:

- `index.html` → `YYYY-MM-DD.html` answers *"what happened on the 12th?"*.
- `recipients.html` answers *"what did we send this person, and did it fail?"* —
  a single searchable page over all history, filtered by phone, name, invoice or
  app, plus a sent / failed-abandoned filter. The filter is plain client-side
  JavaScript: the pages are static files with no server-side code behind them.

The raw record is an append-only JSONL log, and the HTML is *derived* from it.
That means a renderer bug is fixable by regenerating the pages without losing
history, and a failed regeneration can never destroy yesterday's report.

Only **real send attempts** are logged. A dry run, a no-op, an invoice skipped
for having no phone, a listing or payload failure, and an invoice deferred by the
per-cycle send cap are all events about the *pipeline*, not messages to a
customer, so none of them create a row.

## The data model

One JSON object per line, written by `JsonlSendOutcomeRecorder`, read back by
`ReportStore`. The shape is defined by `SendOutcome` in
`sender/domain/models.py`:

| field | meaning |
| --- | --- |
| `app` | the Daftra app the invoice belongs to |
| `invoice_id`, `invoice_number` | identifiers, the number is what the customer sees |
| `customer_name`, `customer_phone` | recipient |
| `currency`, `total`, `issue_date` | what the invoice says |
| `attempted_at` | epoch seconds |
| `status` | `sent`, `failed`, or `abandoned` |
| `error` | Meta's error text/code when it failed |
| `fallback` | whether the free-form fallback template was used |
| `wamid` | Meta's message id, for cross-referencing the webhook |

A single malformed historical line costs one row, not the whole report.

## Running it

```bash
# Rebuild every page from the log on demand.
python -m sender report

# One day only.
python -m sender report --date 2026-10-02

# Use the stub data directory instead of live history.
python -m sender report --stub
```

`report` needs no WhatsApp or Daftra credentials — it only reads and writes
files — so it is safe to run anywhere, including a laptop that has a copy of the
log. The normal poll cycle calls the same render at the end of each run, so the
pages stay current without a separate cron entry.

All paths come from the environment:

| key | meaning |
| --- | --- |
| `REPORT_ENABLED` | `false` turns the whole report off |
| `REPORT_DIR` | where the HTML is written (the browsable directory) |
| `REPORT_DATA_DIR` | where the JSONL lives — keep this **outside** the web root |
| `REPORT_STUB_DIR` | where `--stub` runs write instead |
| `REPORT_RETENTION_DAYS` | history to keep; `0` disables pruning |
| `REPORT_OBFUSCATE_PHONE` | mask the middle of numbers in the pages (default `true`) |

## Serving it safely

The pages contain customer names, invoices and phone numbers, so they must not
be public. The supported shape is a directory under the site's document root
behind **HTTP basic auth**, with the JSONL kept somewhere the web server cannot
reach at all:

```bash
# 1. A directory the web server serves, and a data directory the web server cannot.
mkdir -p /home/USER/public_html/reports
mkdir -p /home/USER/aizen-report-data

# 2. A password file OUTSIDE the web root.
htpasswd -c /home/USER/.htpasswd-reports aizen   # or: openssl passwd -apr1

# 3. Point the app at them (see .env.example for the full block).
#    REPORT_DIR=/home/USER/public_html/reports
#    REPORT_DATA_DIR=/home/USER/aizen-report-data

# 4. Build the pages once, then add the auth block to the generated .htaccess.
python -m sender report
```

The report root's `.htaccess` is **merged, not replaced**. The directives the app
generates live between two marker comments (`# BEGIN` / `# END Aizen invoice
sender generated block`); everything outside them belongs to the operator and is
returned untouched. So the five-minute cron cycle that re-renders the report
never clobbers the auth block you add:

```apache
# Operator-managed. Preserved by ReportStore.render().
AuthType Basic
AuthName "Aizen send reports"
AuthUserFile /home/USER/.htpasswd-reports
Require valid-user
```

The generated region adds two cheap extra doors closed regardless of auth:
directory listing is off, `*.jsonl` is denied by `<FilesMatch>`, and
`RedirectMatch 404 ^/(.*/)?data/` hides a `data/` subdirectory if one is ever
placed here. (The leading `(.*/)?` matters: under `/reports` the request path is
`/reports/data/...`, which a bare `^/data/` does not match.)

`REPORT_OBFUSCATE_PHONE=true` masks the middle of each number in the pages, so a
reader cannot text the customer. Set it to `false` only behind strong basic auth
and only when operators genuinely need the full number to do their job — it
exposes customer PII to anyone who has the password. The JSONL always holds the
full number; obfuscation is a rendering decision, not redaction of the source.

## Drill down

- [Delivery status & webhook](delivery-status.md) — what the `wamid` in a row points at.
- [Getting started](getting-started.md) — install and configure.
- [Template contract](template-contract.md) — why a send can fail with a Meta error code.
