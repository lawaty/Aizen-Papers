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


@pytest.mark.parametrize("raw", ["1e3", "1E3", "1e+3", "1e-3", "1.5e3", "1e999", "1 e 3"])
def test_money_rejects_exponent_notation_instead_of_mangling_it(mapper, raw):
    """The sanitizer strips every non-digit, so exponent form used to survive as a
    silent change of magnitude: ``"1e3"`` became ``13`` and ``"1e999"`` became
    ``1999``. That is precisely what this function promises never to do, so it
    raises instead."""
    with pytest.raises(ValueError, match="Unparseable money value"):
        mapper.to_invoice({"Invoice": {"id": "1", "no": "000001", "summary_total": raw}})


def test_currency_words_still_strip_so_the_exponent_check_is_not_too_broad(mapper):
    """The exponent guard must not reject the currency spelling that the
    sanitizing regex exists to tolerate."""
    invoice = mapper.to_invoice({"Invoice": {"id": "1", "no": "000001", "summary_total": "EGP 1,250.00"}})
    assert invoice.total == Decimal("1250.00")


def test_unmapped_tax_or_discount_is_reported_rather_than_dropped_silently(caplog, mapper):
    """Tax is deliberately not modelled, so the customer's document would omit it
    with nothing said. The mapper is the only layer that sees the raw payload, so
    the tripwire lives here."""
    payload = {
        "Invoice": {
            "id": "1",
            "no": "000001",
            "summary_total": "1000",
            "summary_tax1": "140",
            "InvoiceItem": [{"item": "Roll", "quantity": "1", "unit_price": "1000", "subtotal": 1000}],
        }
    }
    with caplog.at_level("WARNING", logger="sender.infrastructure.daftra.mapper"):
        mapper.to_invoice(payload)
    assert "summary_tax1" in caplog.text
    assert "000001" in caplog.text


def test_item_level_tax_is_reported_too(caplog, mapper):
    payload = {
        "Invoice": {
            "id": "1",
            "no": "000001",
            "summary_total": "1000",
            "InvoiceItem": [{"item": "Roll", "quantity": "1", "unit_price": "1000", "subtotal": 1000, "tax1": 140}],
        }
    }
    with caplog.at_level("WARNING", logger="sender.infrastructure.daftra.mapper"):
        mapper.to_invoice(payload)
    assert "InvoiceItem.tax1" in caplog.text


def test_a_null_tax_does_not_warn(caplog, mapper):
    """Daftra's own idiom for 'no tax' is ``null``, which must stay quiet — this is
    what every live account sends today."""
    payload = {
        "Invoice": {
            "id": "1",
            "no": "000001",
            "summary_total": "1000",
            "summary_tax1": None,
            "InvoiceItem": [{"item": "Roll", "quantity": "1", "unit_price": "1000", "subtotal": 1000, "tax1": None}],
        }
    }
    with caplog.at_level("WARNING", logger="sender.infrastructure.daftra.mapper"):
        mapper.to_invoice(payload)
    assert "does not model" not in caplog.text


def test_an_item_missing_money_is_reported_but_still_maps(caplog, mapper):
    """Absent warns rather than raises: Daftra uses ``null`` for not-applicable
    money elsewhere, and a raise here is a bare ``ValueError``, which the poller
    classifies permanent — retiring a real invoice on an unfamiliar shape. The
    defaults still apply so the document renders."""
    payload = {
        "Invoice": {
            "id": "1",
            "no": "000001",
            "InvoiceItem": [{"item": "Service call"}],
        }
    }
    with caplog.at_level("WARNING", logger="sender.infrastructure.daftra.mapper"):
        invoice = mapper.to_invoice(payload)
    assert "quantity" in caplog.text
    item = invoice.items[0]
    assert (item.name, item.quantity, item.unit_price, item.total) == ("Service call", Decimal("1"), Decimal("0"), Decimal("0"))


def test_a_complete_item_does_not_warn(caplog, mapper):
    payload = {
        "Invoice": {
            "id": "1",
            "no": "000001",
            "InvoiceItem": [{"item": "Roll", "quantity": "2", "unit_price": "500", "subtotal": 1000}],
        }
    }
    with caplog.at_level("WARNING", logger="sender.infrastructure.daftra.mapper"):
        mapper.to_invoice(payload)
    assert "carries no" not in caplog.text


