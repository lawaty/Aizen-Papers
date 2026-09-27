"""Mapper contract checks against the payload shape Daftra actually returns.

Daftra nests Client/InvoiceItem *inside* the Invoice object, so these tests
pin that nesting down; the flat shape is only accepted for compatibility.
"""

from decimal import Decimal

import pytest

from sender.infrastructure.daftra.mapper import DaftraInvoiceMapper

# Captured from GET /invoices/1.json on the live account, trimmed to the fields
# the mapper reads. Field names, value types and nesting are unchanged.
REAL_INVOICE_RESPONSE = {
    "result": "successful",
    "code": 200,
    "data": {
        "Invoice": {
            "id": "1",
            "no": "000001",
            "draft": "0",
            "payment_status": 0,
            "summary_subtotal": 35000,
            "summary_total": 35000,
            "summary_paid": 0,
            "summary_unpaid": 35000,
            "currency_code": "EGP",
            "date": "25/09/2026",
            "client_id": "1",
            "client_business_name": "Print Home",
            "client_first_name": "",
            "client_last_name": "",
            "client_country_code": "EG",
            "invoice_html_url": "https://aizenpaper.daftra.com/invoices/preview/1?hash=0846864e66",
            "invoice_pdf_url": "https://aizenpaper.daftra.com/invoices/view/1.pdf?hash=0846864e66",
            "Client": {
                "id": "1",
                "client_number": "000001",
                "business_name": "Print Home",
                "first_name": "",
                "last_name": "",
                "phone1": "+201022322634",
                "phone2": "+201022322634",
                "country_code": "EG",
                "default_currency_code": "EGP",
                "type": "3",
            },
            "InvoiceItem": [
                {
                    "id": "1",
                    "item": "دوبلكس فايج 300جم بكر 70سم",
                    "description": "",
                    "quantity": "1",
                    "unit_price": "35000",
                    "subtotal": 35000,
                    "item_subtotal": 35000,
                    "site_id": "5059700",
                    "tax1": None,
                    "tax2": None,
                    "Product": {
                        "id": "8",
                        "name": "دوبلكس فايج 300جم بكر 70سم",
                        "unit_price": "35000",
                        "product_code": "000008",
                        "brand": "فايج",
                    },
                }
            ],
            "InvoicePayment": [],
            "InvoiceTax": [],
        }
    },
}


@pytest.fixture
def mapper() -> DaftraInvoiceMapper:
    return DaftraInvoiceMapper()


def test_nested_client_supplies_the_phone(mapper):
    invoice = mapper.to_invoice(REAL_INVOICE_RESPONSE)
    assert invoice.customer_phone == "201022322634"


def test_nested_invoice_items_are_not_dropped(mapper):
    invoice = mapper.to_invoice(REAL_INVOICE_RESPONSE)
    assert len(invoice.items) == 1
    item = invoice.items[0]
    assert item.name == "دوبلكس فايج 300جم بكر 70سم"
    assert item.quantity == Decimal("1")
    assert item.unit_price == Decimal("35000")
    assert item.total == Decimal("35000")


def test_invoice_header_fields_map_from_the_real_payload(mapper):
    invoice = mapper.to_invoice(REAL_INVOICE_RESPONSE)
    assert invoice.id == "1"
    assert invoice.number == "000001"
    assert invoice.customer_name == "Print Home"
    assert invoice.status == "Unpaid"
    assert invoice.currency == "EGP"
    assert invoice.total == Decimal("35000")
    assert invoice.balance_due == Decimal("35000")
    assert invoice.issue_date.isoformat() == "2026-09-25"
    assert invoice.public_url.endswith("/invoices/preview/1?hash=0846864e66")


def test_flat_payload_shape_is_still_accepted(mapper):
    """Older payloads and the offline stub put Client/InvoiceItem beside Invoice."""
    invoice = mapper.to_invoice(
        {
            "Invoice": {"id": "1", "no": "000001", "summary_total": 100},
            "Client": {"business_name": "Acme", "phone1": "01027693262"},
            "InvoiceItem": [{"item": "Paper", "quantity": "2", "unit_price": "50", "subtotal": 100}],
        }
    )
    assert invoice.customer_name == "Acme"
    assert invoice.customer_phone == "201027693262"
    assert [item.name for item in invoice.items] == ["Paper"]


def test_nested_client_wins_over_a_flat_duplicate(mapper):
    invoice = mapper.to_invoice(
        {
            "Invoice": {
                "id": "1",
                "no": "000001",
                "Client": {"business_name": "Nested", "phone1": "01027693262"},
                "InvoiceItem": [],
            },
            "Client": {"business_name": "Flat", "phone1": "01234567890"},
            "InvoiceItem": [{"item": "Flat item", "subtotal": 5}],
        }
    )
    assert invoice.customer_name == "Nested"
    assert invoice.customer_phone == "201027693262"
    assert invoice.items == ()


