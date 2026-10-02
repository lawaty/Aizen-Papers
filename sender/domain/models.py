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


#: Outcome statuses a :class:`SendOutcome` can carry. These mirror the poller's
#: own vocabulary so a row in the report and a line in the poll summary agree.
SENT = "sent"
FAILED = "failed"
ABANDONED = "abandoned"


@dataclass(frozen=True)
class SendOutcome:
    """One WhatsApp send attempt and its result, for the HTML report.

    Only *actual* send attempts belong here: never a dry run, never an invoice
    skipped for having no phone, and never a failure that happened before the
    send (a listing or payload-build error never reached Meta). Recording those
    would make the report claim messages went out that did not.

    ``status`` is one of :data:`SENT`, :data:`FAILED` (transient, will be
    retried) or :data:`ABANDONED` (permanent, given up). The snapshot of invoice
    details is deliberate: the state file only keeps ids, so by the time an
    operator reads the report the invoice may since have been edited or deleted.
    """

    app: str
    invoice_id: str
    invoice_number: str
    customer_name: str
    attempted_at: float
    customer_phone: str | None = None
    currency: str = ""
    total: Decimal = Decimal("0")
    issue_date: date | None = None
    status: str = SENT
    #: Failure reason, or the template error that triggered a free-form fallback.
    error: str | None = None
    #: True when this attempt was delivered by the free-form fallback instead of
    #: the template (which means the template itself rejected the send).
    fallback: bool = False
    #: Meta's message id, when the send returned one.
    wamid: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the message was accepted by Meta."""
        return self.status == SENT