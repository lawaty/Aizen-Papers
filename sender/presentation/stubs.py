from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Iterable

from sender.domain.models import Invoice, InvoiceItem


def make_stub_invoice(**overrides) -> Invoice:
    defaults = {
        "id": "1",
        "number": "INV-001",
        "customer_name": "Ahmed Hassan",
        "customer_phone": "01027693262",
        "status": "Unpaid",
        "currency": "EGP",
        "subtotal": Decimal("1500.00"),
        "total": Decimal("1500.00"),
        "total_paid": Decimal("0"),
        "balance_due": Decimal("1500.00"),
        "issue_date": date(2026, 9, 1),
        "items": (
            InvoiceItem(
                name="A4 Paper Ream 80gsm",
                quantity=Decimal("1"),
                unit_price=Decimal("1500.00"),
                total=Decimal("1500.00"),
            ),
        ),
        "public_url": "https://demo.daftra.com/invoices/INV-001",
    }
    defaults.update(overrides)
    return Invoice(**defaults)


def default_stub_invoices() -> tuple[Invoice, ...]:
    return (
        make_stub_invoice(),
        make_stub_invoice(
            id="2",
            number="INV-002",
            customer_name="Mona Farouk",
            customer_phone="01234567890",
            status="Partially Paid",
            subtotal=Decimal("3200.50"),
            total=Decimal("3200.50"),
            total_paid=Decimal("1000.00"),
            balance_due=Decimal("2200.50"),
            issue_date=date(2026, 9, 5),
            public_url="https://demo.daftra.com/invoices/INV-002",
        ),
        make_stub_invoice(
            id="3",
            number="INV-003",
            customer_name="Nour Adel",
            customer_phone=None,
            status="Paid",
            subtotal=Decimal("750.25"),
            total=Decimal("750.25"),
            total_paid=Decimal("750.25"),
            balance_due=Decimal("0"),
            issue_date=date(2026, 9, 10),
            public_url="https://demo.daftra.com/invoices/INV-003",
        ),
    )


class StubInvoiceSource:
    def __init__(self, invoices: Iterable[Invoice] = ()) -> None:
        self._seeded: list[Invoice] = []
        self._index: dict[str, Invoice] = {}
        self.requests: list[str] = []
        for invoice in invoices:
            self.seed(invoice)

    def seed(self, invoice: Invoice) -> None:
        self._seeded.append(invoice)
        self._index[str(invoice.id)] = invoice
        self._index[invoice.number] = invoice

    def get_invoice(self, invoice_id: int | str) -> Invoice:
        key = str(invoice_id)
        self.requests.append(key)
        if key not in self._index:
            raise ValueError(f"Stub source has no invoice {invoice_id!r}")
        return self._index[key]

    def get_raw_invoice(self, invoice_id: int | str) -> dict:
        invoice = self.get_invoice(invoice_id)
        return {
            "Invoice": {
                "id": invoice.id,
                "no": invoice.number,
                "payment_status": invoice.status,
                "currency_code": invoice.currency,
                "summary_subtotal": str(invoice.subtotal),
                "summary_total": str(invoice.total),
                "summary_paid": str(invoice.total_paid),
                "summary_unpaid": str(invoice.balance_due),
                "date": invoice.issue_date.strftime("%Y-%m-%d") if invoice.issue_date else None,
            },
            "Client": {"business_name": invoice.customer_name, "phone1": invoice.customer_phone or ""},
            "InvoiceItem": [],
        }

    def list_invoices(self, limit: int = 10) -> list[Invoice]:
        return self._seeded[:limit]