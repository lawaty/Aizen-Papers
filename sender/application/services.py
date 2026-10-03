from __future__ import annotations

from collections.abc import Sequence

from sender.domain.models import Customer, Invoice, Payment
from sender.domain.ports import CustomerSource, InvoiceSource, MessageSender, PaymentSource
from sender.domain.templates import InvoiceTemplateBuilder, TemplateBuilder


def _recipients_for(doc, to_phone: str | None, *, noun: str, number: str) -> tuple[str, ...]:
    """The numbers a manual send should go to, in order.

    An explicit ``--to`` is a single recipient and stays exactly that: it is the
    operator overriding the customer record, so honouring it literally is the
    whole point. Otherwise the document's own phones are used — every one of
    them, because Daftra keeps two fields on a client and either may be filled,
    and a customer who gave the business two numbers asked to be reachable on
    both.

    Duplicates are collapsed here as well as in the adapter and the poller,
    because two fields holding the same number must not become two messages to
    one person.
    """
    if to_phone:
        return (to_phone,)
    recipients: list[str] = []
    for phone in doc.customer_phones:
        if phone and phone not in recipients:
            recipients.append(phone)
    if not recipients:
        raise ValueError(
            f"{noun} {number} has no valid WhatsApp phone on file "
            "(missing or failed normalization); pass --to explicitly."
        )
    return tuple(recipients)


def _fan_out(
    sender: MessageSender,
    recipients: Sequence[str],
    payloads: Sequence[dict],
    delivered: list[str],
    *,
    exclude: Sequence[str] = (),
) -> tuple[list[str], list[dict], list[dict]]:
    """POST one payload per recipient, returning what went out.

    ``delivered`` accumulates the recipients Meta accepted and is what the
    caller's ``delivered_recipients`` exposes. Each recipient is appended *after*
    its own POST succeeds, so a failure part-way through leaves an accurate
    record: the caller retries with those numbers excluded, which is the only way
    a re-sent template cannot land twice on a customer who already got it.

    The failing recipient is deliberately not appended — its message did not go
    out, so the retry must include it.
    """
    skip = set(exclude)
    sent: list[str] = []
    sent_payloads: list[dict] = []
    responses: list[dict] = []
    for recipient, payload in zip(recipients, payloads):
        if recipient in skip:
            continue
        response = sender.send(payload)
        delivered.append(recipient)
        sent.append(recipient)
        sent_payloads.append(payload)
        responses.append(response)
    return sent, sent_payloads, responses


class InvoiceNotificationService:
    def __init__(
        self,
        source: InvoiceSource,
        sender: MessageSender | None,
        builder: InvoiceTemplateBuilder,
    ) -> None:
        self._source = source
        self._sender = sender
        self._builder = builder
        self._delivered: list[str] = []

    @property
    def builder(self):
        return self._builder

    @property
    def delivered_recipients(self) -> tuple[str, ...]:
        """The numbers the last send actually delivered to.

        Read this in the ``except`` branch of a retry: it is how many of the
        customer-facing sends already went out, so the retry excludes them rather
        than messaging those customers a second time.
        """
        return tuple(self._delivered)

    @property
    def builder(self) -> InvoiceTemplateBuilder:
        return self._builder

    def swap_builder(self, builder: InvoiceTemplateBuilder) -> None:
        self._builder = builder

    def get_invoice(self, invoice_id: int | str) -> Invoice:
        return self._source.get_invoice(invoice_id)

    def get_raw_invoice(self, invoice_id: int | str) -> dict:
        return self._source.get_raw_invoice(invoice_id)

    def list_invoices(self, limit: int = 10, page: int = 1) -> list[Invoice]:
        # The port declares a page (the poller pages over saturated listings);
        # dropping it here silently pinned every caller to the first page.
        return self._source.list_invoices(limit=limit, page=page)

    def build_message(self, invoice: Invoice, to_phone: str) -> dict:
        return self._builder.build(invoice, to_phone)

    def preview_invoice(self, invoice_id: int | str, to_phone: str | None = None, freeform: bool = False):
        """The invoice, the numbers it goes to, and one payload per number.

        Returns tuples rather than single values because the recipient count is a
        property of the customer's record, not of the caller: an invoice whose
        client has two filled phone fields has two payloads, and the builder runs
        once per recipient because each payload carries that recipient's number.
        """
        invoice = self.get_invoice(invoice_id)
        recipients = _recipients_for(
            invoice, to_phone, noun="Invoice", number=invoice.number
        )
        builder_method = self._builder.build_text if freeform else self._builder.build
        return invoice, recipients, [builder_method(invoice, r) for r in recipients]

    def send_invoice(
        self,
        invoice_id: int | str,
        to_phone: str | None = None,
        dry_run: bool = False,
        *,
        exclude: Sequence[str] = (),
    ) -> dict:
        if self._sender is None:
            raise RuntimeError("WhatsApp credentials are not configured; cannot send.")
        invoice, recipients, payloads = self.preview_invoice(invoice_id, to_phone)
        if dry_run:
            return {
                "dry_run": True, "invoice": invoice,
                "recipients": list(recipients), "payloads": payloads,
            }
        self._delivered = []
        sent, sent_payloads, responses = _fan_out(
            self._sender, recipients, payloads, self._delivered, exclude=exclude
        )
        return {
            "invoice": invoice, "recipients": sent,
            "payloads": sent_payloads, "responses": responses,
        }

    def send_freeform(
        self, invoice_id: int | str, to_phone: str | None = None, dry_run: bool = False
    ) -> dict:
        if self._sender is None:
            raise RuntimeError("WhatsApp credentials are not configured; cannot send.")
        invoice, recipients, payloads = self.preview_invoice(
            invoice_id, to_phone, freeform=True
        )
        if dry_run:
            return {
                "dry_run": True, "invoice": invoice,
                "recipients": list(recipients), "payloads": payloads,
            }
        self._delivered = []
        sent, sent_payloads, responses = _fan_out(
            self._sender, recipients, payloads, self._delivered
        )
        return {
            "invoice": invoice, "recipients": sent,
            "payloads": sent_payloads, "responses": responses,
        }


