"""Offline behavioral specs for the WhatsApp payloads delivered by the notification service."""

from datetime import date
from decimal import Decimal

import pytest

from fakes import CapturingSender, FailingSender, StubInvoiceSource, make_stub_invoice
from sender.application.services import InvoiceNotificationService
from sender.domain.errors import WhatsAppApiError
from sender.domain.templates import LegacyInvoiceTemplateBuilder


CONTRACT_VALUES = ["Ahmed Hassan", "INV-001", "01/09/2026", "1,500.00"]


def _builder() -> LegacyInvoiceTemplateBuilder:
    return LegacyInvoiceTemplateBuilder(template_name="aizen_invoice", language="en", country_code="20")


def _body_parameters(payload: dict) -> list[dict]:
    components = payload["template"]["components"]
    assert [component["type"] for component in components] == ["header", "body"]
    return components[1]["parameters"]


def _service(sender, invoices=None) -> InvoiceNotificationService:
    source = StubInvoiceSource(invoices or [make_stub_invoice()])
    return InvoiceNotificationService(source=source, sender=sender, builder=_builder())


class RecordingListSource:
    """Records the arguments the service hands to the port's list_invoices."""

    def __init__(self, invoices=()) -> None:
        self._invoices = list(invoices)
        self.list_calls: list[dict] = []

    def list_invoices(self, limit: int = 10, page: int = 1):
        self.list_calls.append({"limit": limit, "page": page})
        return list(self._invoices)

    def get_invoice(self, invoice_id):
        return self._invoices[0]

    def get_raw_invoice(self, invoice_id):
        return {}


def test_list_invoices_passes_the_page_through_to_the_source():
    from sender.presentation.stubs import default_stub_invoices

    source = RecordingListSource(default_stub_invoices())
    service = InvoiceNotificationService(source=source, sender=CapturingSender(), builder=_builder())
    invoices = service.list_invoices(limit=5, page=3)
    assert source.list_calls == [{"limit": 5, "page": 3}]
    assert {invoice.number for invoice in invoices} == {"INV-001", "INV-002", "INV-003"}


def test_list_invoices_defaults_to_the_first_page():
    source = RecordingListSource()
    service = InvoiceNotificationService(source=source, sender=CapturingSender(), builder=_builder())
    service.list_invoices(2)
    assert source.list_calls == [{"limit": 2, "page": 1}]


def test_send_invoice_delivers_exactly_one_payload_to_the_sender():
    sender = CapturingSender()
    result = _service(sender).send_invoice("1")
    assert len(sender.calls) == 1
    assert result["payloads"][0] is sender.payloads[0]
    assert result["responses"] == [{"messages": [{"id": "wamid.FAKE"}]}]


def test_template_payload_declares_the_whatsapp_message_envelope():
    sender = CapturingSender()
    _service(sender).send_invoice("1")
    payload = sender.payloads[0]
    assert payload["messaging_product"] == "whatsapp"
    assert payload["recipient_type"] == "individual"
    assert payload["type"] == "template"
    assert payload["to"] == "201027693262"


def test_template_payload_carries_the_approved_template_name_and_language():
    sender = CapturingSender()
    _service(sender).send_invoice("1")
    payload = sender.payloads[0]
    assert payload["template"]["name"] == "aizen_invoice"
    assert payload["template"]["language"] == {"code": "en"}
    assert [component["type"] for component in payload["template"]["components"]] == ["header", "body"]


def test_template_header_attaches_the_invoice_public_url_document():
    sender = CapturingSender()
    _service(sender).send_invoice("1")
    header = sender.payloads[0]["template"]["components"][0]
    source = StubInvoiceSource([make_stub_invoice()])
    expected_link = source.get_invoice("1").public_url
    assert header == {
        "type": "header",
        "parameters": [
            {"type": "document", "document": {"link": expected_link, "filename": "INV-001.pdf"}}
        ],
    }


def test_template_body_parameters_are_the_four_contract_values_in_order():
    sender = CapturingSender()
    _service(sender).send_invoice("1")
    assert _body_parameters(sender.payloads[0]) == [
        {"type": "text", "text": "\u2068Ahmed Hassan\u2069"},
        {"type": "text", "text": "\u2066INV-001\u2069"},
        {"type": "text", "text": "\u206601/09/2026\u2069"},
        {"type": "text", "text": "\u20661,500.00\u2069"},
    ]


def test_send_freeform_delivers_exactly_one_payload_to_the_sender():
    sender = CapturingSender()
    result = _service(sender).send_freeform("1")
    assert len(sender.payloads) == 1
    assert result["payloads"][0] is sender.payloads[0]
    assert result["responses"] == [{"messages": [{"id": "wamid.FAKE"}]}]


def test_freeform_payload_is_a_plain_whatsapp_text_message():
    sender = CapturingSender()
    _service(sender).send_freeform("1")
    payload = sender.payloads[0]
    assert payload["type"] == "text"
    assert payload["messaging_product"] == "whatsapp"
    assert payload["recipient_type"] == "individual"
    assert payload["to"] == "201027693262"
    assert "template" not in payload


def test_freeform_body_is_a_non_empty_string():
    sender = CapturingSender()
    _service(sender).send_freeform("1")
    body = sender.payloads[0]["text"]["body"]
    assert isinstance(body, str)
    assert len(body) > 0


