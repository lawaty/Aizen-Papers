"""Offline checks for the freeform (plain-text) WhatsApp send path."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

from sender.domain.models import Invoice
from sender.domain.templates import LegacyInvoiceTemplateBuilder


def _sample_invoice() -> Invoice:
    return Invoice(
        id="1",
        number="INV-001",
        customer_name="Ahmed Hassan",
        issue_date=date(2026, 9, 1),
        total=Decimal("1500.00"),
    )


def _builder() -> LegacyInvoiceTemplateBuilder:
    return LegacyInvoiceTemplateBuilder(template_name="aizen_invoice", language="en", country_code="20")


def test_render_text_contains_exact_values():
    body = _builder().render_text(_sample_invoice())
    assert "Ahmed Hassan" in body
    assert "INV-001" in body
    assert "01/09/2026" in body
    assert "1,500.00" in body


def test_render_text_has_no_leftover_placeholders():
    body = _builder().render_text(_sample_invoice())
    for i in range(1, 5):
        assert f"{{{{{i}}}}}" not in body


def test_build_text_payload_shape():
    payload = _builder().build_text(_sample_invoice(), "01027693262")
    assert payload["messaging_product"] == "whatsapp"
    assert payload["recipient_type"] == "individual"
    assert payload["to"] == "201027693262"
    assert payload["type"] == "text"
    assert isinstance(payload["text"]["body"], str)
    assert "template" not in payload


def test_parameters_unchanged():
    params = _builder().parameters(_sample_invoice())
    texts = [param["text"] for param in params]
    assert texts == [
        "\u2068Ahmed Hassan\u2069",
        "\u2066INV-001\u2069",
        "\u206601/09/2026\u2069",
        "\u20661,500.00\u2069",
    ]
    assert all(param["type"] == "text" for param in params)


def test_build_template_unchanged():
    invoice = replace(_sample_invoice(), public_url="https://demo.daftra.com/invoices/INV-001.pdf")
    payload = _builder().build(invoice, "01027693262")
    assert payload["to"] == "201027693262"
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "aizen_invoice"
    assert payload["template"]["language"]["code"] == "en"
    assert [component["type"] for component in payload["template"]["components"]] == ["header", "body"]