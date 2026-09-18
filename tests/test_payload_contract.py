"""Offline payload contract checks for the approved aizen_invoice template."""

from datetime import date
from decimal import Decimal

from sender.domain.models import Invoice
from sender.domain.templates import InvoiceTemplateBuilder


def _sample_invoice() -> Invoice:
    return Invoice(
        id="1",
        number="INV-001",
        customer_name="Ahmed Hassan",
        issue_date=date(2026, 9, 1),
        total=Decimal("1500.00"),
    )


def test_parameters_match_approved_template_body_placeholders():
    builder = InvoiceTemplateBuilder(template_name="aizen_invoice", language="en_EG", country_code="20")
    params = builder.parameters(_sample_invoice())
    texts = [param["text"] for param in params]
    assert texts == ["Ahmed Hassan", "INV-001", "01/09/2026", "1,500.00"]
    assert all(param["type"] == "text" for param in params)


def test_build_normalizes_recipient_and_wraps_template():
    builder = InvoiceTemplateBuilder(template_name="aizen_invoice", language="en_EG", country_code="20")
    payload = builder.build(_sample_invoice(), "01027693262")
    assert payload["to"] == "201027693262"
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "aizen_invoice"
    assert payload["template"]["language"]["code"] == "en_EG"