class PaymentNotificationService:
    """The payments counterpart of :class:`InvoiceNotificationService`.

    The shape is identical on purpose — one-shot send, preview, raw fetch — because
    an operator should not have to learn a second vocabulary. What is *not* here
    is ``swap_builder``: there is exactly one legal payload shape for the payment
    template (no header component), so there is nothing to swap to, and offering
    the method would imply a choice that does not exist.
    """

    def __init__(
        self,
        source: PaymentSource,
        sender: MessageSender | None,
        builder: TemplateBuilder,
    ) -> None:
        self._source = source
        self._sender = sender
        self._builder = builder
        self._delivered: list[str] = []

    @property
    def builder(self):
        return self._builder

    @property
    def delivered_recipients(self) -> tuple[str, ...]:
        """The numbers the last send actually delivered to (see the invoice service)."""
        return tuple(self._delivered)

    @property
    def builder(self) -> TemplateBuilder:
        return self._builder

    def get_payment(self, payment_id: int | str) -> Payment:
        return self._source.get_payment(payment_id)

    def get_raw_payment(self, payment_id: int | str) -> dict:
        return self._source.get_raw_payment(payment_id)

    def list_payments(self, limit: int = 10, page: int = 1) -> list[Payment]:
        return self._source.list_payments(limit=limit, page=page)

    def build_message(self, payment: Payment, to_phone: str) -> dict:
        return self._builder.build(payment, to_phone)

    def preview_payment(
        self, payment_id: int | str, to_phone: str | None = None, freeform: bool = False
    ):
        payment = self.get_payment(payment_id)
        # A payment's phones come from the *payer's own client record*, so it
        # carries both of that client's numbers exactly as the invoice does.
        recipients = _recipients_for(payment, to_phone, noun="Payment", number=payment.number)
        builder_method = self._builder.build_text if freeform else self._builder.build
        return payment, recipients, [builder_method(payment, r) for r in recipients]

    def send_payment(
        self, payment_id: int | str, to_phone: str | None = None, dry_run: bool = False
    ) -> dict:
        if self._sender is None:
            raise RuntimeError("WhatsApp credentials are not configured; cannot send.")
        payment, recipients, payloads = self.preview_payment(payment_id, to_phone)
        if dry_run:
            return {
                "dry_run": True, "payment": payment,
                "recipients": list(recipients), "payloads": payloads,
            }
        self._delivered = []
        sent, sent_payloads, responses = _fan_out(
            self._sender, recipients, payloads, self._delivered
        )
        return {
            "payment": payment, "recipients": sent,
            "payloads": sent_payloads, "responses": responses,
        }

    def send_freeform(
        self, payment_id: int | str, to_phone: str | None = None, dry_run: bool = False
    ) -> dict:
        if self._sender is None:
            raise RuntimeError("WhatsApp credentials are not configured; cannot send.")
        payment, recipients, payloads = self.preview_payment(payment_id, to_phone, freeform=True)
        if dry_run:
            return {
                "dry_run": True, "payment": payment,
                "recipients": list(recipients), "payloads": payloads,
            }
        self._delivered = []
        sent, sent_payloads, responses = _fan_out(
            self._sender, recipients, payloads, self._delivered
        )
        return {
            "payment": payment, "recipients": sent,
            "payloads": sent_payloads, "responses": responses,
        }