def test_an_unparseable_date_is_reported(caplog, mapper):
    """Both an absent and an unparseable date render as ``None`` -> "N/A" on the
    PDF, so the two are indistinguishable downstream unless the mapper says so."""
    with caplog.at_level("WARNING", logger="sender.infrastructure.daftra.mapper"):
        invoice = mapper.to_invoice({"Invoice": {"id": "1", "no": "000001", "date": "last Tuesday"}})
    assert invoice.issue_date is None
    assert "unparseable invoice date" in caplog.text


def test_a_zero_deposit_or_discount_stays_quiet(caplog, mapper):
    """Daftra sends ``deposit: "0"`` and ``summary_discount: 0`` on invoices that
    carry neither. A truthiness test calls both present — a *string* zero is
    truthy — and warns on essentially the whole ledger, which is the fastest way
    to make an operator ignore the warning that matters."""
    payload = {
        "Invoice": {
            "id": "1",
            "no": "000001",
            "summary_total": "1000",
            "deposit": "0",
            "summary_discount": 0,
            "InvoiceItem": [{"item": "Roll", "quantity": "1", "unit_price": "1000", "subtotal": 1000, "discount": "0.00"}],
        }
    }
    with caplog.at_level("WARNING", logger="sender.infrastructure.daftra.mapper"):
        mapper.to_invoice(payload)
    assert "does not model" not in caplog.text


def test_an_unparseable_tax_amount_is_treated_as_present(caplog, mapper):
    """Unparseable is not zero. It must be reported rather than silently skipped —
    and it must not raise, because that would abandon an invoice over a field that
    never reaches the document."""
    payload = {"Invoice": {"id": "1", "no": "000001", "summary_tax1": "n/a"}}
    with caplog.at_level("WARNING", logger="sender.infrastructure.daftra.mapper"):
        invoice = mapper.to_invoice(payload)
    assert invoice.number == "000001"
    assert "summary_tax1" in caplog.text


def test_an_absent_date_stays_quiet(caplog, mapper):
    with caplog.at_level("WARNING", logger="sender.infrastructure.daftra.mapper"):
        mapper.to_invoice({"Invoice": {"id": "1", "no": "000001"}})
    assert "unparseable invoice date" not in caplog.text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("١٢٥٠٫٠٠", Decimal("1250.00")),  # Arabic decimal separator
        ("12٫50", Decimal("12.50")),
        ("١٢٣,٤٥٦", Decimal("123456")),  # Arabic-Indic digits, ASCII thousands
        ("١٢٥٠.٠٠", Decimal("1250.00")),  # Arabic-Indic digits, ASCII dot
        ("−1,250.00", Decimal("-1250.00")),  # Unicode minus on a credit/refund
        ("۱٬۲۵۰٫۵۰", Decimal("1250.50")),  # Persian digits + localized separators
    ],
)
def test_money_parses_localized_arabic_and_persian_amounts(mapper, raw, expected):
    """A proxy/localization must not change an amount's magnitude: Arabic decimal
    separators, Arabic-Indic digits and the Unicode minus all fold to ASCII."""
    invoice = mapper.to_invoice({"Invoice": {"id": "1", "no": "000001", "summary_total": raw}})
    assert invoice.total == expected


def test_money_rejects_non_scalar_values(mapper):
    with pytest.raises(ValueError, match="Unparseable money value"):
        mapper.to_invoice({"Invoice": {"id": "1", "no": "000001", "summary_total": [1, 2]}})


def test_money_treats_whitespace_only_as_empty(mapper):
    assert mapper.to_invoice({"Invoice": {"id": "1", "no": "000001", "summary_total": "   "}}).total == Decimal("0")


def test_money_still_accepts_json_numbers_and_empty_values(mapper):
    assert mapper.to_invoice({"Invoice": {"id": "1", "no": "000001", "summary_total": 35000}}).total == Decimal("35000")
    assert mapper.to_invoice({"Invoice": {"id": "1", "no": "000001"}}).total == Decimal("0")


def test_to_invoices_skips_a_bad_row_without_losing_the_rest(caplog):
    """One unparseable invoice must not take the tenant's whole cycle down, and
    the warning must name the invoice so the operator can find it."""
    rows = [
        {"Invoice": {"id": "1", "no": "000001", "summary_total": 100}},
        {"Invoice": {"id": "2", "no": "000002", "summary_total": "not-money"}},
        {"Invoice": {"id": "3", "no": "000003", "summary_total": 300}},
    ]
    with caplog.at_level("WARNING"):
        invoices = DaftraInvoiceMapper().to_invoices({"data": rows})
    assert [invoice.id for invoice in invoices] == ["1", "3"]
    assert any("could not be normalized" in record.message for record in caplog.records)
    assert any("invoice 000002" in record.message for record in caplog.records)
