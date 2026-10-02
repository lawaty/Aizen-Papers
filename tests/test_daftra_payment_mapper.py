"""Wire-format specs for ``/invoice_payments`` → :class:`Payment`.

Mirrors ``test_daftra_mapper.py``. The captured row is a real response from a
live tenant, so these tests fail loudly if Daftra's shape ever moves.
"""

from datetime import date
from decimal import Decimal

import pytest

from sender.infrastructure.daftra.client import DaftraClient
from sender.infrastructure.daftra.mapper import DaftraPaymentMapper, _row_label

#: Verbatim from `GET /invoice_payments.json` on a live account. Note the two
#: things that shape the whole design: ``amount`` arrives as a string with four
#: decimals (a tax split), and the payer fields are empty because the payer only
#: exists on the linked invoice.
REAL_PAYMENT_ROW = {
    "InvoicePayment": {
        "id": "116",
        "invoice_id": "39",
        "client_id": None,
        "payment_method": "cash",
        "amount": "32999.9961",
        "transaction_id": "",
        "date": "2026-05-02 00:00:00",
        "email": None,
        "status": "1",
        "notes": None,
        "created": "2026-05-02 12:31:01",
        "modified": "2026-05-02 12:31:01",
        "added_by": "1",
        "currency_code": "EGP",
        "first_name": "",
        "last_name": "",
        "city": "ويش",
        "country_code": "EG",
        "phone1": "",
        "phone2": "",
        "transaction_type": "",
        "processed": None,
        "attachment": None,
        "staff_id": "0",
        "receipt_notes": None,
        "treasury_id": "1",
        "pos_shift_id": None,
        "branch_id": "1",
        "extra_details": '{"paymentSetting":{"id":"1"}}',
        "source": None,
        "code": "000116",
    }
}

#: The linked invoice, trimmed to what the mapper reads.
REAL_INVOICE = {
    "data": {
        "Invoice": {
            "id": "39",
            "no": "000039",
            "client_business_name": "أ/ علي تويج",
            "currency_code": "EGP",
            "date": "02/05/2026",
            "Client": {
                "id": "6",
                "business_name": "أ/ علي تويج",
                "first_name": "",
                "last_name": "",
                "phone1": "+201022322634",
                "phone2": "",
            },
        }
    }
}


@pytest.fixture
def mapper() -> DaftraPaymentMapper:
    return DaftraPaymentMapper(country_code="20")


def test_the_real_row_maps_onto_the_model(mapper: DaftraPaymentMapper):
    payment = mapper.to_payment(REAL_PAYMENT_ROW)
    assert payment.id == "116"
    # ``code`` is the receipt reference the template prints, not the internal id.
    assert payment.number == "000116"
    assert payment.amount == Decimal("32999.9961")
    assert payment.payment_date == date(2026, 5, 2)
    assert payment.currency == "EGP"
    assert payment.status == "1"
    assert payment.payment_method == "cash"
    assert payment.invoice_id == "39"


def test_the_amount_survives_daftras_extra_decimals(mapper: DaftraPaymentMapper):
    """Daftra sends a float split across tax lines; the message must show the
    rounded amount, and nothing may silently truncate the fraction away."""
    payment = mapper.to_payment(REAL_PAYMENT_ROW)
    assert f"{payment.amount:,.2f}" == "33,000.00"


def test_the_a_datetime_date_is_parsed(mapper: DaftraPaymentMapper):
    assert mapper.to_payment(REAL_PAYMENT_ROW).payment_date == date(2026, 5, 2)


def test_the_payer_is_absent_until_the_linked_invoice_is_read(mapper: DaftraPaymentMapper):
    """The single most important fact about this source: the payment row names
    nobody, so an unresolved payment has no reachable customer."""
    payment = mapper.to_payment(REAL_PAYMENT_ROW)
    assert payment.customer_phone is None
    assert payment.invoice_id == "39"


def test_the_linked_invoice_supplies_the_payer(mapper: DaftraPaymentMapper):
    payment = mapper.to_payment(REAL_PAYMENT_ROW, REAL_INVOICE)
    assert payment.customer_name == "أ/ علي تويج"
    assert payment.customer_phone == "201022322634"


def test_a_payment_without_an_invoice_is_still_a_valid_payment(mapper: DaftraPaymentMapper):
    row = {"InvoicePayment": {"id": "7", "amount": "10", "code": "000007"}}
    payment = mapper.to_payment(row)
    assert payment.id == "7"
    assert payment.invoice_id is None
    assert payment.customer_phone is None


def test_the_code_falls_back_to_the_id_when_absent(mapper: DaftraPaymentMapper):
    """Never send a blank identifier: the template prints it."""
    payment = mapper.to_payment({"InvoicePayment": {"id": "7", "amount": "10"}})
    assert payment.number == "7"


def test_a_payment_with_no_code_or_id_is_rejected(mapper: DaftraPaymentMapper):
    with pytest.raises(ValueError):
        mapper.to_payment({"InvoicePayment": {"amount": "10"}})


