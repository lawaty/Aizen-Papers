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
    assert invoice.customer_phones == ("201022322634",)


def _detail_response(client_overrides: dict) -> dict:
    """REAL_INVOICE_RESPONSE with its ``Client`` adjusted.

    Built on the captured detail payload rather than written from scratch, so
    these tests keep the real envelope, nesting and field names — the parts that
    decide whether ``phone2`` is read at all.
    """
    import copy

    payload = copy.deepcopy(REAL_INVOICE_RESPONSE)
    payload["data"]["Invoice"]["Client"].update(client_overrides)
    return payload


def test_the_detail_endpoint_parses_both_phone_fields_as_two_recipients(mapper):
    """The second phone on ``GET /invoices/{id}.json`` is a real recipient.

    This is the test that makes ``phone2`` load-bearing. The captured payload
    happens to carry the same number in both fields, so an implementation that
    read only ``phone1`` would still satisfy
    ``test_nested_client_supplies_the_phone`` — the gap this closes. Mutating the
    mapper to ignore ``phone2`` must fail here.
    """
    invoice = mapper.to_invoice(_detail_response({"phone2": "+201155566677"}))

    assert invoice.customer_phones == ("201155566677", "201022322634")
    # phone2 leads: it is the field the ERP's own ordering prefers, and it decides
    # which number the report lists first.
    assert invoice.customer_phones[0] == "201155566677"


def test_the_detail_endpoint_reads_the_second_phone_when_the_first_is_empty(mapper):
    """Order of *reading* must not become order of *accepting*.

    A customer who filled only ``phone2`` is the whole reason the field is
    collected: an implementation that stopped at the first field present, or
    preferred ``phone1``, would leave this customer unreachable.
    """
    invoice = mapper.to_invoice(_detail_response({"phone1": "", "phone2": "+201155566677"}))

    assert invoice.customer_phones == ("201155566677",)


def test_the_detail_endpoint_reads_the_first_phone_when_the_second_is_empty(mapper):
    invoice = mapper.to_invoice(_detail_response({"phone1": "+201022322634", "phone2": ""}))

    assert invoice.customer_phones == ("201022322634",)


def test_the_real_capture_collapses_to_one_recipient_because_both_fields_agree(mapper):
    """What the live account actually returns today: ``phone1 == phone2``.

    Two fields holding one number must be one recipient, or every existing
    customer would receive each invoice twice.
    """
    invoice = mapper.to_invoice(REAL_INVOICE_RESPONSE)

    assert invoice.customer_phones == ("201022322634",)


def test_two_written_forms_of_one_number_collapse_on_the_detail_endpoint(mapper):
    """Deduplication happens after normalization, which is the only way to tell
    two entries for the same subscriber apart."""
    invoice = mapper.to_invoice(_detail_response({"phone1": "01027693262", "phone2": "+201027693262"}))

    assert invoice.customer_phones == ("201027693262",)


def test_an_unusable_second_phone_is_dropped_and_the_first_still_goes_out(mapper, caplog):
    """One mistyped field must not cost the customer the invoice.

    Filling both fields and mistyping one is exactly the case this protects; the
    warning names the field so an operator can fix the record.
    """
    with caplog.at_level("WARNING", logger="sender.infrastructure.daftra.mapper"):
        invoice = mapper.to_invoice(_detail_response({"phone2": "12345"}))

    assert invoice.customer_phones == ("201022322634",)
    assert "phone2" in caplog.text


def test_the_flattened_client_phone_fields_are_both_read_too(mapper):
    """Some payloads flatten the client onto the invoice.

    The flattened spelling is a *fallback source* of the same fields, so it has
    to yield the same two recipients — otherwise the fan-out silently depends on
    which endpoint answered.
    """
    invoice = mapper.to_invoice(
        {
            "Invoice": {
                "id": "1",
                "no": "000001",
                "client_business_name": "Acme",
                "client_phone1": "01027693262",
                "client_phone2": "01115556677",
                "InvoiceItem": [],
            }
        }
    )

    assert invoice.customer_phones == ("201115556677", "201027693262")


def test_nested_invoice_items_are_not_dropped(mapper):
    invoice = mapper.to_invoice(REAL_INVOICE_RESPONSE)
    assert len(invoice.items) == 1
    item = invoice.items[0]
    assert item.name == "دوبلكس فايج 300جم بكر 70سم"
    assert item.quantity == Decimal("1")
    assert item.unit_price == Decimal("35000")
    assert item.total == Decimal("35000")


def _with_invoice_fields(**fields) -> dict:
    """REAL_INVOICE_RESPONSE with keys added to the ``Invoice`` node.

    Built on the captured payload for the same reason as :func:`_detail_response`:
    the real envelope, nesting and field names are the parts that decide whether a
    key is read at all, and a hand-written row would quietly stop testing that.
    """
    import copy

    payload = copy.deepcopy(REAL_INVOICE_RESPONSE)
    payload["data"]["Invoice"].update(fields)
    return payload


