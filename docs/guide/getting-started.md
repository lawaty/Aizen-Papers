# Getting started

**Breadcrumb:** [Home](../index.md) / [Guide](.) / [Getting started](getting-started.md)

---

## Prerequisites

- Python **≥ 3.10** (developed against 3.14).
- A **Daftra** account with an API key (Settings → API Access).
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

## Configure

Copy `.env.example` to `.env` and fill in the values:

| Variable | Required | Purpose |
|---|---|---|
| `DAFTRA_API_KEY` | always | Daftra API key (never committed) |
| `WHATSAPP_ACCESS_TOKEN` | `send`/`preview` | Meta app token |
| `WHATSAPP_PHONE_NUMBER_ID` | `send`/`preview` | business phone number id |
| `DAFTRA_BASE_URL` | no (default `https://api.daftra.com/api2`) | Daftra endpoint root |
| `DAFTRA_TIMEOUT` | no (default `15`) | Daftra HTTP timeout (s) |
| `WHATSAPP_API_VERSION` | no (default `v25.0`) | Graph API version |
| `WHATSAPP_TEMPLATE_NAME` | no (default `aizen_invoice`) | approved template name |
| `WHATSAPP_TEMPLATE_LANG` | no (default `en_EG`) | template language code |
| `WHATSAPP_TIMEOUT` | no (default `15`) | Meta HTTP timeout (s) |
| `WHATSAPP_DRY_RUN` | no (default `false`) | never really send |
| `DEFAULT_COUNTRY_CODE` | no (default `20`) | for local→E.164 phone conversion |
| `LOG_LEVEL` | no (default `INFO`) | logging level |

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
- [Architecture](../architecture.md) — how the pieces fit.
- [Domain layer](../layers/domain.md) — normalization & formatting rules.