def test_the_detail_envelope_is_unwrapped(mapper: DaftraPaymentMapper):
    """A single record arrives as ``{"data": {"InvoicePayment": {...}}}``."""
    detail = {"result": "successful", "code": 200, "data": REAL_PAYMENT_ROW["InvoicePayment"]}
    assert mapper.to_payment(detail).id == "116"


def test_a_flat_row_is_accepted(mapper: DaftraPaymentMapper):
    assert mapper.to_payment(REAL_PAYMENT_ROW["InvoicePayment"]).id == "116"


def test_to_payments_skips_a_bad_row_without_losing_the_rest(mapper: DaftraPaymentMapper):
    payments = mapper.to_payments(
        {
            "data": [
                REAL_PAYMENT_ROW,
                {"InvoicePayment": {"amount": "not-a-number"}},
                {"InvoicePayment": {"id": "117", "amount": "5", "code": "000117"}},
            ]
        }
    )
    assert [p.id for p in payments] == ["116", "117"]


def test_an_empty_listing_is_an_empty_list(mapper: DaftraPaymentMapper):
    assert mapper.to_payments({"data": []}) == []
    assert mapper.to_payments({}) == []


def test_the_row_label_names_the_payment_for_the_operator():
    assert _row_label(REAL_PAYMENT_ROW) == "payment 000116"
    assert _row_label({"InvoicePayment": {"amount": "x"}}) == "a payment"
    # The invoice wording is unchanged, so its existing warnings still read right.
    assert _row_label({"Invoice": {"no": "000002"}}) == "invoice 000002"
    assert _row_label({"Invoice": {}}) == "an invoice"


def test_a_localized_amount_is_parsed(mapper: DaftraPaymentMapper):
    """Same rule as invoices: an Arabic-Indic amount must not change magnitude."""
    assert mapper.to_payment(
        {"InvoicePayment": {"id": "1", "amount": "١٢٥٠٫٠٠"}}
    ).amount == Decimal("1250.00")


def test_an_unparseable_amount_raises_rather_than_becoming_zero(mapper: DaftraPaymentMapper):
    """A silent ``0.00`` on a customer-facing message is the worst failure this
    mapper can have, so a present-but-unparseable amount fails loudly."""
    with pytest.raises(ValueError):
        mapper.to_payment({"InvoicePayment": {"id": "1", "amount": "n/a"}})


class _PaymentSession:
    """Answers the two-hop payment fetch and records which URLs were hit."""

    def __init__(self, payment: dict = None, invoice: dict | None = None) -> None:
        self._payment = payment if payment is not None else REAL_PAYMENT_ROW["InvoicePayment"]
        self._invoice = invoice
        self.headers: dict = {}
        self.urls: list[str] = []
        self.params = None

    def get(self, url: str, **kwargs):
        from fakes import FakeResponse

        self.urls.append(url)
        self.params = kwargs.get("params")
        if "/invoice_payments/" in url:
            return FakeResponse(200, {"result": "successful", "data": {"InvoicePayment": self._payment}})
        return FakeResponse(200, self._invoice)


def test_the_client_resolves_the_payer_with_two_requests():
    """The cost of reaching a payment's customer, made explicit: one request for
    the payment, one for the invoice that names the payer."""
    session = _PaymentSession(REAL_PAYMENT_ROW["InvoicePayment"], REAL_INVOICE)
    client = DaftraClient(
        api_key="k", session=session, country_code="20", payments_status="1"
    )
    payment = client.get_payment("116")
    assert payment.customer_phone == "201022322634"
    assert [url.split("/api2/")[-1] for url in session.urls] == [
        "invoice_payments/116.json",
        "invoices/39.json",
    ]


def test_the_client_sends_the_status_filter_to_daftra():
    from fakes import FakeResponse

    class _Listing(_PaymentSession):
        def get(self, url: str, **kwargs):
            self.urls.append(url)
            self.params = kwargs.get("params")
            return FakeResponse(200, {"data": [REAL_PAYMENT_ROW]})

    session = _Listing()
    client = DaftraClient(api_key="k", session=session, payments_status="1")
    payments = client.list_payments(limit=5, page=2)
    assert session.params == {"page": 2, "limit": 5, "status": "1"}
    assert [p.id for p in payments] == ["116"]


def test_no_status_filter_sends_no_status_parameter():
    from fakes import FakeResponse

    class _Listing(_PaymentSession):
        def get(self, url: str, **kwargs):
            self.params = kwargs.get("params")
            return FakeResponse(200, {"data": []})

    session = _Listing()
    client = DaftraClient(api_key="k", session=session, payments_status=None)
    client.list_payments(limit=5)
    assert "status" not in session.params


def test_a_non_numeric_payment_id_is_rejected_before_any_request():
    client = DaftraClient(api_key="k", session=_PaymentSession())
    with pytest.raises(ValueError):
        client.get_payment("../etc/passwd")