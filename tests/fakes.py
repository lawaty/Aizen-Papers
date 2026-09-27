"""Offline test doubles for the invoice source and the WhatsApp sender."""

import json

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


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self.payload = payload
        self.headers: dict = {}

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    @property
    def text(self) -> str:
        return json.dumps(self.payload) if self.payload else ""

    def json(self) -> dict:
        if self.payload is None:
            raise ValueError("no json body")
        return self.payload


class FakeSession:
    def __init__(self, responses: dict | None = None) -> None:
        self.headers: dict = {}
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict = responses or {}
        #: URLs in call order. ``calls`` keeps the legacy (method, kwargs) shape,
        #: and the media upload protocol lives or dies on which endpoint was hit,
        #: so the URL is recorded separately rather than stuffed into kwargs.
        self.urls: list[str] = []
        #: (method, url, kwargs) per call, for assertions that need all three.
        self.requests_log: list[tuple[str, str, dict]] = []

    def _record(self, method: str, url: str, kwargs: dict) -> FakeResponse:
        self.calls.append((method, dict(kwargs)))
        self.urls.append(url)
        self.requests_log.append((method, url, dict(kwargs)))
        return self._response_for(method)

    def _response_for(self, method: str) -> FakeResponse:
        """Resolve a canned response for *method*.

        A list is consumed in order, which is how the two-phase media upload gets
        a different answer for the upload and the finish step.
        """
        canned = self.responses.get(method)
        if isinstance(canned, list):
            return canned.pop(0) if canned else FakeResponse(200, {})
        if canned is not None:
            return canned
        if method == "get":
            return FakeResponse(200, {"display_phone_number": "201280805534"})
        return FakeResponse(200, {"messages": [{"id": "wamid.OK"}]})

    def get(self, url: str, **kwargs) -> FakeResponse:
        return self._record("get", url, kwargs)

    def post(self, url: str, **kwargs) -> FakeResponse:
        return self._record("post", url, kwargs)
