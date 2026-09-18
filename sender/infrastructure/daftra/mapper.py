from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sender.domain.models import Invoice, InvoiceItem
from sender.domain.phones import normalize_phone

STATUS_LABELS = {
    -1: "Draft",
    0: "Unpaid",
    1: "Partially Paid",
    2: "Paid",
    3: "Refunded",
    4: "Overpaid",
}

NUMBER_KEYS = ("no", "number", "code")
TOTAL_KEYS = ("summary_total", "total", "total_amount")
SUBTOTAL_KEYS = ("summary_subtotal", "subtotal", "sub_total")
PAID_KEYS = ("summary_paid", "total_paid", "paid_amount")
BALANCE_KEYS = ("summary_unpaid", "balance_due", "remaining", "due_amount")
ISSUE_KEYS = ("date", "issue_date", "invoice_date")
CURRENCY_KEYS = ("currency_code", "currency", "default_currency_code")
URL_KEYS = ("invoice_html_url", "invoice_pdf_url", "public_url", "permalink")


class DaftraInvoiceMapper:
    def to_invoice(self, raw: dict) -> Invoice:
        node = self._data_node(raw)
        if not isinstance(node, dict):
            raise ValueError(f"Unexpected Daftra payload shape: {type(raw).__name__}")
        invoice = node.get("Invoice") if isinstance(node.get("Invoice"), dict) else node
        if "Invoice" in node and not isinstance(node.get("Invoice"), dict):
            raise ValueError(
                f"Malformed Daftra response: 'Invoice' is not an object: {list(node)[:6]}"
            )
        if not invoice.get("id") and not invoice.get("no"):
            raise ValueError(f"Malformed Daftra response: missing invoice data in {list(node)[:6]}")
        client = node.get("Client") if isinstance(node.get("Client"), dict) else {}
        items_raw = node.get("InvoiceItem") or []
        if isinstance(items_raw, dict):
            items_raw = [items_raw]
        if not isinstance(items_raw, list):
            items_raw = []
        items = tuple(self._to_item(item) for item in items_raw if isinstance(item, dict))

        total = self._money(self._first(invoice, TOTAL_KEYS))
        paid = self._money(self._first(invoice, PAID_KEYS))
        balance = self._first(invoice, BALANCE_KEYS)
        if balance is None:
            balance = total - paid

        return Invoice(
            id=str(invoice.get("id", "")),
            number=self._number(invoice),
            customer_name=self._customer_name(invoice, client),
            customer_phone=self._phone(self._customer_phone(invoice, client)),
            status=self._status(invoice),
            currency=str(self._first(invoice, CURRENCY_KEYS) or ""),
            subtotal=self._money(self._first(invoice, SUBTOTAL_KEYS)),
            total=total,
            total_paid=paid,
            balance_due=self._money(balance),
            issue_date=self._date(self._first(invoice, ISSUE_KEYS)),
            items=items,
            public_url=self._first(invoice, URL_KEYS),
        )

    def to_invoices(self, raw: dict) -> list[Invoice]:
        data = raw.get("data") if isinstance(raw, dict) else None
        rows = data if isinstance(data, list) else []
        return [self.to_invoice(row) for row in rows if isinstance(row, dict)]

    def _data_node(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            return raw
        data = raw.get("data")
        return data if isinstance(data, dict) else raw

    def _to_item(self, item: dict) -> InvoiceItem:
        return InvoiceItem(
            name=str(self._first(item, ("item", "name", "description"), default="Item")),
            quantity=self._money(self._first(item, ("quantity", "qty"), default=1)),
            unit_price=self._money(self._first(item, ("unit_price", "price"))),
            total=self._money(self._first(item, ("subtotal", "total", "line_total"))),
        )

    def _number(self, invoice: dict) -> str:
        number = self._first(invoice, NUMBER_KEYS)
        if number is not None:
            return str(number)
        return str(invoice.get("id") or "")

    def _customer_name(self, invoice: dict, client: dict) -> str:
        for key in ("business_name", "client_business_name"):
            value = self._first(client, (key,)) or self._first(invoice, (key,))
            if value:
                return str(value)
        first = self._first(invoice, ("client_first_name",)) or self._first(client, ("first_name",))
        last = self._first(invoice, ("client_last_name",)) or self._first(client, ("last_name",))
        joined = " ".join(str(part) for part in (first, last) if part).strip()
        return joined or "Customer"

    def _customer_phone(self, invoice: dict, client: dict) -> str | None:
        for key in ("phone2", "phone1", "mobile", "phone"):
            value = self._first(client, (key,)) or self._first(invoice, (f"client_{key}",))
            if value:
                return str(value)
        return None

    def _status(self, invoice: dict) -> str:
        draft = self._first(invoice, ("draft",), default=False)
        if draft in (1, "1", True, "true", "True"):
            return "Draft"
        payment_status = self._first(invoice, ("payment_status", "status"))
        if isinstance(payment_status, bool):
            payment_status = int(payment_status)
        if isinstance(payment_status, int) and payment_status in STATUS_LABELS:
            return STATUS_LABELS[payment_status]
        if isinstance(payment_status, str) and payment_status.isdigit():
            return STATUS_LABELS.get(int(payment_status), "Unknown")
        text = str(payment_status or "").replace("_", " ").title()
        return text or "Unknown"

    def _first(self, node: dict, keys: tuple[str, ...], default=None):
        for key in keys:
            value = self._pick(node, key)
            if value is None or value == "" or value == []:
                continue
            return value
        return default

    def _pick(self, node: dict, key: str):
        current = node
        for part in key.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    @staticmethod
    def _money(value) -> Decimal:
        if isinstance(value, bool):
            return Decimal("1" if value else "0")
        if value is None or value == "":
            return Decimal("0")
        if isinstance(value, Decimal):
            return value if value.is_finite() else Decimal("0")
        text = str(value).strip()
        if "," in text and "." in text:
            if text.rindex(",") > text.rindex("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            if re.fullmatch(r"\d{1,3},\d{3}", text):
                text = text.replace(",", "")
            else:
                text = text.replace(",", ".")
        try:
            result = Decimal(text)
        except (InvalidOperation, ValueError):
            return Decimal("0")
        return result if result.is_finite() else Decimal("0")

    @staticmethod
    def _date(value) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if value is None or value == "":
            return None
        text = str(value).strip()[:10]
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _phone(value) -> str | None:
        if value is None or value == "":
            return None
        try:
            return normalize_phone(value)
        except ValueError:
            return None