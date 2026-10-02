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
    #: Free text the seller wrote about *this line* (Daftra's
    #: ``InvoiceItem.description``) — a size, a colour, a finishing note. It is the
    #: line's own prose and not a second name, which is why the PDF draws it under
    #: the name rather than in the name column. Empty on most rows, and empty is
    #: the ordinary case rather than a missing value: nothing is drawn for it.
    #:
    #: Last in the field order on purpose: callers construct items positionally
    #: (``InvoiceItem("ورق", Decimal("2"), Decimal("50"), Decimal("100"))``), so
    #: inserting it next to :attr:`name` would silently shift the three figures
    #: one place along and turn a quantity into a description.
    description: str = ""


@dataclass(frozen=True)
class Invoice:
    id: str
    number: str
    customer_name: str
#: Every distinct, already-normalized WhatsApp number this document should be
    #: sent to, most-preferred first. Daftra keeps **two** phone fields on a
    #: client (``phone1``/``phone2``) and either may be filled, so a document is
    #: addressed to a *set* of numbers: both when both are usable, the one that
    #: is when only one is. A single-value tuple is the ordinary case — one field
    #: empty, or both fields holding the same number once normalized — and an
    #: empty tuple means there is nowhere to send. Never index ``[0]``
    #: unconditionally: senders iterate, and display code must handle the empty
    #: case. The order is the Daftra field preference, unchanged from when this
    #: was a single value, so the primary recipient of a document that always had
    #: two identical numbers is still the same one.
    customer_phones: tuple[str, ...] = ()
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
    #: Free text about the invoice as a whole (Daftra's ``notes``) — delivery
    #: instructions, a payment term, a thank-you. Distinct from the per-line
    #: :attr:`InvoiceItem.description`: this one belongs to the document, so the
    #: PDF prints it once, above the products. Empty when the seller wrote nothing,
    #: which is the common case.
    description: str = ""


#: Outcome statuses a :class:`SendOutcome` can carry. These mirror the poller's
#: own vocabulary so a row in the report and a line in the poll summary agree.
SENT = "sent"
FAILED = "failed"
ABANDONED = "abandoned"

#: Which document kind a send was about. The report is one audit trail for all
#: three pipelines, so a row has to say which template it went out under.
KIND_INVOICE = "invoice"
KIND_PAYMENT = "payment"
KIND_CUSTOMER = "customer"


@dataclass(frozen=True)
class Payment:
    """One recorded payment against an invoice, as an outbound notification needs it.

    Modelled the same way as :class:`Invoice` — only the facts a customer-facing
    message carries, never a mirror of the ERP's payment record.

    The customer fields are the awkward part and are deliberately *not* on the
    payment itself: a Daftra payment row names no client and carries no phone, so
    ``customer_name`` and ``customer_phones`` are filled in by the adapter from the
    **linked invoice**, which is the only place the client record lives — and which
    means a payment inherits *both* of that client's phone fields, so a payment
    reaches a customer on as many numbers as their invoice does. A payment whose
    invoice no longer resolves therefore arrives with no phone at all and is
    skipped rather than guessed at.
    """

    id: str
    #: Daftra's payment reference code (e.g. ``"000116"``) — the "operation
    #: number" the template body prints. Falls back to ``id`` when absent.
    number: str
    customer_name: str = ""
#: Every distinct, already-normalized WhatsApp number this document should be
    #: sent to, most-preferred first. Daftra keeps **two** phone fields on a
    #: client (``phone1``/``phone2``) and either may be filled, so a document is
    #: addressed to a *set* of numbers: both when both are usable, the one that
    #: is when only one is. A single-value tuple is the ordinary case — one field
    #: empty, or both fields holding the same number once normalized — and an
    #: empty tuple means there is nowhere to send. Never index ``[0]``
    #: unconditionally: senders iterate, and display code must handle the empty
    #: case. The order is the Daftra field preference, unchanged from when this
    #: was a single value, so the primary recipient of a document that always had
    #: two identical numbers is still the same one.
    customer_phones: tuple[str, ...] = ()
    #: Daftra's raw payment status (``"1"`` = completed), kept for logging only.
    status: str = "Unknown"
    currency: str = ""
    amount: Decimal = Decimal("0")
    payment_date: date | None = None
    #: The invoice this payment settles; what makes the customer reachable.
    invoice_id: str | None = None
    #: ``cash``/``bank``/``cheque``/a gateway key. Logging only — not on the
    #: template, so an unknown method never blocks a notification.
    payment_method: str = ""


@dataclass(frozen=True)
class Customer:
    """One client record in Daftra, as an outbound welcome needs it.

    The third pipeline's document. Unlike a payment, a client row carries its own
    name and phone, so reaching this customer costs **one** request — there is no
    join to follow.

    ``customer_name`` and ``customer_phones`` reuse the invoice field names on
    purpose: the poller, the report and the templates all address "a customer"
    the same way regardless of which document kind surfaced it, which is what lets
    one engine drive all three pipelines.
    """

    id: str
    #: The client number an operator reads in the ERP ("000001"). Falls back to
    #: the id when Daftra leaves it blank.
    number: str = ""
    #: ``business_name``, else first + last, else a neutral fallback. Never empty:
    #: the welcome template greets the customer by name, and skipping a nameless
    #: client would mark it seen and silently lose the welcome forever.
    customer_name: str = ""
#: Every distinct, already-normalized WhatsApp number this document should be
    #: sent to, most-preferred first. Daftra keeps **two** phone fields on a
    #: client (``phone1``/``phone2``) and either may be filled, so a document is
    #: addressed to a *set* of numbers: both when both are usable, the one that
    #: is when only one is. A single-value tuple is the ordinary case — one field
    #: empty, or both fields holding the same number once normalized — and an
    #: empty tuple means there is nowhere to send. Never index ``[0]``
    #: unconditionally: senders iterate, and display code must handle the empty
    #: case. The order is the Daftra field preference, unchanged from when this
    #: was a single value, so the primary recipient of a document that always had
    #: two identical numbers is still the same one.
    customer_phones: tuple[str, ...] = ()
    #: When the account was created. Not used to decide who is new — the seen-set
    #: does that — but it is what makes a "new customer" claim checkable after the
    #: fact, so it is carried through to the report.
    created: date | None = None
    email: str = ""
    #: Daftra's raw client type (1/2/3 seen in the wild). Carried for display
    #: only and deliberately never filtered on: the semantics are undocumented,
    #: and guessing them would silently drop real customers.
    type: str = ""
    suspend: str = ""
    is_offline: str = ""


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
    #: The document id: an invoice id, or a payment id for a payment send. The
    #: field keeps its historic name so rows already on disk in the report's
    #: JSONL stay readable; :attr:`kind` is what says which kind this is.
    invoice_id: str
    #: The document number: an invoice number, or a payment reference code.
    invoice_number: str
    customer_name: str
    attempted_at: float
    customer_phone: str | None = None
    currency: str = ""
    #: The document amount: an invoice total, or a payment amount.
    total: Decimal = Decimal("0")
    #: The document date: an invoice issue date, or a payment date.
    issue_date: date | None = None
    status: str = SENT
    #: Which pipeline produced this row, ``KIND_INVOICE`` or ``KIND_PAYMENT``.
    #: Defaults to an invoice so rows written before the payments pipeline
    #: existed still render — and label — correctly.
    kind: str = KIND_INVOICE
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