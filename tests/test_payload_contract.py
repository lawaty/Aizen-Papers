"""Offline payload contract checks for the aizen_invoice template builders."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

from sender.domain.models import Invoice
from sender.domain.templates import (
    CleanTextTemplateBuilder,
    LegacyInvoiceTemplateBuilder,
)


def _sample_invoice() -> Invoice:
    return Invoice(
        id="1",
        number="INV-001",
        customer_name="Ahmed Hassan",
        issue_date=date(2026, 9, 1),
        total=Decimal("1500.00"),
        public_url="https://demo.daftra.com/invoices/INV-001.pdf",
    )


def test_parameters_match_template_body_placeholders():
    builder = LegacyInvoiceTemplateBuilder(template_name="aizen_invoice", language="en", country_code="20")
    params = builder.parameters(_sample_invoice())
    texts = [param["text"] for param in params]
    assert texts == [
        "\u2068Ahmed Hassan\u2069",
        "\u2066INV-001\u2069",
        "\u206601/09/2026\u2069",
        "\u20661,500.00\u2069",
    ]
    assert all(param["type"] == "text" for param in params)


def test_build_normalizes_recipient_and_wraps_template():
    builder = LegacyInvoiceTemplateBuilder(template_name="aizen_invoice", language="en", country_code="20")
    payload = builder.build(_sample_invoice(), "01027693262")
    assert payload["to"] == "201027693262"
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "aizen_invoice"
    assert payload["template"]["language"]["code"] == "en"


def test_legacy_build_carries_the_header_document_from_the_invoice_public_url():
    builder = LegacyInvoiceTemplateBuilder(template_name="aizen_invoice", language="en", country_code="20")
    payload = builder.build(_sample_invoice(), "01027693262")
    header = payload["template"]["components"][0]
    assert header["type"] == "header"
    assert header["parameters"] == [
        {
            "type": "document",
            "document": {
                "link": "https://demo.daftra.com/invoices/INV-001.pdf",
                "filename": "INV-001.pdf",
            },
        }
    ]


def test_legacy_build_body_component_sits_after_the_header():
    builder = LegacyInvoiceTemplateBuilder(template_name="aizen_invoice", language="en", country_code="20")
    payload = builder.build(_sample_invoice(), "01027693262")
    assert [component["type"] for component in payload["template"]["components"]] == ["header", "body"]


def test_legacy_build_requires_a_public_url_for_the_header_document():
    builder = LegacyInvoiceTemplateBuilder(template_name="aizen_invoice", language="en", country_code="20")
    invoice = replace(_sample_invoice(), public_url=None)
    try:
        builder.build(invoice, "01027693262")
    except ValueError as exc:
        assert "no public URL" in str(exc)
    else:
        raise AssertionError("expected ValueError for missing public URL")


def test_clean_build_is_body_only_without_media_header():
    builder = CleanTextTemplateBuilder(template_name="aizen_invoice", language="en", country_code="20")
    payload = builder.build(_sample_invoice(), "01027693262")
    assert [component["type"] for component in payload["template"]["components"]] == ["body"]
    assert payload["template"]["components"][0]["parameters"] == builder.parameters(_sample_invoice())


def test_clean_build_ignores_missing_public_url():
    builder = CleanTextTemplateBuilder(template_name="aizen_invoice", language="en", country_code="20")
    invoice = replace(_sample_invoice(), public_url=None)
    payload = builder.build(invoice, "01027693262")
    assert payload["template"]["components"][0]["type"] == "body"


def test_base_builder_requires_a_concrete_subclass():
    from sender.domain.templates import InvoiceTemplateBuilder

    builder = InvoiceTemplateBuilder(template_name="aizen_invoice", language="en", country_code="20")
    try:
        builder.build(_sample_invoice(), "01027693262")
    except NotImplementedError:
        pass
    else:
        raise AssertionError("expected NotImplementedError from the base builder")


def test_a_truncated_parameter_keeps_its_closing_isolation_mark():
    # Truncation used to happen after the FSI/LRI wrap, so a long customer name
    # lost its PDI: an unterminated isolate breaks the bidi rendering of the rest
    # of the message. The value is capped at 512 chars, marks included.
    builder = LegacyInvoiceTemplateBuilder(template_name="aizen_invoice", language="en", country_code="20")
    name = "Ahmed " * 100  # 600 characters, over the 512 cap
    params = builder.parameters(replace(_sample_invoice(), customer_name=name))
    text = params[0]["text"]
    assert text.startswith("\u2068")
    assert text.endswith("\u2069")
    assert text.count("\u2069") == 1
    assert len(text) <= 512
    assert "\u2068" * 2 not in text