def test_freeform_body_contains_the_four_contract_values():
    sender = CapturingSender()
    _service(sender).send_freeform("1")
    body = sender.payloads[0]["text"]["body"]
    for value in CONTRACT_VALUES:
        assert value in body


def test_freeform_body_leaves_no_unsubstituted_placeholders():
    sender = CapturingSender()
    _service(sender).send_freeform("1")
    body = sender.payloads[0]["text"]["body"]
    assert "{{" not in body


def test_freeform_values_appear_in_the_template_parameter_order():
    sender = CapturingSender()
    service = _service(sender)
    service.send_invoice("1")
    service.send_freeform("1")
    template_values = [param["text"] for param in _body_parameters(sender.payloads[0])]
    positions = [sender.payloads[1]["text"]["body"].index(value) for value in template_values]
    assert positions == sorted(positions)


def test_missing_to_falls_back_to_the_invoice_customer_phone():
    sender = CapturingSender()
    result = _service(sender).send_invoice("1")
    assert result["recipients"] == ["01027693262"]
    assert sender.payloads[0]["to"] == "201027693262"


def test_explicit_to_overrides_the_invoice_customer_phone():
    sender = CapturingSender()
    result = _service(sender).send_invoice("1", to_phone="+201234567890")
    assert sender.payloads[0]["to"] == "201234567890"
    assert result["recipients"] == ["+201234567890"]


def test_local_format_to_is_normalized_with_the_default_country_code():
    sender = CapturingSender()
    _service(sender).send_invoice("1", to_phone="01234567890")
    assert sender.payloads[0]["to"] == "201234567890"


def test_invoice_without_phone_and_no_to_is_rejected():
    invoice = make_stub_invoice(customer_phones=())
    service = _service(CapturingSender(), invoices=[invoice])
    with pytest.raises(ValueError, match="no valid WhatsApp phone on file"):
        service.send_invoice("1")


def test_source_resolves_the_requested_invoice_by_id():
    second = make_stub_invoice(
        id="2",
        number="INV-002",
        customer_name="Mona Farouk",
        customer_phones=("01234567890",),
        issue_date=date(2026, 9, 5),
        total=Decimal("3200.50"),
    )
    sender = CapturingSender()
    _service(sender, invoices=[make_stub_invoice(), second]).send_invoice("2")
    texts = [p["text"] for p in _body_parameters(sender.payloads[0])]
    assert texts == [
        "\u2068Mona Farouk\u2069",
        "\u2066INV-002\u2069",
        "\u206605/09/2026\u2069",
        "\u20663,200.50\u2069",
    ]


def test_stub_registry_resolves_by_id_and_number():
    first = make_stub_invoice()
    second = make_stub_invoice(
        id="2",
        number="INV-002",
        customer_name="Mona Farouk",
        customer_phones=("01234567890",),
        issue_date=date(2026, 9, 5),
        total=Decimal("3200.50"),
    )
    source = StubInvoiceSource([first, second])
    assert source.get_invoice(1) is first
    assert source.get_invoice("2") is second
    assert source.get_invoice("INV-002") is second


def test_unknown_invoice_id_is_rejected():
    with pytest.raises(ValueError, match="Stub source has no invoice"):
        _service(CapturingSender()).send_invoice("99")


def test_dry_run_returns_the_template_payload_without_sending():
    sender = CapturingSender()
    result = _service(sender).send_invoice("1", dry_run=True)
    assert result["dry_run"] is True
    assert result["payloads"][0]["type"] == "template"
    assert result["payloads"][0]["to"] == "201027693262"
    assert sender.payloads == []
    assert "response" not in result


def test_dry_run_returns_the_text_payload_without_sending():
    sender = CapturingSender()
    result = _service(sender).send_freeform("1", dry_run=True)
    assert result["dry_run"] is True
    assert result["payloads"][0]["type"] == "text"
    assert "Ahmed Hassan" in result["payloads"][0]["text"]["body"]
    assert sender.payloads == []


def test_dry_run_still_requires_a_configured_sender():
    service = _service(None)
    with pytest.raises(RuntimeError, match="WhatsApp credentials are not configured"):
        service.send_invoice("1", dry_run=True)


def test_send_invoice_without_a_sender_raises_runtime_error():
    service = _service(None)
    with pytest.raises(RuntimeError, match="WhatsApp credentials are not configured"):
        service.send_invoice("1")


def test_send_freeform_without_a_sender_raises_runtime_error():
    service = _service(None)
    with pytest.raises(RuntimeError, match="WhatsApp credentials are not configured"):
        service.send_freeform("1")


def test_send_invoice_propagates_the_senders_whatsapp_api_error():
    failing = FailingSender()
    with pytest.raises(WhatsAppApiError) as excinfo:
        _service(failing).send_invoice("1")
    assert excinfo.value is failing.error
    assert len(failing.payloads) == 1
    assert failing.payloads[0]["type"] == "template"


def test_send_freeform_propagates_the_senders_whatsapp_api_error():
    failing = FailingSender()
    with pytest.raises(WhatsAppApiError) as excinfo:
        _service(failing).send_freeform("1")
    assert excinfo.value is failing.error
    assert failing.payloads[0]["type"] == "text"