def _with_item_fields(**fields) -> dict:
    """REAL_INVOICE_RESPONSE with keys added to its single ``InvoiceItem``.

    The captured row already carries ``description: ""``, so a test that wants to
    see a description written has to fill it — which is also the point: the empty
    string Daftra sends on most rows and a sentence an operator typed arrive over
    the same key, and both have to reach the model.
    """
    import copy

    payload = copy.deepcopy(REAL_INVOICE_RESPONSE)
    payload["data"]["Invoice"]["InvoiceItem"][0].update(fields)
    return payload


def test_the_captured_items_empty_description_is_an_empty_string(mapper):
    """The captured row says ``description: ""``, and that has to stay empty.

    Not a cosmetic assertion: Daftra sends the key on every line whether or not
    anyone wrote anything, so "the key is there" and "the seller wrote something"
    are different facts, and only the second may put words on the customer's PDF.
    """
    assert mapper.to_invoice(REAL_INVOICE_RESPONSE).items[0].description == ""


def test_a_written_line_description_is_mapped(mapper):
    """``InvoiceItem.description`` is the line's own prose and reaches the model.

    The key is not the name: the captured row carries both ``item`` (the product)
    and ``description``, and a mapper that read the wrong one would either print
    the product twice or drop what the operator wrote about the line.
    """
    invoice = mapper.to_invoice(_with_item_fields(description="وزن 300 جرام وعرض 70 سم"))
    assert invoice.items[0].description == "وزن 300 جرام وعرض 70 سم"
    # The name is untouched by it: the two are different fields, not one.
    assert invoice.items[0].name == "دوبلكس فايج 300جم بكر 70سم"


def test_a_line_with_no_description_key_at_all_is_empty_not_a_placeholder(mapper):
    """Absent and empty are the same answer, and neither is ``"-"``.

    A placeholder would print a dash under every line of every invoice an account
    that never fills descriptions in sends, which is the overwhelming majority.
    """
    import copy

    payload = copy.deepcopy(REAL_INVOICE_RESPONSE)
    del payload["data"]["Invoice"]["InvoiceItem"][0]["description"]
    assert mapper.to_invoice(payload).items[0].description == ""


def test_the_invoice_notes_are_mapped(mapper):
    """``html_notes`` is the free text about the invoice as a whole.

    The field the operator's rich-text box writes, and the only one of Daftra's
    three that this client ever fills.
    """
    invoice = mapper.to_invoice(
        _with_invoice_fields(html_notes="التوصيل خلال أسبوع داخل القاهرة")
    )
    assert invoice.description == "التوصيل خلال أسبوع داخل القاهرة"


def test_an_invoice_with_no_notes_is_empty(mapper):
    """The captured payload carries no ``notes``, and that is the ordinary case."""
    assert mapper.to_invoice(REAL_INVOICE_RESPONSE).description == ""


def test_the_empty_note_columns_are_not_read_at_all(mapper):
    """``notes`` and ``description`` are empty on every invoice, so they are not read.

    These three tests used to assert that a note under ``notes`` or
    ``description`` was still found. That behaviour is deliberately gone: neither
    column is ever filled by this client, so reading them was a claim about
    hypothetical other shapes, and — the half that actually cost something —
    ``notes`` being tried *first* is why every real invoice rendered with no notes
    at all until ``html_notes`` was found.
    """
    assert mapper.to_invoice(_with_invoice_fields(description="Payment within 14 days")).description == ""
    assert mapper.to_invoice(_with_invoice_fields(notes="", description="x")).description == ""
    assert mapper.to_invoice(_with_invoice_fields(notes="ملاحظات")).description == ""


#: The operator's notes as Daftra actually stores them, captured from
#: ``GET /invoices/82.json`` on the account whose invoices carry them. This is the
#: payload that made the plain ``notes`` key look permanently empty: ``notes`` is
#: ``null`` on that same invoice and the text lives here, wrapped in the rich-text
#: editor's own markup.
REAL_HTML_NOTES = (
    '<p>الوزن القائم = 411.8<span style="color: rgb(0, 0, 0); -webkit-tap-highlight-color: '
    'transparent;">&nbsp;</span><span style="-webkit-tap-highlight-color: transparent;">'
    'كيلو</span></p><p>الوزن صافي = 397.319<span style="color: rgb(0, 0, 0); '
    '-webkit-tap-highlight-color: transparent;">&nbsp;</span><span '
    'style="-webkit-tap-highlight-color: transparent;">كيلو</span></p>'
    "<p>سعر الكيلو = 188 ج</p><p>الاجمالي = 397.319&nbsp;<span style=\"-webkit-tap-"
    'highlight-color: transparent;">* 188 =&nbsp;</span><span style="-webkit-tap-'
    'highlight-color: transparent;"><span style="color: rgb(58, 62, 99); '
    'background-color: rgb(248, 249, 250);">74,695.94</span>&nbsp;ج</span></p>'
    '<span class="replace_template_notes"></span>'
)


