from __future__ import annotations

import json
import logging
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from sender.domain.models import Customer, Invoice, InvoiceItem, Payment
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


def make_stub_payment(**overrides) -> Payment:
    """A payment fixture, shaped like what :class:`StubPaymentSource` serves.

    The customer phone is stored *un-normalized* (``010…``) on purpose: it is what
    the Daftra mapper actually produces before the domain's normalizer runs, so
    exercising a fixture that is already E.164 would quietly stop testing the rule
    that matters.
    """
    defaults = {
        "id": "1",
        "number": "000001",
        "customer_name": "Ahmed Hassan",
        "customer_phone": "01027693262",
        "status": "1",
        "currency": "EGP",
        "amount": Decimal("1500.00"),
        "payment_date": date(2026, 9, 1),
        "invoice_id": "1",
        "payment_method": "cash",
    }
    defaults.update(overrides)
    return Payment(**defaults)


def default_stub_payments() -> tuple[Payment, ...]:
    """Three payments that between them hit the branches a rehearsal must show.

    The third carries **no phone**, which is the branch that most often surprises
    an operator: a payment nobody can be reached about is skipped and retired
    rather than retried forever, and that only becomes obvious in a rehearsal.
    """
    return (
        make_stub_payment(),
        make_stub_payment(
            id="2",
            number="000002",
            customer_name="Mona Farouk",
            customer_phone="01234567890",
            amount=Decimal("3200.50"),
            payment_date=date(2026, 9, 5),
            invoice_id="2",
            payment_method="bank",
        ),
        make_stub_payment(
            id="3",
            number="000003",
            customer_name="Nour Adel",
            customer_phone=None,
            amount=Decimal("750.25"),
            payment_date=date(2026, 9, 10),
            invoice_id="3",
        ),
    )


def _payment_to_dict(payment: Payment) -> dict:
    return {
        "id": payment.id,
        "number": payment.number,
        "customer_name": payment.customer_name,
        "customer_phone": payment.customer_phone,
        "status": payment.status,
        "currency": payment.currency,
        "amount": str(payment.amount),
        "payment_date": payment.payment_date.isoformat() if payment.payment_date else None,
        "invoice_id": payment.invoice_id,
        "payment_method": payment.payment_method,
    }


def _payment_from_dict(data: dict) -> Payment:
    paid = data.get("payment_date")
    return Payment(
        id=str(data.get("id", "")),
        number=str(data.get("number", "")),
        customer_name=str(data.get("customer_name", "")),
        customer_phone=data.get("customer_phone"),
        status=str(data.get("status", "Unknown")),
        currency=str(data.get("currency", "")),
        amount=Decimal(str(data.get("amount", "0"))),
        payment_date=date.fromisoformat(paid) if paid else None,
        invoice_id=data.get("invoice_id"),
        payment_method=str(data.get("payment_method", "")),
    )


class StubMessageSender:
    """Offline stand-in for the WhatsApp Cloud API client.

    Separate from the source stubs on purpose. Stubbing *where a document comes
    from* says nothing about *where a message goes*: a rehearsal with a stubbed
    Daftra and a real sender still messages real customers, which is the trap this
    class exists to let someone step around explicitly. Only ``--meta-stub``
    swaps in this one, so "did I rehearse offline?" has an unambiguous answer.

    Every payload is accepted and kept, so a rehearsal can show exactly what
    would have gone out, and the rendered message is logged at INFO — a stub run
    that only printed a count would not tell you whether the Arabic body and its
    parameters were right, which is the thing a rehearsal is for.
    """

    #: Mirrors ``InvoiceAttachmentProvider.mode``, so the "what is stubbed"
    #: vocabulary reads the same wherever it appears.
    mode = "stub"

    def __init__(self, response: dict | None = None) -> None:
        self.response = response or {"messages": [{"id": "wamid.STUB"}]}
        self.payloads: list[dict] = []

    @property
    def calls(self) -> list[dict]:
        return self.payloads

    def send(self, payload: dict) -> dict:
        self.payloads.append(payload)
        log.info("stub meta: would send to %s: %s", payload.get("to"), payload.get("type"))
        body = _stub_message_body(payload)
        if body:
            log.info("stub meta: message body:\n%s", body)
        return self.response


