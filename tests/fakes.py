"""Offline test doubles for the invoice source and the WhatsApp sender."""

from sender.domain.errors import WhatsAppApiError
from sender.presentation.stubs import StubInvoiceSource, default_stub_invoices, make_stub_invoice


class CapturingSender:
    def __init__(self, response: dict | None = None) -> None:
        self.response = response or {"messages": [{"id": "wamid.FAKE"}]}
        self.payloads: list[dict] = []

    @property
    def calls(self) -> list[dict]:
        return self.payloads

    def send(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return self.response


class FailingSender:
    def __init__(self, error: WhatsAppApiError | None = None) -> None:
        self.error = error or WhatsAppApiError(131037, "Re-engagement message", {"error": {"code": 131037}})
        self.payloads: list[dict] = []

    def send(self, payload: dict) -> dict:
        self.payloads.append(payload)
        raise self.error