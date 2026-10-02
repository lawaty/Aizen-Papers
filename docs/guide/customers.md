# Customers pipeline

**Breadcrumb:** [Home](../index.md) / [Guide](.) / [Customers](customers.md)

---

The third notification pipeline: it watches Daftra for newly created client
records and welcomes each new one under the `aizen_new_customer` WhatsApp
template.

It is the same machinery as the invoice and payments pipelines — same engine,
same state store, same retry/abandon rules, same send cap, same HTML/JSONL report,
same per-dependency stub flags — as its own command with its own state file, lock
and budget. The reasoning is in [design decision 12](../design.md); this page is
how to run it.

> **Read the [MARKETING-category section](#marketing-category-and-before-you-enable-it)
> before enabling this in production.** `aizen_new_customer` is classified
> MARKETING by Meta, unlike the UTILITY invoice and payment templates, and that
> carries requirements the code cannot satisfy for you.

## Commands

| Command | What it does |
|---|---|
| `python -m sender poll-customers` | Watch for new customers and welcome them (the production path) |
| `python -m sender customers` | List recent customers |
| `python -m sender show-customer --customer-id N` | Print a normalized customer (`--raw` for the Daftra JSON) |
| `python -m sender send-customer --customer-id N` | Send one welcome by hand |
| `python -m sender poll-status --customers` | What has been seen, retried or abandoned |
| `python -m sender poll-reset --customers --app A --customer-id N --yes` | Re-drive one customer |
| `python -m sender stub-customer-add` | Add a customer to the offline rehearsal fixture |

`poll-customers` takes `--once`, `--interval`, `--limit`, `--max-cycles`,
`--max-sends`, `--timeout`, `--dry-run`, `--send-existing` and the stub flags. It
takes **no** `--builder` and **no** `--attachment`: the welcome template has one
approved shape with no header document, so there is nothing to choose and nothing
to attach.

## The template contract

`aizen_new_customer`, language **`ar_EG`**, category **MARKETING**. It has a body
and a footer and **no header**, so:

- there is no document parameter — nothing is rendered and nothing is uploaded;
- the payload has exactly one legal shape, so this path does not consult
  `TemplateRegistry` (that exists only to choose between the two invoice builders);
- the language must be `ar_EG`. Anything else is a `132001` on every send.

**One** body parameter: the customer's name.

```
*أهلًا بكم في Aizen Paper 👋*

مَرْحَبًا {{1}}،

يسعدنا انضمامكم إلى عملاء Aizen Paper، ونرحب ببداية تعاون مثمر ومستمر معكم. 🤝

تم تسجيل حسابكم بنجاح في نظامنا، ونتطلع دائمًا لتقديم أفضل خدمة لكم.
```

Footer: `Aizen Paper`. A mismatch here fails nowhere in this repo — it surfaces
only as a `132000`-series rejection from Meta, at send time, for a real customer.
That is why [tests/test_customer_payload_contract.py](../../tests/test_customer_payload_contract.py)
pins all of it.

## What a "new customer" is

A client record this pipeline has not seen before. That is the whole rule.

It is deliberately **not** a `created`-date filter. Daftra accepts `created_from`,
but its boundary is date-granular and inclusive (the time component is truncated),
so a precise watermark cannot be expressed, and Daftra **silently ignores** every
other spelling — answering `200` with the unfiltered set, so a typo would look like
it worked. The accounts hold one to six clients each; there is nothing to save. The
seen-set is exact where a watermark would be approximate.

The first run therefore **seeds without sending**: every existing customer is
recorded as handled and none is messaged. Use `--send-existing` if you actually
want a one-off welcome to the current customer base.

### Ordering is requested explicitly, and it matters

Daftra's `/clients.json` does **not** come back newest-first — its default order is
stable but arbitrary (a live account returned ids `[5,1,2,6,4,3]`). The poller
walks pages forward and stops at the first record it has already seen, which is
only correct if newer records come first, so the adapter always sends
`sort=created&direction=desc`.

If those parameters are ever lost, the pipeline stops early on a saturated page and
silently never announces a new customer further back — no error, no warning. A
test pins the exact request. If welcomes mysteriously stop arriving, check that
first.

## How a customer is resolved

One Daftra request per new customer, versus two for a payment:

- **Name:** `business_name`, else `first_name` + `last_name`, else `"Customer"`.
  Live accounts use all three shapes, so no single field is reliable. The fallback
  is sent rather than skipped — a reachable customer with no name on file is still
  a real customer, and skipping would mark them handled and lose the welcome
  permanently. (An all-null row reads `مَرْحَبًا Customer،` — awkward but delivered.)
- **Phone:** `phone2`, then `phone1`, then `mobile`/`phone`. Already-E.164 numbers
  pass through; local `010…` gets `DEFAULT_COUNTRY_CODE` prepended; empty or
  unparseable becomes "no phone", and the poller skips the customer and records it
  as handled rather than retrying forever.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `WHATSAPP_CUSTOMER_TEMPLATE_NAME` | `aizen_new_customer` | The Meta template |
| `WHATSAPP_CUSTOMER_TEMPLATE_LANG` | `ar_EG` | Must match the approved language |
| `POLL_CUSTOMERS_STATE_PATH` | `poll_customers_state.json` | Gitignored, machine-specific |
| `POLL_CUSTOMERS_LIMIT` | `10` | Listing page size |
| `POLL_CUSTOMERS_MAX_SENDS_PER_RUN` | `10` | Cap per cycle, across all apps; `0` disables |
| `WHATSAPP_CUSTOMER_FREEFORM_FALLBACK` | `false` | See below |
| `POLL_CUSTOMERS_STUB_STATE_PATH` | `poll_customers_state.stub.json` | Offline rehearsal state |
| `STUB_CUSTOMERS_PATH` | `stub_customers.json` | Offline rehearsal fixture |

Shared with the other two pipelines on purpose: `POLL_INTERVAL`, `POLL_MAX_PAGES`,
`POLL_MAX_BACKOFF`, `POLL_MAX_SEEN`, `DEFAULT_COUNTRY_CODE`,
`WHATSAPP_FREEFORM_FALLBACK` and every `REPORT_*` knob. All three pipelines write
to **one** send report, distinguished by a `kind` column, because it is one audit
trail of everything this sender put in front of a customer.

### Why the free-form fallback is off here

`WHATSAPP_FREEFORM_FALLBACK` is `true` for invoices and payments; this pipeline has
its own knob, defaulting to `false`.

A welcome goes to a brand-new number, which is by definition **outside** WhatsApp's
24-hour customer-service window — the only place free-form text is deliverable. A
fallback here can never rescue a send; it can only spend a doomed second request
and bury the real error. With it off, a rejected template leaves the customer
**pending**: fix the template and they flow on a later cycle. Nothing is consumed.

## Deployment

A third cron entry. `tools/run_poll.sh` takes the subcommand from
`POLL_SUBCOMMAND`, so all three pipelines share one wrapper and one log format:

```cron
*/5 * * * * PYTHON=/opt/hc_python/bin/python3.12 \
  /path/to/tools/run_poll.sh --once --timeout 240

*/5 * * * * PYTHON=/opt/hc_python/bin/python3.12 POLL_SUBCOMMAND=poll-payments \
  /path/to/tools/run_poll.sh --once --timeout 240

*/5 * * * * PYTHON=/opt/hc_python/bin/python3.12 POLL_SUBCOMMAND=poll-customers \
  /path/to/tools/run_poll.sh --once --timeout 240
```

**Never upload a `poll_customers_state.json` from development.** It is keyed by
client id; a file from another machine marks real customers as already welcomed and
they are never sent to again.

## Rehearsing offline

Each external dependency is stubbed by **its own flag**, and stubbing one does not
imply stubbing the others:

| Flag | Effect |
|---|---|
| `--customer-stub` | Read customers from `stub_customers.json`, not Daftra |
| `--invoice-stub` | Read invoices from `stub_invoices.json`, not Daftra |
| `--payment-stub` | Read payments from `stub_payments.json`, not Daftra |
| `--meta-stub` | **Never call Meta.** Payloads are captured and logged instead |

`--meta-stub` is what makes a rehearsal safe, and it is a separate flag
deliberately: a stubbed *source* says nothing about the *sender*. A run with only
`--customer-stub` reads fixture data and then really messages whoever the fixture
names.

```bash
# fully offline: no Daftra, no Meta, no credentials needed
python -m sender poll-customers --once --customer-stub --meta-stub --send-existing

# simulate a signup, then watch exactly one welcome go out on the next cycle
python -m sender stub-customer-add
python -m sender poll-customers --once --customer-stub --meta-stub

# see the exact message a customer would receive
python -m sender send-customer --customer-id 1 --customer-stub --meta-stub --freeform
```

`.env` is resolved from the **working directory**, not from the package location,
so a run started anywhere else cannot pick up this checkout's credentials.

## Failure behaviour

Inherited from the invoice pipeline rather than reimplemented:

- network/timeout, HTTP 429, 5xx, and Meta codes `4`/`80007`/`130429`/`131056` are
  **retried** with bounded exponential backoff and never abandoned;
- `131047`/`131048`/`131049` are quality signals and are **permanent**;
- a `132000`-series template rejection keeps the customer **pending** (free-form
  fallback is off here — see above);
- the first run per tenant **seeds without sending**;
- `--dry-run` writes nothing, not even the state file.

## MARKETING category, and before you enable it

Meta classifies `aizen_new_customer` as **MARKETING**; the invoice and payment
templates are UTILITY. That is not a labelling detail — it changes what Meta
requires of you:

1. **Opt-in evidence.** Marketing messages require demonstrable consent. Before
   enabling this, someone must be able to say *how* a new customer opted in (signup
   checkbox, terms acceptance, …). If that answer does not exist, do not enable it:
   no amount of code correctness makes it compliant, and Meta can penalise or
   suspend the sending number for it.
2. **Numbers that are not on WhatsApp yet.** A brand-new number may not be
   registered. Those are rejected permanently, abandoned once, and left visible in
   `poll-status --customers`. The engine handles this correctly; expect to see those
   rows.
3. **Message quality and rating apply.** Marketing templates are scored. A low
   rating degrades delivery for the number across *all* templates, including the
   transactional invoice and payment ones.
4. **First-run seeding means existing customers are not welcomed.** By design. A
   one-off welcome to current customers is `--send-existing` on the first run.

## Related

- [Design decision 12](../design.md)
- [Template contract](template-contract.md)
- [Delivery status](delivery-status.md)
- [Payments pipeline](payments.md)
- [Architecture](../architecture.md)
