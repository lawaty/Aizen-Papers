from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class InvoiceItem:
    name: str
    quantity: Decimal = Decimal("1")
    unit_price: Decimal = Decimal("0")
    total: Decimal = Decimal("0")


@dataclass(frozen=True)
class Invoice:
    id: str
    number: str
    customer_name: str
    customer_phone: str | None = None
    status: str = "Unknown"
    currency: str = ""
    subtotal: Decimal = Decimal("0")
    total: Decimal = Decimal("0")
    total_paid: Decimal = Decimal("0")
    balance_due: Decimal = Decimal("0")
    issue_date: date | None = None
    items: tuple[InvoiceItem, ...] = ()
    public_url: str | None = None
    #: The invoice PDF itself, when the source exposes one (``invoice_pdf_url``).
    #: ``public_url`` stays the human-facing page (Daftra's HTML preview), which is
    #: a *different* resource: the template header document must point at the PDF.
    pdf_url: str | None = None