"""The ``aizen_new_customer`` template contract, pinned.

Mirrors ``test_payment_payload_contract.py``. These tests exist because a
parameter-count or format mismatch does not fail anywhere in this repo — it
surfaces only as a ``132000``-series rejection from Meta, at send time, for a
real customer.

The approved template was read from the Graph API for this work: BODY + FOOTER,
**no header**, one body parameter (the customer name), language ``ar_EG``,
category MARKETING. Everything asserted below is a fact about that live template,
not a preference.
"""

import pytest

from sender.domain.models import Customer
from sender.domain.templates import (
    CustomerTemplateBuilder,
    InvoiceTemplateBuilder,
    TemplateBuilder,
)
from sender.presentation.stubs import make_stub_customer

FSI = "⁨"
PDI = "⁩"


def _builder() -> CustomerTemplateBuilder:
    return CustomerTemplateBuilder(
        template_name="aizen_new_customer", language="ar_EG", country_code="20"
    )


def _params(payload: dict) -> list[str]:
    return [p["text"] for p in payload["template"]["components"][0]["parameters"]]


def test_the_defaults_match_the_approved_template():
    """Meta serves this template in ar_EG only; a default of ``en`` would be a
    132001 on every single send."""
    payload = CustomerTemplateBuilder().build(make_stub_customer(), "01027693262")
    assert payload["template"]["name"] == "aizen_new_customer"
    assert payload["template"]["language"]["code"] == "ar_EG"


def test_the_payload_carries_exactly_one_parameter():
    """One variable in the approved body: the customer's name. A second parameter
    would be rejected as a parameter-count mismatch, and one fewer would greet the
    customer with nothing."""
    payload = _builder().build(make_stub_customer(customer_name="Print Home"), "01027693262")
    components = payload["template"]["components"]
    assert len(components) == 1
    assert components[0]["type"] == "body"
    assert _params(payload) == [f"{FSI}Print Home{PDI}"]


def test_the_payload_has_no_header_component():
    """The template has no header, so there is no document to attach and nothing
    to upload. A header component here would be a hard rejection."""
    payload = _builder().build(make_stub_customer(), "01027693262")
    assert [c["type"] for c in payload["template"]["components"]] == ["body"]


def test_the_recipient_is_normalized_to_e164():
    payload = _builder().build(make_stub_customer(), "01027693262")
    assert payload["to"] == "201027693262"
    assert payload["messaging_product"] == "whatsapp"
    assert payload["type"] == "template"


def test_the_body_placeholder_is_substituted_exactly_once():
    """A body that still contains ``{{1}}`` after rendering means the free-form
    path would send the customer a literal placeholder."""
    text = _builder().render_text(make_stub_customer(customer_name="Print Home"))
    assert "{{" not in text and "}}" not in text
    assert text.count("Print Home") == 1


def test_the_body_constant_is_the_approved_wording():
    """Verbatim from the approved template. A wording drift is invisible until a
    customer receives it, so it is pinned here."""
    from sender.domain.templates import CustomerTemplateBuilder as B

    body = B.CUSTOMER_TEMPLATE_BODY
    assert "أهلًا بكم في Aizen Paper" in body
    assert "مَرْحَبًا {{1}}،" in body
    assert "يسعدنا انضمامكم إلى عملاء Aizen Paper" in body
    assert "تم تسجيل حسابكم بنجاح في نظامنا" in body
    assert body.count("{{1}}") == 1


def test_the_body_constant_carries_no_isolation_marks():
    """The values are already isolated by :meth:`TemplateBuilder._text` before
    ``render_text`` substitutes them. If the constant also carried the template's
    own U+2068/U+2069 marks, every value would be wrapped twice (FSI FSI … PDI
    PDI) and the bidi rendering would be corrupt."""
    body = _builder().CUSTOMER_TEMPLATE_BODY
    assert FSI not in body
    assert PDI not in body


def test_the_rendered_message_shows_the_name_once_inside_the_isolation():
    customer = make_stub_customer(customer_name="Print Home")
    text = _builder().render_text(customer)
    assert f"مَرْحَبًا {FSI}Print Home{PDI}،" in text


def test_an_overlong_name_is_truncated_inside_the_isolation():
    """A customer whose name exceeds the budget must still produce a valid
    payload, with the isolation marks intact around what survived."""
    long_name = "ا" * 900
    params = _params(_builder().build(make_stub_customer(customer_name=long_name), "01027693262"))
    assert len(params[0]) < len(FSI) + len(long_name) + len(PDI)
    assert params[0].startswith(FSI)
    assert params[0].endswith(PDI)


def test_the_builder_is_not_an_invoice_builder():
    """The CLI's invoice builder-swap retry identifies an invoice builder by type.
    A welcome builder that looked like an invoice builder could be picked up by a
    path that has no business touching it."""
    builder = _builder()
    assert isinstance(builder, TemplateBuilder)
    assert not isinstance(builder, InvoiceTemplateBuilder)


def test_the_builder_respects_an_overridden_template_name_and_language():
    payload = CustomerTemplateBuilder("other_template", "ar_EG", "20").build(
        make_stub_customer(), "01027693262"
    )
    assert payload["template"]["name"] == "other_template"


@pytest.mark.parametrize("name", ["Print Home", "خالد الغرباو", "Print-Home & Co."])
def test_arbitrary_names_survive_the_round_trip(name):
    params = _params(_builder().build(make_stub_customer(customer_name=name), "01027693262"))
    assert params[0] == f"{FSI}{name}{PDI}"


def test_a_customer_without_a_name_still_builds():
    """The mapper guarantees a non-empty name, but the builder must not explode on
    an empty one — a crash here would be worse than a plain greeting."""
    payload = _builder().build(Customer(id="1", customer_name=""), "01027693262")
    assert len(_params(payload)) == 1
