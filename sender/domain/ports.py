from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import Invoice


class InvoiceSource(Protocol):
    def get_invoice(self, invoice_id: int | str) -> Invoice: ...
    def get_raw_invoice(self, invoice_id: int | str) -> dict: ...
    def list_invoices(self, limit: int = 10) -> list[Invoice]: ...


@runtime_checkable
class MessageSender(Protocol):
    def send(self, payload: dict) -> dict: ...