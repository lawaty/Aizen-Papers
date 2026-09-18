"""Offline behavioral specs for the WhatsApp payloads delivered by the notification service."""

from datetime import date
from decimal import Decimal

import pytest

from fakes import CapturingSender, FailingSender, StubInvoiceSource, make_stub_invoice
from sender.application.services import InvoiceNotificationService
from sender.domain.errors import WhatsAppApiError
from sender.domain.templates import InvoiceTemplateBuilder


CONTRACT_VALUES = ["Ahmed Hassan", "INV-001", "01/09/2026", "1,500.00"]


def _builder() -> InvoiceTemplateBuilder:
    return InvoiceTemplateBuilder(template_name="aizen_invoice", language="en_EG", country_code="20")


def _service(sender, invoices=None) -> InvoiceNotificationService:
    source = StubInvoiceSource(invoices or [make_stub_invoice()])
    return InvoiceNotificationService(source=source, sender=sender, builder=_builder())


def test_send_invoice_delivers_exactly_one_payload_to_the_sender():
    sender = CapturingSender()
    result = _service(sender).send_invoice("1")
    assert len(sender.calls) == 1
    assert result["payload"] is sender.payloads[0]
    assert result["response"] == {"messages": [{"id": "wamid.FAKE"}]}


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
    assert payload["template"]["language"] == {"code": "en_EG"}
    assert len(payload["template"]["components"]) == 1
    assert payload["template"]["components"][0]["type"] == "body"


def test_template_body_parameters_are_the_four_contract_values_in_order():
    sender = CapturingSender()
    _service(sender).send_invoice("1")
    payload = sender.payloads[0]
    assert payload["template"]["components"][0]["parameters"] == [
        {"type": "text", "text": "Ahmed Hassan"},
        {"type": "text", "text": "INV-001"},
        {"type": "text", "text": "01/09/2026"},
        {"type": "text", "text": "1,500.00"},
    ]


def test_send_freeform_delivers_exactly_one_payload_to_the_sender():
    sender = CapturingSender()
    result = _service(sender).send_freeform("1")
    assert len(sender.payloads) == 1
    assert result["payload"] is sender.payloads[0]
    assert result["response"] == {"messages": [{"id": "wamid.FAKE"}]}


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
    template_values = [param["text"] for param in sender.payloads[0]["template"]["components"][0]["parameters"]]
    positions = [sender.payloads[1]["text"]["body"].index(value) for value in template_values]
    assert positions == sorted(positions)


def test_missing_to_falls_back_to_the_invoice_customer_phone():
    sender = CapturingSender()
    result = _service(sender).send_invoice("1")
    assert result["to"] == "01027693262"
    assert sender.payloads[0]["to"] == "201027693262"


def test_explicit_to_overrides_the_invoice_customer_phone():
    sender = CapturingSender()
    result = _service(sender).send_invoice("1", to_phone="+201234567890")
    assert sender.payloads[0]["to"] == "201234567890"
    assert result["to"] == "+201234567890"


def test_local_format_to_is_normalized_with_the_default_country_code():
    sender = CapturingSender()
    _service(sender).send_invoice("1", to_phone="01234567890")
    assert sender.payloads[0]["to"] == "201234567890"


def test_invoice_without_phone_and_no_to_is_rejected():
    invoice = make_stub_invoice(customer_phone=None)
    service = _service(CapturingSender(), invoices=[invoice])
    with pytest.raises(ValueError, match="no valid WhatsApp phone on file"):
        service.send_invoice("1")


def test_source_resolves_the_requested_invoice_by_id():
    second = make_stub_invoice(
        id="2",
        number="INV-002",
        customer_name="Mona Farouk",
        customer_phone="01234567890",
        issue_date=date(2026, 9, 5),
        total=Decimal("3200.50"),
    )
    sender = CapturingSender()
    _service(sender, invoices=[make_stub_invoice(), second]).send_invoice("2")
    texts = [p["text"] for p in sender.payloads[0]["template"]["components"][0]["parameters"]]
    assert texts == ["Mona Farouk", "INV-002", "05/09/2026", "3,200.50"]


def test_stub_registry_resolves_by_id_and_number():
    first = make_stub_invoice()
    second = make_stub_invoice(
        id="2",
        number="INV-002",
        customer_name="Mona Farouk",
        customer_phone="01234567890",
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
    assert result["payload"]["type"] == "template"
    assert result["payload"]["to"] == "201027693262"
    assert sender.payloads == []
    assert "response" not in result


def test_dry_run_returns_the_text_payload_without_sending():
    sender = CapturingSender()
    result = _service(sender).send_freeform("1", dry_run=True)
    assert result["dry_run"] is True
    assert result["payload"]["type"] == "text"
    assert "Ahmed Hassan" in result["payload"]["text"]["body"]
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