def _stub_message_body(payload: dict) -> str:
    """The human-readable message inside a payload, for the rehearsal log.

    Handles the three shapes this sender can be handed — a template message (whose
    parameters are assembled back into ``{{n}}`` placeholders, which is the only
    way to read them), a free-form text message, and a free-form document message
    (which has no body at all, so it reports its document instead).
    """
    kind = payload.get("type")
    if kind == "text":
        return str((payload.get("text") or {}).get("body") or "")
    if kind == "document":
        document = payload.get("document") or {}
        target = document.get("id") or document.get("link") or "?"
        return f"[document: {document.get('filename', '?')} -> {target}]"
    if kind != "template":
        return ""
    template = payload.get("template") or {}
    body = ""
    for component in template.get("components") or []:
        if str(component.get("type") or "").lower() != "body":
            continue
        for index, parameter in enumerate(component.get("parameters") or [], start=1):
            text = parameter.get("text")
            if text is None:
                text = str(parameter.get("document") or "")
            body += str(text).replace(f"{{{{{index}}}}}", text)
    return body


class StubPaymentSource:
    """Offline stand-in for :class:`DaftraClient`'s payment half.

    Structurally the twin of :class:`StubInvoiceSource`, with one deliberate
    difference: a stub payment arrives already carrying its customer, so
    ``get_payment`` is a single lookup where the Daftra adapter needs two. That is
    the stub being *nicer* than reality on purpose — a rehearsal should not be
    made to care about Daftra's join order.
    """

    def __init__(
        self,
        payments: Iterable[Payment] = (),
        path: str | None = None,
        status: str | None = "1",
    ) -> None:
        self._seeded: list[Payment] = []
        self._index: dict[str, Payment] = {}
        self.requests: list[str] = []
        self._next_id = 1
        self._path = Path(path) if path else None
        # Mirrors the Daftra client's server-side status narrowing, default and
        # all, so a rehearsal pages over the same rows production would. ``None``
        # disables it — the same escape hatch the real filter has.
        self._status = status
        if self._path is not None and self._path.exists():
            self._load_from_path()
        else:
            seed = list(payments) if payments else (
                list(default_stub_payments()) if self._path is not None else []
            )
            for payment in seed:
                self.seed(payment)
            if self._path is not None:
                self._save_to_path()
        numeric = [int(payment.id) for payment in self._seeded if str(payment.id).isdigit()]
        self._next_id = max(numeric, default=0) + 1

    def seed(self, payment: Payment) -> None:
        self._seeded.append(payment)
        self._index[str(payment.id)] = payment
        self._index[payment.number] = payment

    def add_new_payment(self, **overrides) -> Payment:
        """Simulate a new payment appearing in Daftra between poll cycles."""
        overrides.setdefault("id", str(self._next_id))
        # Zero-padded to six digits to match the shape Daftra gives a payment
        # reference code, so a rehearsal does not normalize formatting the
        # production path will see.
        overrides.setdefault("number", f"{self._next_id:06d}")
        payment = make_stub_payment(**overrides)
        self.seed(payment)
        if str(payment.id).isdigit():
            self._next_id = max(self._next_id, int(payment.id) + 1)
        if self._path is not None:
            self._save_to_path()
        return payment

    def _load_from_path(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            log.warning("stub payment fixture at %s is unreadable; starting empty", self._path)
            data = []
        for row in data if isinstance(data, list) else []:
            if isinstance(row, dict):
                self.seed(_payment_from_dict(row))

    def _save_to_path(self) -> None:
        try:
            write_json_atomic(self._path, [_payment_to_dict(p) for p in self._seeded])
        except OSError:
            log.warning("could not write the stub payment fixture at %s", self._path)

    def get_payment(self, payment_id: int | str) -> Payment:
        key = str(payment_id)
        self.requests.append(key)
        if key not in self._index:
            raise ValueError(f"Stub source has no payment {payment_id!r}")
        return self._index[key]

    def get_raw_payment(self, payment_id: int | str) -> dict:
        payment = self.get_payment(payment_id)
        return {
            "result": "successful",
            "code": 200,
            "data": {
                "InvoicePayment": {
                    "id": payment.id,
                    "invoice_id": payment.invoice_id,
                    "payment_method": payment.payment_method,
                    "amount": str(payment.amount),
                    "date": payment.payment_date.strftime("%Y-%m-%d 00:00:00")
                    if payment.payment_date
                    else None,
                    "status": payment.status,
                    "currency_code": payment.currency,
                    # The live row carries an empty payer; the customer only
                    # appears once the linked invoice is read. Reproduced here so
                    # a rehearsal of the raw payload looks like production.
                    "client_id": None,
                    "first_name": "",
                    "last_name": "",
                    "phone1": "",
                    "phone2": "",
                    "code": payment.number,
                }
            },
        }

    def list_payments(self, limit: int = 10, page: int = 1) -> list[Payment]:
        # Newest first, mirroring /invoice_payments.json, including the status
        # narrowing the real client does server-side: a stub run that ignored it
        # would rehearse a page shape the poller never sees in production.
        start = (page - 1) * limit
        rows = list(reversed(self._seeded))[start : start + limit]
        if self._status is None:
            return rows
        return [row for row in rows if str(row.status) == self._status]

def make_stub_customer(**overrides) -> Customer:
    """A client fixture, shaped like what :class:`StubCustomerSource` serves.

    The phone is stored *un-normalized* (``010…``) for the same reason the payment
    fixture's is: it is what the Daftra mapper produces before the domain's
    normalizer runs, so a fixture that started out E.164 would quietly stop
    testing the rule that matters.
    """
    defaults = {
        "id": "1",
        "number": "000001",
        "customer_name": "Print Home",
        "customer_phone": "01027693262",
        "created": date(2026, 9, 24),
        "email": "",
        "type": "3",
        "suspend": "0",
        "is_offline": "1",
    }
    defaults.update(overrides)
    return Customer(**defaults)


def default_stub_customers() -> tuple[Customer, ...]:
    """Three clients that between them hit the branches a rehearsal must show.

    Deliberately mirrors the shapes seen live in Daftra, because these are the
    cases that break a mapper: a **company** (business name set, first/last
    empty — what Aizen Paper's own account actually holds), an **individual**
    (first + last, no business name), and one with **no phone**, which is the
    branch that most often surprises an operator: a customer nobody can be reached
    about is skipped and retired rather than retried forever.
    """
    return (
        make_stub_customer(),
        make_stub_customer(
            id="2",
            number="000002",
            customer_name="Mona Farouk",
            customer_phone="01234567890",
            created=date(2026, 9, 28),
            type="2",
        ),
        make_stub_customer(
            id="3",
            number="000003",
            customer_name="Nour Adel",
            customer_phone=None,
            created=date(2026, 10, 1),
            type="2",
        ),
    )


def _customer_to_dict(customer: Customer) -> dict:
    return {
        "id": customer.id,
        "number": customer.number,
        "customer_name": customer.customer_name,
        "customer_phone": customer.customer_phone,
        "created": customer.created.isoformat() if customer.created else None,
        "email": customer.email,
        "type": customer.type,
        "suspend": customer.suspend,
        "is_offline": customer.is_offline,
    }


def _customer_from_dict(data: dict) -> Customer:
    created = data.get("created")
    return Customer(
        id=str(data.get("id", "")),
        number=str(data.get("number", "")),
        customer_name=str(data.get("customer_name", "")),
        customer_phone=data.get("customer_phone"),
        created=date.fromisoformat(created) if created else None,
        email=str(data.get("email", "")),
        type=str(data.get("type", "")),
        suspend=str(data.get("suspend", "")),
        is_offline=str(data.get("is_offline", "")),
    )


class StubCustomerSource:
    """Offline stand-in for :class:`DaftraClient`'s client half.

    The third source stub, structurally identical to
    :class:`StubPaymentSource` minus the status filter: there is no server-side
    narrowing to mirror here, because the customers pipeline decides who is new
    from its own seen-set rather than from a query parameter.

    ``list_customers`` serves **newest first**, mirroring the
    ``sort=created&direction=desc`` the real adapter must request. That ordering
    is the poller's paging contract, so a stub that served insertion order would
    rehearse a walk production never performs.
    """

    def __init__(
        self,
        customers: Iterable[Customer] = (),
        path: str | None = None,
    ) -> None:
        self._seeded: list[Customer] = []
        self._index: dict[str, Customer] = {}
        self.requests: list[str] = []
        self._next_id = 1
        self._path = Path(path) if path else None
        if self._path is not None and self._path.exists():
            self._load_from_path()
        else:
            seed = list(customers) if customers else (
                list(default_stub_customers()) if self._path is not None else []
            )
            for customer in seed:
                self.seed(customer)
            if self._path is not None:
                self._save_to_path()
        numeric = [int(c.id) for c in self._seeded if str(c.id).isdigit()]
        self._next_id = max(numeric, default=0) + 1

    def seed(self, customer: Customer) -> None:
        self._seeded.append(customer)
        self._index[str(customer.id)] = customer
        self._index[customer.number] = customer

    def add_new_customer(self, **overrides) -> Customer:
        """Simulate a new client appearing in Daftra between poll cycles."""
        overrides.setdefault("id", str(self._next_id))
        # Zero-padded to six digits to match the shape Daftra gives a client
        # number, so a rehearsal does not normalize formatting the production
        # path will see.
        overrides.setdefault("number", f"{self._next_id:06d}")
        customer = make_stub_customer(**overrides)
        self.seed(customer)
        if str(customer.id).isdigit():
            self._next_id = max(self._next_id, int(customer.id) + 1)
        if self._path is not None:
            self._save_to_path()
        return customer

    def _load_from_path(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError):
            log.warning("stub customer fixture at %s is unreadable; starting empty", self._path)
            data = []
        for row in data if isinstance(data, list) else []:
            if isinstance(row, dict):
                self.seed(_customer_from_dict(row))

    def _save_to_path(self) -> None:
        try:
            write_json_atomic(self._path, [_customer_to_dict(c) for c in self._seeded])
        except OSError:
            log.warning("could not write the stub customer fixture at %s", self._path)

    def get_customer(self, customer_id: int | str) -> Customer:
        key = str(customer_id)
        self.requests.append(key)
        if key not in self._index:
            raise ValueError(f"Stub source has no customer {customer_id!r}")
        return self._index[key]

    def get_raw_customer(self, customer_id: int | str) -> dict:
        customer = self.get_customer(customer_id)
        return {
            "result": "successful",
            "code": 200,
            "data": {
                "Client": {
                    "id": customer.id,
                    "client_number": customer.number,
                    "business_name": customer.customer_name,
                    "first_name": "",
                    "last_name": "",
                    "email": customer.email,
                    "phone1": customer.customer_phone or "",
                    "phone2": "",
                    "country_code": "EG",
                    "created": customer.created.strftime("%Y-%m-%d 00:00:00")
                    if customer.created
                    else None,
                    "suspend": customer.suspend,
                    "is_offline": customer.is_offline,
                    "type": customer.type,
                }
            },
        }

    def list_customers(self, limit: int = 10, page: int = 1) -> list[Customer]:
        # Newest first, mirroring the sort the real adapter has to request.
        start = (page - 1) * limit
        return list(reversed(self._seeded))[start : start + limit]
