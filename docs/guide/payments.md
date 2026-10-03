# Payments pipeline

**Breadcrumb:** [Home](../index.md) / [Guide](.) / [Payments](payments.md)

---

A second notification pipeline, parallel to the invoice one: it watches Daftra for
recorded **client payments** and confirms each new one to the customer under the
`aizen_new_payment` WhatsApp template.

A *client payment* is money received into a client's **account** — a deposit, a
prepayment, an opening balance. That is what `aizen_new_payment` talks about: it
says a payment was recorded **على حسابكم** ("on your account") and that the account
balance was updated. It never mentions an invoice. See
[What a "new payment" is](#what-a-new-payment-is) for the resource this does and
does not read.

It is the same machinery as the invoice pipeline — same state store, same
retry/abandon rules, same send cap, same free-form fallback, same HTML report —
running as its own command with its own state file, lock and budget. The
reasoning is in [design decision 11](../design.md); this page is how to run it.

## Commands

| Command | What it does |
|---|---|
| `python -m sender poll-payments` | Watch for new payments and confirm them (the production path) |
| `python -m sender payments` | List recent payments |
| `python -m sender show-payment --payment-id N` | Print a normalized payment (`--raw` for the Daftra JSON) |
| `python -m sender send-payment --payment-id N` | Send one confirmation by hand |
| `python -m sender poll-status --payments` | What has been seen, retried or abandoned |
| `python -m sender poll-reset --payments --app A --payment-id N --yes` | Re-drive one payment |
| `python -m sender stub-payment-add` | Add a payment to the offline rehearsal fixture |

`poll-payments` takes `--once`, `--interval`, `--limit`, `--max-cycles`,
`--max-sends`, `--timeout`, `--dry-run`, `--send-existing` and the stub flags. It
takes **no** `--builder` and **no** `--attachment`: the payment template has one
approved shape with no header document, so there is nothing to choose and nothing
to attach.

## The template contract

`aizen_new_payment`, language **`ar_EG`**, category UTILITY. It has a body and a
footer and **no header**, so:

- there is no document parameter — nothing is rendered and nothing is uploaded;
- the payload has exactly one legal shape, so the payments path does not consult
  `TemplateRegistry` (that exists only to choose between the two invoice builders);
- the language must be `ar_EG`. Anything else is a `132001` on every send.

Four positional body parameters, in this order:

| # | Body text | Value | Format |
|---|---|---|---|
| 1 | `مَرْحَبًا …` | customer name | sanitized, bidi-isolated |
| 2 | `رقم العملية` | payment **reference code** (`code`, e.g. `000244`) | isolated |
| 3 | `تاريخ الدفع` | payment date | `DD/MM/YYYY` |
| 4 | `مبلغ الدفعة` | amount | `1,500.00`, no currency symbol (`ج.م` is in the body) |

A mismatch here fails nowhere in this repo — it surfaces only as a
`132000`-series rejection from Meta, at send time, for a real customer. That is
why [tests/test_payment_payload_contract.py](../../tests/test_payment_payload_contract.py)
pins all of it.

## What a "new payment" is

Daftra's `GET /client_payments.json`, newest first. By default only **completed**
payments (`status=1`), filtered server-side, because the message says the account
balance was updated and only a completed payment does that. Set
`POLL_PAYMENTS_STATUS` blank to announce every row, or to `2`/`3`/… for another
status.

### It is not the invoice-payments endpoint

Daftra has two payment resources and they are **not two views of the same rows**:

| | `/client_payments.json` | `/invoice_payments.json` |
|---|---|---|
| what the money is | into a client's **account** | settling a specific **invoice** |
| rows carrying `invoice_id` | 0 of 109 | all of them |
| overlapping ids (live tenant) | **none** — 109 vs 125 | |
| the payer | inline on the row | only via the invoice |

The two id spaces are disjoint even though both run from 1 to ~245, so they are
never confused with each other — and the consequence is that switching is a **swap,
not an addition**: the 126 invoice payments this pipeline briefly announced are not
part of this resource, and are no longer announced.

The template settles which one is right. `aizen_new_payment` is written in account
terms throughout, so `/client_payments.json` is the resource it describes. Announcing
invoice payments through it would tell a customer their *account balance* was
updated when the money had actually settled a bill.

Unlike the invoice resource, this one **hides nothing**: it returned all 109 rows on
the live tenant across `cash`, `bank` and `manual_payment_19`, with no flag needed.
(For the record, `/invoice_payments.json` *does* exclude `client_credit` rows unless
sent `include_client_credit=1`, which is how that resource was losing 124 of 125
payments. That workaround is deliberately **not** sent here — it would be cargo cult
for a bug in a resource this pipeline does not read.)

### If you have just switched this pipeline over

The seen-set is what stops a newly-visible backlog from being announced all at once,
and it only helps if it already contains those rows. A state file written against the
*other* resource contains none of these ids, so the first cycle after switching would
otherwise treat all 109 as new. Seed first, with a page size large enough to hold the
whole backlog:

```bash
python -m sender poll-reset --payments --app <tenant> --yes
POLL_PAYMENTS_LIMIT=500 python -m sender poll-payments --once --dry-run   # check
POLL_PAYMENTS_LIMIT=500 python -m sender poll-payments --once             # then seed
```

The `--dry-run` reports what it *would* seed and writes nothing. Confirm with
`poll-status --payments` before letting cron resume.

## Why two Daftra calls per payment

A `ClientPayment` row names its payer by `client_id` and — on 99 of the 109 live rows
— carries the phone number inline. What it **never** carries is a business name: for
a company client the name lives on the `Client` record and the row's own
`first_name`/`last_name` are empty. So reading the listing alone would greet every
company customer as the generic "Customer".

Each new payment therefore costs:

1. `GET /client_payments/{id}.json` — the payment,
2. `GET /clients/{client_id}.json` — and its name and phones.

That join lives in the adapter (`DaftraClient.get_payment`), so the application
layer never learns Daftra's join order. It is bounded by the send cap: a backlog
costs listing pages, not one detail call per row.

A row with no `client_id` costs **one** request and keeps whatever phone it carried
itself; one with neither is unreachable and is skipped rather than guessed at.

## Configuration

All knobs live in `.env` (see `.env.example`). The ones that matter:

| Variable | Default | Meaning |
|---|---|---|
| `WHATSAPP_PAYMENT_TEMPLATE_NAME` | `aizen_new_payment` | The Meta template |
| `WHATSAPP_PAYMENT_TEMPLATE_LANG` | `ar_EG` | Must match the approved language |
| `POLL_PAYMENTS_STATE_PATH` | `poll_payments_state.json` | Gitignored, machine-specific |
| `POLL_PAYMENTS_LIMIT` | `10` | Listing page size |
| `POLL_PAYMENTS_MAX_SENDS_PER_RUN` | `10` | Cap per cycle, across all apps; `0` disables |
| `POLL_PAYMENTS_STATUS` | `1` | Which statuses to announce; blank = all |
| `POLL_PAYMENTS_STUB_STATE_PATH` | `poll_payments_state.stub.json` | Offline rehearsal state |
| `STUB_PAYMENTS_PATH` | `stub_payments.json` | Offline rehearsal fixture |

Shared with the invoice pipeline on purpose: `POLL_INTERVAL`, `POLL_MAX_PAGES`,
`POLL_MAX_BACKOFF`, `POLL_MAX_SEEN`, `DEFAULT_COUNTRY_CODE`,
`WHATSAPP_FREEFORM_FALLBACK` and every `REPORT_*` knob. Both pipelines write to
**one** send report, distinguished by a `kind` column, because it is one audit
trail of everything this sender put in front of a customer.

## Deployment

A second cron entry. `tools/run_poll.sh` takes the subcommand from
`POLL_SUBCOMMAND`, so both pipelines share one wrapper and one log format:

```cron
*/5 * * * * PYTHON=/opt/hc_python/bin/python3.12 \
  /path/to/tools/run_poll.sh --once --timeout 240

*/5 * * * * PYTHON=/opt/hc_python/bin/python3.12 POLL_SUBCOMMAND=poll-payments \
  /path/to/tools/run_poll.sh --once --timeout 240
```

**Never upload a `poll_payments_state.json` from development.** It is keyed by
payment id; a file from another machine marks real payments as already confirmed
and they are never sent again. It is also **meaningless across a resource change**:
the ids in it belong to whichever endpoint last wrote it, so switching this pipeline
between client payments and invoice payments silently re-marks every row. Re-seed
whenever the endpoint changes — see
[If you have just switched this pipeline over](#if-you-have-just-switched-this-pipeline-over).

## Rehearsing offline

Each external dependency is stubbed by **its own flag**, and stubbing one does not
imply stubbing the others:

| Flag | Effect |
|---|---|
| `--payment-stub` | Read payments from `stub_payments.json`, not Daftra |
| `--invoice-stub` | Read invoices from `stub_invoices.json`, not Daftra |
| `--meta-stub` | **Never call Meta.** Payloads are captured and logged instead |

`--meta-stub` is the one that makes a rehearsal safe, and it is a separate flag
deliberately: a stubbed *source* says nothing about the *sender*. A run with only
`--payment-stub` reads fixture data and then really messages whoever the fixture
names.

```bash
# fully offline: no Daftra, no Meta, no credentials needed
python -m sender poll-payments --once --payment-stub --meta-stub --dry-run

# see the exact message a customer would receive
python -m sender send-payment --payment-id 1 --payment-stub --meta-stub --freeform
```

`.env` is resolved from the **working directory**, not from the package location,
so a run started anywhere else cannot pick up this checkout's credentials.

## Failure behaviour

Identical to the invoice pipeline, and inherited rather than reimplemented:

- network/timeout, HTTP 429, 5xx, and Meta codes `4`/`80007`/`130429`/`131056` are
  **retried** with bounded exponential backoff and never abandoned;
- `131047`/`131048`/`131049` are quality signals and are **permanent**;
- a `132000`-series template rejection falls back to the free-form text body (the
  same one `--freeform` prints) when `WHATSAPP_FREEFORM_FALLBACK` is on, and
  otherwise keeps the payment **pending** on the template error. It is never
  abandoned and never consumed;
- the first run per tenant **seeds without sending** — use `--send-existing` to
  confirm the existing history instead;
- `--dry-run` writes nothing, not even the state file.

### The quiet-listing tripwire

A quiet cycle is normal and logs nothing. Two things that are *not* quiet are
reported, because the alternative is the failure this pipeline actually had — a
healthy `new 0`, a zero exit code, and no indication that the endpoint was showing
one row out of a hundred and twenty-five:

- **an empty listing for a tenant that has already handled documents.** Every
  handled document exists in the account, so an empty page means the filter, the
  endpoint, or the account changed — not that the day was quiet;
- **the source's own count is below the number already handled.** A page of
  already-seen documents looks identical whether the account shrank or not, so only
  Daftra's `pagination.total_results` can reveal that the listing has *narrowed*.
  It is read through `getattr`, so a source with no opinion (the offline stub) is
  simply never asked; `None` means "no opinion", never "there is nothing there".

Both warnings name the tenant and say to check the listing rather than the send
path, because that is where the fault was.

## Related

- [Design decision 11](../design.md)
- [Template contract](template-contract.md)
- [Delivery status](delivery-status.md)
- [Architecture](../architecture.md)