# Aizen Papers — Docs

**Breadcrumb:** Home

---

## The problem

Aizen Paper is an Egyptian interior-decoration business that issues sales invoices
in **Daftra** (its ERP). When a new invoice is issued, the team had to manually call
each customer to share the invoice details — slow, error-prone, and un-scalable.

The alternative, automating over WhatsApp, is constrained by the **Meta WhatsApp
Business Cloud API** rule: a business can only start a conversation with a customer
through a **pre-approved message template**. So the message cannot be a free-form
text; it has to reference a template (here: `aizen_invoice`) and fill its variables
with the invoice's real data.

## The solution

A small, dependency-light Python command-line tool that:

1. **Reads** an invoice from the Daftra REST API (v2),
2. **Normalizes** it into a clean internal `Invoice` model,
3. **Builds** a WhatsApp template payload whose 4 variables (`{{1}}`–`{{4}}`)
   are filled with the customer name, invoice number, issue date, and total.
   The invoice **PDF** is attached when the active template has a **document
   header** — rendered here and uploaded to Meta, because Daftra exposes no
   export and its own file URL is behind a login; a **text-header** template
   sends the details only, with nothing rendered or uploaded
   (builder auto-switches as the template is updated in WhatsApp Manager),
4. **Sends** it through the Meta API to the customer's phone,
5. **Polls** Daftra continuously (`poll`): each cycle lists recent invoices from
   every configured Daftra app (one or two tenants), sends the *new* ones to
   their customers, and remembers what it already handled in `poll_state.json`.

The result is a single repeatable command:

```bash
python -m sender send --invoice-id <id>
```

or, to automate the whole flow:

```bash
python -m sender poll
```

with `preview`, `show`, and `list` commands for safe, non-destructive
inspection, `poll-status`/`poll-reset` to inspect and reset the poll state, and
a persistent `--stub` mode to rehearse the whole flow offline.

## Reading this documentation

The docs are organized as a small tree. Each page has a **breadcrumb** at the top
(showing where you are) and **drill-down** links at the bottom (where to go next),
so you can always backtrack.

```
docs/
├── index.md                     ← you are here (overview: problem & solution)
├── architecture.md              ← layers, runtime flow, external systems
├── design.md                    ← why it's built this way (decisions)
├── guide/
│   ├── getting-started.md       ← install, configure, run
│   ├── template-contract.md     ← the aizen_invoice template & variable mapping
│   ├── invoice-pdf.md           ← how the invoice PDF is attached
│   └── delivery-status.md       ← webhook setup, 200-wamid trap, error codes
└── layers/
    ├── domain.md                ← domain layer (pure, no dependencies)
    ├── application.md           ← application/use-case layer
    ├── infrastructure.md        ← adapters, config, HTTP clients
    └── presentation.md          ← CLI entry point & composition root
```

## Drill down

- [Next: Architecture](architecture.md)
- [Design decisions](design.md)
- [Guides — getting started](guide/getting-started.md)
- [Guides — WhatsApp template contract](guide/template-contract.md)
- [Guides — invoice PDF attachment](guide/invoice-pdf.md)
- [Guides — delivery status & webhook](guide/delivery-status.md)
- [Layers — domain](layers/domain.md)
- [Layers — application](layers/application.md)
- [Layers — infrastructure](layers/infrastructure.md)
- [Layers — presentation](layers/presentation.md)