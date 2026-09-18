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
   are filled with the customer name, invoice number, issue date, and total,
4. **Sends** it through the Meta API to the customer's phone.

The result is a single repeatable command:

```bash
python -m sender send --invoice-id <id>
```

with `preview`, `show`, and `list` commands for safe, non-destructive inspection.

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
│   └── template-contract.md     ← the aizen_invoice template & variable mapping
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
- [Layers — domain](layers/domain.md)
- [Layers — application](layers/application.md)
- [Layers — infrastructure](layers/infrastructure.md)
- [Layers — presentation](layers/presentation.md)