class CustomerNotificationService:
    """The customers counterpart of :class:`PaymentNotificationService`.

    The shape is identical for the same reason: one-shot send, preview, raw fetch,
    so an operator does not have to learn a third vocabulary. As with payments
    there is no ``swap_builder`` — the welcome template has exactly one legal
    payload shape (body only, no header) — and, unlike the other two, free-form
    text is not a normal operating mode here: the shared default is on only
    because the invoice template has no approved English translation, and a
    welcome to a brand-new number is outside the 24-hour window where free-form
    is deliverable at all. The preview path still offers it for layout testing.
    """

    def __init__(
        self,
        source: CustomerSource,
        sender: MessageSender | None,
        builder: TemplateBuilder,
    ) -> None:
        self._source = source
        self._sender = sender
        self._builder = builder
        self._delivered: list[str] = []

    @property
    def builder(self):
        return self._builder

    @property
    def delivered_recipients(self) -> tuple[str, ...]:
        """The numbers the last send actually delivered to (see the invoice service)."""
        return tuple(self._delivered)

    @property
    def builder(self) -> TemplateBuilder:
        return self._builder

    def get_customer(self, customer_id: int | str) -> Customer:
        return self._source.get_customer(customer_id)

    def get_raw_customer(self, customer_id: int | str) -> dict:
        return self._source.get_raw_customer(customer_id)

    def list_customers(self, limit: int = 10, page: int = 1) -> list[Customer]:
        return self._source.list_customers(limit=limit, page=page)

    def build_message(self, customer: Customer, to_phone: str) -> dict:
        return self._builder.build(customer, to_phone)

    def preview_customer(
        self, customer_id: int | str, to_phone: str | None = None, freeform: bool = False
    ):
        customer = self.get_customer(customer_id)
        # A client row carries both of its own phone fields, so a welcome can go
        # to both numbers without the customer record being consulted twice.
        recipients = _recipients_for(customer, to_phone, noun="Customer", number=customer.number)
        builder_method = self._builder.build_text if freeform else self._builder.build
        return customer, recipients, [builder_method(customer, r) for r in recipients]

    def send_customer(
        self, customer_id: int | str, to_phone: str | None = None, dry_run: bool = False
    ) -> dict:
        if self._sender is None:
            raise RuntimeError("WhatsApp credentials are not configured; cannot send.")
        customer, recipients, payloads = self.preview_customer(customer_id, to_phone)
        if dry_run:
            return {
                "dry_run": True, "customer": customer,
                "recipients": list(recipients), "payloads": payloads,
            }
        self._delivered = []
        sent, sent_payloads, responses = _fan_out(
            self._sender, recipients, payloads, self._delivered
        )
        return {
            "customer": customer, "recipients": sent,
            "payloads": sent_payloads, "responses": responses,
        }

    def send_freeform(
        self, customer_id: int | str, to_phone: str | None = None, dry_run: bool = False
    ) -> dict:
        if self._sender is None:
            raise RuntimeError("WhatsApp credentials are not configured; cannot send.")
        customer, recipients, payloads = self.preview_customer(customer_id, to_phone, freeform=True)
        if dry_run:
            return {
                "dry_run": True, "customer": customer,
                "recipients": list(recipients), "payloads": payloads,
            }
        self._delivered = []
        sent, sent_payloads, responses = _fan_out(
            self._sender, recipients, payloads, self._delivered
        )
        return {
            "customer": customer, "recipients": sent,
            "payloads": sent_payloads, "responses": responses,
        }
