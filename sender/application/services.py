from __future__ import annotations

from sender.domain.models import Customer, Invoice, Payment
from sender.domain.ports import CustomerSource, InvoiceSource, MessageSender, PaymentSource
from sender.domain.templates import InvoiceTemplateBuilder, TemplateBuilder


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
        invoice = self.get_invoice(invoice_id)
        recipient = to_phone or invoice.customer_phone
        if not recipient:
            raise ValueError(
                f"Invoice {invoice.number} has no valid WhatsApp phone on file "
                "(missing or failed normalization); pass --to explicitly."
            )
        builder_method = self._builder.build_text if freeform else self._builder.build
        return invoice, recipient, builder_method(invoice, recipient)

    def send_invoice(self, invoice_id: int | str, to_phone: str | None = None, dry_run: bool = False) -> dict:
        if self._sender is None:
            raise RuntimeError("WhatsApp credentials are not configured; cannot send.")
        invoice, recipient, payload = self.preview_invoice(invoice_id, to_phone)
        if dry_run:
            return {"dry_run": True, "invoice": invoice, "payload": payload, "to": recipient}
        response = self._sender.send(payload)
        return {"invoice": invoice, "payload": payload, "response": response, "to": recipient}

    def send_freeform(self, invoice_id: int | str, to_phone: str | None = None, dry_run: bool = False) -> dict:
        if self._sender is None:
            raise RuntimeError("WhatsApp credentials are not configured; cannot send.")
        invoice, recipient, payload = self.preview_invoice(invoice_id, to_phone, freeform=True)
        if dry_run:
            return {"dry_run": True, "invoice": invoice, "payload": payload, "to": recipient}
        response = self._sender.send(payload)
        return {"invoice": invoice, "payload": payload, "response": response, "to": recipient}


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
        recipient = to_phone or payment.customer_phone
        if not recipient:
            raise ValueError(
                f"Payment {payment.number} has no valid WhatsApp phone on file "
                "(the payer is read from the linked invoice, which may have no "
                "number); pass --to explicitly."
            )
        builder_method = self._builder.build_text if freeform else self._builder.build
        return payment, recipient, builder_method(payment, recipient)

    def send_payment(
        self, payment_id: int | str, to_phone: str | None = None, dry_run: bool = False
    ) -> dict:
        if self._sender is None:
            raise RuntimeError("WhatsApp credentials are not configured; cannot send.")
        payment, recipient, payload = self.preview_payment(payment_id, to_phone)
        if dry_run:
            return {"dry_run": True, "payment": payment, "payload": payload, "to": recipient}
        response = self._sender.send(payload)
        return {"payment": payment, "payload": payload, "response": response, "to": recipient}

    def send_freeform(
        self, payment_id: int | str, to_phone: str | None = None, dry_run: bool = False
    ) -> dict:
        if self._sender is None:
            raise RuntimeError("WhatsApp credentials are not configured; cannot send.")
        payment, recipient, payload = self.preview_payment(payment_id, to_phone, freeform=True)
        if dry_run:
            return {"dry_run": True, "payment": payment, "payload": payload, "to": recipient}
        response = self._sender.send(payload)
        return {"payment": payment, "payload": payload, "response": response, "to": recipient}


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
        recipient = to_phone or customer.customer_phone
        if not recipient:
            raise ValueError(
                f"Customer {customer.number} has no valid WhatsApp phone on file; "
                "pass --to explicitly."
            )
        builder_method = self._builder.build_text if freeform else self._builder.build
        return customer, recipient, builder_method(customer, recipient)

    def send_customer(
        self, customer_id: int | str, to_phone: str | None = None, dry_run: bool = False
    ) -> dict:
        if self._sender is None:
            raise RuntimeError("WhatsApp credentials are not configured; cannot send.")
        customer, recipient, payload = self.preview_customer(customer_id, to_phone)
        if dry_run:
            return {"dry_run": True, "customer": customer, "payload": payload, "to": recipient}
        response = self._sender.send(payload)
        return {"customer": customer, "payload": payload, "response": response, "to": recipient}

    def send_freeform(
        self, customer_id: int | str, to_phone: str | None = None, dry_run: bool = False
    ) -> dict:
        if self._sender is None:
            raise RuntimeError("WhatsApp credentials are not configured; cannot send.")
        customer, recipient, payload = self.preview_customer(customer_id, to_phone, freeform=True)
        if dry_run:
            return {"dry_run": True, "customer": customer, "payload": payload, "to": recipient}
        response = self._sender.send(payload)
        return {"customer": customer, "payload": payload, "response": response, "to": recipient}