def test_nested_empty_item_list_does_not_fall_back_to_the_flat_list(mapper):
    invoice = mapper.to_invoice(
        {
            "Invoice": {"id": "1", "no": "000001", "InvoiceItem": []},
            "InvoiceItem": [{"item": "Flat item", "subtotal": 5}],
        }
    )
    assert invoice.items == ()


def test_list_rows_map_each_nested_invoice(mapper):
    invoices = mapper.to_invoices(
        {"result": "successful", "data": [REAL_INVOICE_RESPONSE["data"], {"Invoice": {"id": "2", "no": "000002"}}]}
    )
    assert [invoice.id for invoice in invoices] == ["1", "2"]
    assert invoices[0].customer_phone == "201022322634"
    assert len(invoices[0].items) == 1


def test_name_falls_back_to_person_then_placeholder(mapper):
    invoice = mapper.to_invoice(
        {"Invoice": {"id": "1", "no": "000001", "client_first_name": "Mona", "client_last_name": "Farouk"}}
    )
    assert invoice.customer_name == "Mona Farouk"
    assert mapper.to_invoice({"Invoice": {"id": "1", "no": "000001"}}).customer_name == "Customer"


def test_malformed_payloads_are_rejected(mapper):
    with pytest.raises(ValueError):
        mapper.to_invoice({"data": {"Invoice": "not-an-object"}})
    with pytest.raises(ValueError):
        mapper.to_invoice({"data": {"Unrelated": {}}})


def test_offline_stub_payload_matches_the_real_shape(mapper):
    """The stub is only useful as a double if it nests Client/InvoiceItem the
    way Daftra does; a flat stub would hide the nesting bug above."""
    from sender.presentation.stubs import StubInvoiceSource, default_stub_invoices

    raw = StubInvoiceSource(default_stub_invoices()).get_raw_invoice("1")
    assert "Client" in raw["data"]["Invoice"]
    assert raw["data"]["Invoice"]["InvoiceItem"]

    invoice = mapper.to_invoice(raw)
    assert invoice.customer_name == "Ahmed Hassan"
    assert invoice.customer_phone == "201027693262"
    assert [item.name for item in invoice.items] == ["A4 Paper Ream 80gsm"]
    assert invoice.items[0].total == Decimal("1500.00")


def test_stub_raw_payload_is_json_serializable():
    import json

    from sender.presentation.stubs import StubInvoiceSource, default_stub_invoices

    raw = StubInvoiceSource(default_stub_invoices()).get_raw_invoice("1")
    assert json.loads(json.dumps(raw)) == raw


# --- money parsing (a silent 0.00 on a customer document is the worst failure) ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12,345,678", Decimal("12345678")),
        ("EGP 1,250.00", Decimal("1250.00")),
        ("1 250,00", Decimal("1250.00")),
        ("1,250", Decimal("1250")),
        ("1,99", Decimal("1.99")),
        ("1.234.567", Decimal("1234567")),
        ("-1,250.50", Decimal("-1250.50")),
        ("35000", Decimal("35000")),
        ("1500.00", Decimal("1500.00")),
        ("1.234", Decimal("1.234")),
        ("1,250.00", Decimal("1250.00")),
    ],
)
def test_money_parses_real_world_formatted_amounts(mapper, raw, expected):
    invoice = mapper.to_invoice({"Invoice": {"id": "1", "no": "000001", "summary_total": raw}})
    assert invoice.total == expected


@pytest.mark.parametrize("raw", ["n/a", "abc", "--", "NaN", "12..34", "12,,34", "1234,567"])
def test_money_raises_on_a_present_but_unparseable_amount(mapper, raw):
    with pytest.raises(ValueError, match="Unparseable money value"):
        mapper.to_invoice({"Invoice": {"id": "1", "no": "000001", "summary_total": raw}})


def test_money_still_accepts_json_numbers_and_empty_values(mapper):
    assert mapper.to_invoice({"Invoice": {"id": "1", "no": "000001", "summary_total": 35000}}).total == Decimal("35000")
    assert mapper.to_invoice({"Invoice": {"id": "1", "no": "000001"}}).total == Decimal("0")


def test_to_invoices_skips_a_bad_row_without_losing_the_rest(caplog):
    """One unparseable invoice must not take the tenant's whole cycle down."""
    rows = [
        {"Invoice": {"id": "1", "no": "000001", "summary_total": 100}},
        {"Invoice": {"id": "2", "no": "000002", "summary_total": "not-money"}},
        {"Invoice": {"id": "3", "no": "000003", "summary_total": 300}},
    ]
    with caplog.at_level("WARNING"):
        invoices = DaftraInvoiceMapper().to_invoices({"data": rows})
    assert [invoice.id for invoice in invoices] == ["1", "3"]
    assert any("could not be normalized" in record.message for record in caplog.records)
