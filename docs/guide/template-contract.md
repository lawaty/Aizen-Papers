# WhatsApp template contract — `aizen_invoice`

**Breadcrumb:** [Home](../index.md) / [Guide](.) / [Template contract](template-contract.md)

---

This page is the ground truth for how our payload must look so Meta accepts the
business-initiated message. It mirrors `TEMPLATE_BODY` inside
`sender/domain/templates.py` — **keep the two in sync**.

## The approved body

Language: `en_EG` (English - Egypt). Body text (as registered in Meta's template manager):

```
مَرْحَبًا {{1}}، 👋

نُحيطُكم عِلمًا بأنَّه تمَّ إصدار فاتورة جديدة من Aizen Paper.

📄 رقم الفاتورة: {{2}}
📅 تاريخ الإصدار: {{3}}
💰 إجمالي الفاتورة: {{4}} ج.م

شُكرًا لثقتكم الغالية، ونَسعد دائمًا باستمرار تعاونكم معنا. 🤝

Aizen Paper
✨ ثِقتكم مَحلُّ تقديرنا دائمًا.
```

Exactly **4 placeholders**, in this order.

## Variable ↔ parameter mapping

| Placeholder | Source (`Invoice` field) | Formatting | Example |
|---|---|---|---|
| `{{1}}` | `customer_name` | whitespace-collapsed, ≤512 chars, `-` if missing | `Ahmed Hassan` |
| `{{2}}` | `number` | as-is, sanitized | `INV-001` |
| `{{3}}` | `issue_date` | `DD/MM/YYYY` (or `N/A`) | `01/09/2026` |
| `{{4}}` | `total` | `f"{total:,.2f}"`, **no currency** | `1,500.00` |

Note: `{{4}}` must **not** include the currency — `ج.م` is hardcoded in the body.
Including it produces `1,500.00 ج.م ج.م`.

## What the send payload looks like

```json
{
  "messaging_product": "whatsapp",
  "recipient_type": "individual",
  "to": "201027693262",
  "type": "template",
  "template": {
    "name": "aizen_invoice",
    "language": { "code": "en" },
    "components": [
      { "type": "body", "parameters": [
        { "type": "text", "text": "Ahmed Hassan" },
        { "type": "text", "text": "INV-001" },
        { "type": "text", "text": "01/09/2026" },
        { "type": "text", "text": "1,500.00" }
      ]}
    ]
  }
}
```

`python -m sender preview --invoice-id <id>` prints exactly this JSON.

## Rules that cause Meta to reject a send

- **Placeholder count mismatch** — more or fewer than 4 parameters.
- **Wrong order** — params must match `{{1}}`→`{{4}}` left to right.
- **Wrong language code** — must be `en_EG` (what the template was approved as).
- **Non-E.164 recipient** — `to` must be international (`2010…`, not `010…`).
- **Unsanitized text** — parameters must not contain newlines/tabs/4+ spaces and
  must be ≤512 chars.
- **Unverified recipient (test mode)** — the number must be in the app's verified
  test numbers.

These surface as `132000`-series error codes from the Cloud API. The builder's
sanitization and normalization exist to prevent as many as possible *before* the
HTTP call.

## Related

- [Getting started](getting-started.md)
- [Design decision #3 (why a builder)](../design.md)
- [Domain layer — templates](../layers/domain.md)