def test_notes_are_read_from_the_rich_text_field_they_are_actually_written_to():
    """Daftra stores the operator's notes in ``html_notes``, not ``notes``.

    Measured across all three configured accounts: ``notes`` was empty on all 68
    invoices while ``html_notes`` was populated on 22. An account whose notes are
    read from ``notes`` therefore looks exactly like an operator who never types
    one, and nothing anywhere says otherwise — which is how this went unnoticed.
    """
    invoice = DaftraInvoiceMapper().to_invoice({
        "Invoice": {"id": "82", "no": "14B", "notes": None, "html_notes": REAL_HTML_NOTES}
    })
    assert invoice.description
    assert "الوزن القائم" in invoice.description
    assert "411.8" in invoice.description


def test_notes_come_out_as_one_line_per_block_the_operator_typed():
    """The ``<p>`` elements are the operator's line breaks, and they survive.

    They enter a weighable invoice's figures one under another, and the PDF draws
    one line per break — so the four paragraphs must arrive as four lines, not as
    one run of prose that has to be re-read to tell the figures apart.
    """
    invoice = DaftraInvoiceMapper().to_invoice({
        "Invoice": {"id": "82", "no": "14B", "html_notes": REAL_HTML_NOTES}
    })
    lines = invoice.description.split("\n")
    assert lines == [
        "الوزن القائم = 411.8 كيلو",
        "الوزن صافي = 397.319 كيلو",
        "سعر الكيلو = 188 ج",
        "الاجمالي = 397.319 * 188 = 74,695.94 ج",
    ]


def test_no_editor_markup_reaches_the_customer_document():
    """Not a tag, not an entity, and not the editor's placeholder span.

    The value read raw would put ``<p>الوزن صافي = 397.319<span style="color:
    rgb(0, 0, 0); ...">`` on an invoice. ``replace_template_notes`` is an empty
    placeholder Daftra appends after the operator's own text, so it contributes
    nothing visible — but it is skipped explicitly rather than relied upon to be
    empty.
    """
    description = DaftraInvoiceMapper().to_invoice({
        "Invoice": {"id": "82", "no": "14B", "html_notes": REAL_HTML_NOTES}
    }).description
    for junk in ("<p>", "<span", "</p>", "&nbsp;", "style=", "replace_template_notes"):
        assert junk not in description, f"{junk} reached the document"


def test_only_html_notes_is_read_as_the_notes():
    """The empty columns are not alternate spellings, so they are not fallbacks.

    ``notes`` and ``description`` exist on the payload and are empty on all 68
    invoices across the three configured accounts; the operator's rich-text box
    writes ``html_notes``. Reading the empty ones as fallbacks would be a claim
    about another shape, made without evidence — and it is what kept the notes
    hidden on every real invoice for as long as it stood, because the first key
    tried was the one the client never fills.
    """
    # The fields the client does not use are ignored even when non-empty, so a
    # value appearing there by some other route cannot be mistaken for a note.
    assert DaftraInvoiceMapper().to_invoice({
        "Invoice": {"id": "1", "no": "1", "html_notes": None, "notes": "not a note",
                    "description": "nor this"}
    }).description == ""
    assert DaftraInvoiceMapper().to_invoice({
        "Invoice": {"id": "1", "no": "1", "html_notes": "", "notes": "x", "description": "y"}
    }).description == ""
    # And the field the client does use is unaffected.
    assert DaftraInvoiceMapper().to_invoice({
        "Invoice": {"id": "1", "no": "1", "html_notes": "<p>الوزن القائم = 10</p>",
                    "notes": None}
    }).description == "الوزن القائم = 10"


def test_line_breaks_in_a_plain_note_are_kept_too():
    """The reader does not only understand ``<p>`` — a typed newline is a break.

    An operator who types into a plain-text box across several lines means the
    same thing as one who presses return in the rich-text editor.
    """
    invoice = DaftraInvoiceMapper().to_invoice({
        "Invoice": {"id": "1", "no": "1", "html_notes": "أولا\nثانيا\nثالثا"}
    })
    assert invoice.description.split("\n") == ["أولا", "ثانيا", "ثالثا"]


def test_malformed_notes_markup_never_raises():
    """A note is never worth retiring an invoice over.

    ``ValueError`` reaching the poller is classified permanent, which would
    retire a real invoice over the one field the customer asked to be shown. So
    broken markup degrades to whatever text could be recovered.
    """
    assert DaftraInvoiceMapper().to_invoice({
        "Invoice": {"id": "1", "no": "1", "html_notes": "<p>تسليم <b>صباحا</p>"}
    }).description


def test_offline_stub_payload_matches_the_real_shape(mapper):
    """The stub is only useful as a double if it nests Client/InvoiceItem the
    way Daftra does; a flat stub would hide the nesting bug above."""
    from sender.presentation.stubs import StubInvoiceSource, default_stub_invoices

    raw = StubInvoiceSource(default_stub_invoices()).get_raw_invoice("1")
    assert "Client" in raw["data"]["Invoice"]
    assert raw["data"]["Invoice"]["InvoiceItem"]

    invoice = mapper.to_invoice(raw)
    assert invoice.customer_name == "Ahmed Hassan"
    assert invoice.customer_phones == ("201027693262",)
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


