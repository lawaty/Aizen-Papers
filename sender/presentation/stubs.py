from __future__ import annotations

import json
import logging
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from sender.domain.models import Invoice, InvoiceItem
from sender.infrastructure.util import write_json_atomic

log = logging.getLogger(__name__)


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
        "public_url": "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
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
            public_url="https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
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
            public_url="https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
        ),
    )


def _invoice_to_dict(invoice: Invoice) -> dict:
    return {
        "id": invoice.id,
        "number": invoice.number,
        "customer_name": invoice.customer_name,
        "customer_phone": invoice.customer_phone,
        "status": invoice.status,
        "currency": invoice.currency,
        "subtotal": str(invoice.subtotal),
        "total": str(invoice.total),
        "total_paid": str(invoice.total_paid),
        "balance_due": str(invoice.balance_due),
        "issue_date": invoice.issue_date.isoformat() if invoice.issue_date else None,
        "public_url": invoice.public_url,
        "items": [
            {
                "name": item.name,
                "quantity": str(item.quantity),
                "unit_price": str(item.unit_price),
                "total": str(item.total),
            }
            for item in invoice.items
        ],
    }


def _invoice_from_dict(data: dict) -> Invoice:
    items = tuple(
        InvoiceItem(
            name=str(item.get("name", "Item")),
            quantity=Decimal(str(item.get("quantity", "1"))),
            unit_price=Decimal(str(item.get("unit_price", "0"))),
            total=Decimal(str(item.get("total", "0"))),
        )
        for item in data.get("items", []) or []
    )
    issue = data.get("issue_date")
    return Invoice(
        id=str(data.get("id", "")),
        number=str(data.get("number", "")),
        customer_name=str(data.get("customer_name", "")),
        customer_phone=data.get("customer_phone"),
        status=str(data.get("status", "Unknown")),
        currency=str(data.get("currency", "")),
        subtotal=Decimal(str(data.get("subtotal", "0"))),
        total=Decimal(str(data.get("total", "0"))),
        total_paid=Decimal(str(data.get("total_paid", "0"))),
        balance_due=Decimal(str(data.get("balance_due", "0"))),
        issue_date=date.fromisoformat(issue) if issue else None,
        items=items,
        public_url=data.get("public_url"),
    )


class StubInvoiceSource:
    def __init__(self, invoices: Iterable[Invoice] = (), path: str | None = None) -> None:
        self._seeded: list[Invoice] = []
        self._index: dict[str, Invoice] = {}
        self.requests: list[str] = []
        self._next_id = 1
        self._path = Path(path) if path else None
        if self._path is not None and self._path.exists():
            self._load_from_path()
        else:
            seed = list(invoices) if invoices else (list(default_stub_invoices()) if self._path is not None else [])
            for invoice in seed:
                self.seed(invoice)
            if self._path is not None:
                self._save_to_path()
        numeric = [int(invoice.id) for invoice in self._seeded if str(invoice.id).isdigit()]
        self._next_id = max(numeric, default=0) + 1

    def seed(self, invoice: Invoice) -> None:
        self._seeded.append(invoice)
        self._index[str(invoice.id)] = invoice
        self._index[invoice.number] = invoice

    def add_new_invoice(self, **overrides) -> Invoice:
        """Simulate a new invoice appearing in Daftra between poll cycles."""
        overrides.setdefault("id", str(self._next_id))
        overrides.setdefault("number", f"INV-{self._next_id:03d}")
        invoice = make_stub_invoice(**overrides)
        self.seed(invoice)
        if str(invoice.id).isdigit():
            self._next_id = max(self._next_id, int(invoice.id) + 1)
        if self._path is not None:
            self._save_to_path()
        return invoice

    def _load_from_path(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            log.warning("stub invoice fixture at %s is unreadable; starting empty", self._path)
            data = []
        for row in data if isinstance(data, list) else []:
            if isinstance(row, dict):
                self.seed(_invoice_from_dict(row))

    def _save_to_path(self) -> None:
        try:
            write_json_atomic(self._path, [_invoice_to_dict(invoice) for invoice in self._seeded])
        except OSError:
            log.warning("could not write the stub invoice fixture at %s", self._path)

    def get_invoice(self, invoice_id: int | str) -> Invoice:
        key = str(invoice_id)
        self.requests.append(key)
        if key not in self._index:
            raise ValueError(f"Stub source has no invoice {invoice_id!r}")
        return self._index[key]

    def get_raw_invoice(self, invoice_id: int | str) -> dict:
        invoice = self.get_invoice(invoice_id)
        return {
            "result": "successful",
            "code": 200,
            "data": {
                "Invoice": {
                    "id": invoice.id,
                    "no": invoice.number,
                    "draft": "1" if invoice.status == "Draft" else "0",
                    "payment_status": invoice.status,
                    "currency_code": invoice.currency,
                    "summary_subtotal": str(invoice.subtotal),
                    "summary_total": str(invoice.total),
                    "summary_paid": str(invoice.total_paid),
                    "summary_unpaid": str(invoice.balance_due),
                    "date": invoice.issue_date.strftime("%Y-%m-%d") if invoice.issue_date else None,
                    "invoice_html_url": invoice.public_url,
                    "Client": {
                        "business_name": invoice.customer_name,
                        "phone1": invoice.customer_phone or "",
                    },
                    "InvoiceItem": [
                        {
                            "item": item.name,
                            "quantity": str(item.quantity),
                            "unit_price": str(item.unit_price),
                            "subtotal": str(item.total),
                        }
                        for item in invoice.items
                    ],
                }
            },
        }

    def list_invoices(self, limit: int = 10, page: int = 1) -> list[Invoice]:
        # Newest first, mirroring the Daftra list endpoint the poller pages over.
        start = (page - 1) * limit
        return list(reversed(self._seeded))[start : start + limit]