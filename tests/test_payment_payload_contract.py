"""The ``aizen_new_payment`` template contract, pinned.

Mirrors ``test_payload_contract.py``. These tests exist because a parameter-order
or format mismatch does not fail anywhere in this repo — it surfaces only as a
``132000``-series rejection from Meta, at send time, for a real customer.
"""

from datetime import date
from decimal import Decimal

import pytest

from sender.domain.models import Payment
from sender.domain.templates import (
    PaymentTemplateBuilder,
    TemplateBuilder,
    InvoiceTemplateBuilder,
)
from sender.presentation.stubs import make_stub_payment

FSI = "\u2068"
LRI = "\u2066"
PDI = "\u2069"


def _builder() -> PaymentTemplateBuilder:
    return PaymentTemplateBuilder(
        template_name="aizen_new_payment", language="ar_EG", country_code="20"
    )


def _params(payload: dict) -> list[str]:
    return [p["text"] for p in payload["template"]["components"][0]["parameters"]]


def test_the_defaults_match_the_approved_template():
    """Meta serves this template in ar_EG only; a default of ``en`` would be a
    132001 on every single send."""
    payload = PaymentTemplateBuilder().build(make_stub_payment(), "01027693262")
    assert payload["template"]["name"] == "aizen_new_payment"
    assert payload["template"]["language"]["code"] == "ar_EG"


def test_the_parameters_are_in_the_order_the_body_reads_them():
    payment = make_stub_payment(
        customer_name="Ahmed Hassan",
        number="000116",
        payment_date=date(2026, 5, 2),
        amount=Decimal("32999.9961"),
    )
    name, number, paid, amount = _params(_builder().build(payment, "01027693262"))
    # who, which receipt, when, how much
    assert name == f"{FSI}Ahmed Hassan{PDI}"
    assert number == f"{LRI}000116{PDI}"
    assert paid == f"{LRI}02/05/2026{PDI}"
    assert amount == f"{LRI}33,000.00{PDI}"


def test_the_amount_carries_no_currency_symbol():
    """``ج.م`` is hardcoded in the approved body, so a symbol here would print twice."""
    payload = _builder().build(make_stub_payment(amount=Decimal("1500")), "01027693262")
    assert _params(payload)[3] == f"{LRI}1,500.00{PDI}"


def test_the_date_is_day_first():
    payload = _builder().build(make_stub_payment(payment_date=date(2026, 12, 5)), "01027693262")
    assert _params(payload)[2] == f"{LRI}05/12/2026{PDI}"


def test_a_missing_date_does_not_crash_the_send():
    payload = _builder().build(make_stub_payment(payment_date=None), "01027693262")
    assert _params(payload)[2] == f"{LRI}N/A{PDI}"


def test_the_recipient_is_normalized():
    payload = _builder().build(make_stub_payment(), "01027693262")
    assert payload["to"] == "201027693262"
    assert payload["messaging_product"] == "whatsapp"
    assert payload["type"] == "template"


def test_the_payload_is_body_only_because_the_template_has_no_header():
    """``aizen_new_payment`` has no header component, so a document parameter
    would be a parameter Meta rejects outright."""
    payload = _builder().build(make_stub_payment(), "01027693262")
    components = payload["template"]["components"]
    assert [c["type"] for c in components] == ["body"]


def test_a_payment_builder_is_not_an_invoice_builder():
    """The CLI's builder-swap retry and the attachment plumbing test for the
    invoice builders specifically; a payment builder must never satisfy them."""
    payment_builder = _builder()
    assert isinstance(payment_builder, TemplateBuilder)
    assert not isinstance(payment_builder, InvoiceTemplateBuilder)
    # And it takes no attachment provider: there is no header to attach to.
    assert not hasattr(payment_builder, "attachment")


def test_the_freeform_body_carries_no_isolation_marks_of_its_own():
    """``render_text`` substitutes values that are *already* isolated, so keeping
    the approved template's own marks would wrap every value twice."""
    body = PaymentTemplateBuilder.PAYMENT_TEMPLATE_BODY
    assert FSI not in body
    assert PDI not in body
    assert body.count("{{1}}") == body.count("{{2}}") == body.count("{{3}}") == body.count("{{4}}") == 1


def test_the_freeform_text_renders_the_payment_body():
    text = _builder().build_text(
        make_stub_payment(customer_name="Ahmed Hassan", number="000116", amount=Decimal("1500")),
        "01027693262",
    )
    assert text["type"] == "text"
    assert text["to"] == "201027693262"
    assert f"{FSI}Ahmed Hassan{PDI}" in text["text"]["body"]
    assert f"{LRI}000116{PDI}" in text["text"]["body"]
    assert "{{" not in text["text"]["body"], "every placeholder must be substituted"


def test_the_freeform_body_is_the_approved_wording():
    """The fallback message is what a customer reads when the template is broken,
    so its wording is part of the contract, not a debug aid."""
    body = PaymentTemplateBuilder.PAYMENT_TEMPLATE_BODY
    # Matched without the vowelling, which is presentation, not contract.
    assert "تسجيل دفعة جديدة" in body
    assert "رقم العملية" in body
    assert "تاريخ الدفع" in body
    assert "مبلغ الدفعة" in body
    assert "رصيد حسابكم" in body


def test_an_overlong_value_is_truncated_inside_the_isolation():
    """Sanitizing before isolating is what keeps the closing PDI attached."""
    value = "A" * 600
    name = _params(_builder().build(make_stub_payment(customer_name=value), "01027693262"))[0]
    assert name.startswith(FSI)
    assert name.endswith(PDI)
    # 512 characters total, the two marks included in the budget.
    assert len(name) == 512


def test_whitespace_in_a_value_is_collapsed():
    name = _params(
        _builder().build(make_stub_payment(customer_name="  Ahmed\n\tHassan  "), "01027693262")
    )[0]
    assert name == f"{FSI}Ahmed Hassan{PDI}"


def test_an_empty_value_becomes_a_dash_rather_than_a_blank():
    name = _params(_builder().build(make_stub_payment(customer_name=""), "01027693262"))[0]
    assert name == f"{FSI}-{PDI}"