from __future__ import annotations

from sender.domain.models import Invoice
from sender.domain.ports import InvoiceSource, MessageSender
from sender.domain.templates import InvoiceTemplateBuilder


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