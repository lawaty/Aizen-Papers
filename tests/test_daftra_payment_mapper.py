"""Wire-format specs for ``/client_payments`` → :class:`Payment`.

Mirrors ``test_daftra_mapper.py``. The captured row is a real response from
a live tenant, so these tests fail loudly if Daftra's shape ever moves.

A **client payment** is money received into a client's *account* — a deposit, a
prepayment, an opening balance. It is not a payment settling an invoice: the two
are separate Daftra resources with separate endpoints and, on the live account,
no id in common (125 invoice-payment rows against 109 client-payment rows, zero
shared). The approved ``aizen_new_payment`` template is written in exactly these
terms — a payment recorded **على حسابكم**, "on your account", with the account
balance updated — so this is the resource the pipeline announces.
"""

from datetime import date
from decimal import Decimal

import pytest

from sender.infrastructure.daftra.client import DaftraClient
from sender.infrastructure.daftra.mapper import DaftraPaymentMapper, _row_label

#: Verbatim from `GET /client_payments.json` on the live `mohamedsoph2006`
#: account. Note the three things that shape the whole design: ``amount`` arrives
#: as a string with four decimals (a tax split), ``invoice_id`` is **null** on
#: every client payment, and the row names the payer by ``client_id`` while
#: carrying no business name at all — ``first_name``/``last_name`` are empty
#: because this client is a company and its name lives on the ``Client`` record.
REAL_PAYMENT_ROW = {
    "ClientPayment": {
        "id": "244",
        "code": "000244",
        "client_id": "1",
        "invoice_id": None,
        "amount": "100000",
        "date": "2026-09-24 00:00:00",
        "status": "1",
        "currency_code": "EGP",
        "payment_method": "cash",
        "transaction_id": "",
        "transaction_type": "",
        "notes": "احمد الشحات ",
        "receipt_notes": "",
        "created": "2026-09-24 16:14:30",
        "modified": "2026-09-24 16:14:30",
        "added_by": "1",
        "branch_id": "1",
        "treasury_id": "1",
        "pos_shift_id": None,
        "staff_id": "2",
        "first_name": "",
        "last_name": "",
        "phone1": "+201022322634",
        "phone2": "+201022322634",
        "email": None,
        "address1": "",
        "address2": "",
        "city": "المنصورة",
        "postal_code": "",
        "country_code": "EG",
        "processed": None,
        "source": None,
        "state": "شها",
        "attachment": None,
        "ip": "197.35.68.98",
        "response_code": None,
        "response_message": None,
        "payment_work_order_id": None,
        "extra_details": '{"client_balance":1119194.17340384,"paymentSetting":{"id":"1"}}',
    }
}

#: The payer, from `GET /clients/1.json` on the same account. Trimmed to what the
#: mapper reads. This is the record that carries the business name the payment row
#: does not have.
REAL_CLIENT = {
    "data": {
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
        }
    }
}

#: A row whose payer is a person rather than a company, so the name has to come
#: from first+last. Paired below with a matching person ``Client`` record.
PERSON_ROW = {
    "ClientPayment": {
        "id": "7",
        "code": "000007",
        "client_id": "6",
        "amount": "10",
        "status": "1",
        "first_name": "علي",
        "last_name": "تويج",
    }
}

#: The same person as a ``Client`` record: no business name, both names set.
PERSON_CLIENT = {
    "data": {
        "Client": {
            "id": "6",
            "business_name": "",
            "first_name": "علي",
            "last_name": "تويج",
            "phone1": "+201022322634",
            "phone2": "",
        }
    }
}


@pytest.fixture
def mapper() -> DaftraPaymentMapper:
    return DaftraPaymentMapper(country_code="20")


def test_the_real_row_maps_onto_the_model(mapper: DaftraPaymentMapper):
    payment = mapper.to_payment(REAL_PAYMENT_ROW)
    assert payment.id == "244"
    # ``code`` is the receipt reference, not the internal id.
    assert payment.number == "000244"
    assert payment.amount == Decimal("100000")
    assert payment.payment_date == date(2026, 9, 24)
    assert payment.currency == "EGP"
    assert payment.status == "1"
    assert payment.payment_method == "cash"


def test_the_amount_survives_daftras_extra_decimals(mapper: DaftraPaymentMapper):
    """Daftra sends a float split across tax lines; the message must show the
    rounded amount, and nothing may silently truncate the fraction away."""
    payment = mapper.to_payment(
        {"ClientPayment": {"id": "1", "amount": "32999.9961"}}
    )
    assert payment.amount == Decimal("32999.9961")
    assert f"{payment.amount:,.2f}" == "33,000.00"


def test_the_a_datetime_date_is_parsed(mapper: DaftraPaymentMapper):
    assert mapper.to_payment(REAL_PAYMENT_ROW).payment_date == date(2026, 9, 24)


def test_a_client_payment_has_no_invoice(mapper: DaftraPaymentMapper):
    """The field the template never mentions, and this resource never sets.

    Pinned because the two Daftra resources share a *word* but not a shape: a
    future change that starts reading an invoice id off a client payment would
    otherwise look like an improvement rather than a category error.
    """
    assert mapper.to_payment(REAL_PAYMENT_ROW).invoice_id is None


def test_the_row_phones_alone_make_the_payment_reachable(mapper: DaftraPaymentMapper):
    """A client-payment row carries its own phone numbers, on 99 of 109 live rows.

    This is what the invoice-payment resource does *not* do, and it is why a
    payment can still be delivered when the payer's own record cannot be read:
    skipping a receivable the customer already paid over would be worse than
    trusting a number Daftra put on the receipt itself.
    """
    payment = mapper.to_payment(REAL_PAYMENT_ROW)
    assert payment.customer_phones == ("201022322634",)


def test_the_client_record_supplies_the_business_name(mapper: DaftraPaymentMapper):
    """The reason the second request exists: the row has no company name.

    The captured row's ``first_name``/``last_name`` are **both empty** — this is a
    company client, and Daftra keeps its name on the ``Client`` record. Read
    without that record the payment greets the customer as the generic
    "Customer", which is the whole visible failure of skipping the hop.
    """
    unresolved = mapper.to_payment(REAL_PAYMENT_ROW)
    assert unresolved.customer_name == "Customer", "the row alone cannot name a company"

    resolved = mapper.to_payment(REAL_PAYMENT_ROW, REAL_CLIENT)
    assert resolved.customer_name == "Print Home"
    assert resolved.customer_phones == ("201022322634",)


def test_an_individual_payer_is_named_from_first_and_last(mapper: DaftraPaymentMapper):
    """No business name on either record, so the name is first + last.

    The mirror of the company case, and the reason the merge cannot simply prefer
    ``business_name``: for an individual it is absent on both sides and the
    fallbacks have to carry the whole message.
    """
    assert mapper.to_payment(PERSON_ROW).customer_name == "علي تويج"
    assert mapper.to_payment(PERSON_ROW, PERSON_CLIENT).customer_name == "علي تويج"


def test_the_client_record_wins_for_the_phones(mapper: DaftraPaymentMapper):
    """Phones come from the client record when it is read, empties included.

    It is the authoritative place for them: a client can be re-numbered after a
    payment row was written, and the current number is the one that reaches them.
    The emptied ``phone2`` is the load-bearing half — it must **not** fall back to
    the receipt's stale copy, or a number the operator deliberately cleared would
    keep being messaged.
    """
    renumbered = {
        "data": {
            "Client": {
                "id": "1",
                "business_name": "Print Home",
                "phone1": "+201099988877",
                "phone2": "",
            }
        }
    }
    payment = mapper.to_payment(REAL_PAYMENT_ROW, renumbered)
    assert payment.customer_phones == ("201099988877",), "the receipt's phone2 is stale"


def test_a_payment_whose_payer_is_unreadable_keeps_the_rows_own_phone(mapper: DaftraPaymentMapper):
    """An empty client payload must not erase a phone the row already carried."""
    payment = mapper.to_payment(REAL_PAYMENT_ROW, {})
    assert payment.customer_phones == ("201022322634",)
    assert payment.customer_name == "Customer"


def test_a_payment_with_no_phone_anywhere_is_still_mapped(mapper: DaftraPaymentMapper):
    """Unreachable, not malformed: the poller skips it, the cycle does not fail."""
    payment = mapper.to_payment({"ClientPayment": {"id": "9", "amount": "10", "code": "000009"}})
    assert payment.id == "9"
    assert payment.customer_phones == ()


def test_the_code_falls_back_to_the_id_when_absent(mapper: DaftraPaymentMapper):
    """Never send a blank identifier: the template prints it."""
    payment = mapper.to_payment({"ClientPayment": {"id": "7", "amount": "10"}})
    assert payment.number == "7"


def test_a_payment_with_no_code_or_id_is_rejected(mapper: DaftraPaymentMapper):
    with pytest.raises(ValueError):
        mapper.to_payment({"ClientPayment": {"amount": "10"}})


def test_the_detail_envelope_is_unwrapped(mapper: DaftraPaymentMapper):
    """A single record arrives as ``{"data": {"ClientPayment": {...}}}``."""
    detail = {"result": "successful", "code": 200, "data": REAL_PAYMENT_ROW["ClientPayment"]}
    assert mapper.to_payment(detail).id == "244"

    enveloped = {"result": "successful", "code": 200, "data": {"ClientPayment": REAL_PAYMENT_ROW["ClientPayment"]}}
    assert mapper.to_payment(enveloped).id == "244"


def test_a_flat_row_is_accepted(mapper: DaftraPaymentMapper):
    assert mapper.to_payment(REAL_PAYMENT_ROW["ClientPayment"]).id == "244"


def test_to_payments_skips_a_bad_row_without_losing_the_rest(mapper: DaftraPaymentMapper):
    payments = mapper.to_payments(
        {
            "data": [
                REAL_PAYMENT_ROW,
                {"ClientPayment": {"amount": "not-a-number"}},
                {"ClientPayment": {"id": "245", "amount": "5", "code": "000245"}},
            ]
        }
    )
    assert [p.id for p in payments] == ["244", "245"]


def test_an_empty_listing_is_an_empty_list(mapper: DaftraPaymentMapper):
    assert mapper.to_payments({"data": []}) == []
    assert mapper.to_payments({}) == []


def test_the_row_label_names_the_payment_for_the_operator():
    """Both payment wrappers must label as a payment, and name themselves by code.

    ``ClientPayment`` is the wrapper the payments pipeline now reads; without it in
    this table every warning about a bad row would degrade to "an unidentifiable
    record", which tells an operator nothing about what to go and look at.
    """
    assert _row_label(REAL_PAYMENT_ROW) == "payment 000244"
    assert _row_label({"InvoicePayment": {"code": "000116"}}) == "payment 000116"
    assert _row_label({"ClientPayment": {"amount": "x"}}) == "a payment"
    # The invoice and customer wording is unchanged, so their warnings still read right.
    assert _row_label({"Invoice": {"no": "000002"}}) == "invoice 000002"
    assert _row_label({"Invoice": {}}) == "an invoice"


def test_a_localized_amount_is_parsed(mapper: DaftraPaymentMapper):
    """Same rule as invoices: an Arabic-Indic amount must not change magnitude."""
    assert mapper.to_payment(
        {"ClientPayment": {"id": "1", "amount": "١٢٥٠٫٠٠"}}
    ).amount == Decimal("1250.00")


def test_an_unparseable_amount_raises_rather_than_becoming_zero(mapper: DaftraPaymentMapper):
    """A silent ``0.00`` on a customer-facing message is the worst failure this
    mapper can have, so a present-but-unparseable amount fails loudly."""
    with pytest.raises(ValueError):
        mapper.to_payment({"ClientPayment": {"id": "1", "amount": "n/a"}})


class _PaymentSession:
    """Answers the two-hop payment fetch and records which URLs were hit."""

    def __init__(self, payment: dict = None, client: dict | None = None) -> None:
        self._payment = payment if payment is not None else REAL_PAYMENT_ROW["ClientPayment"]
        self._client = client
        self.headers: dict = {}
        self.urls: list[str] = []
        self.params = None

    def get(self, url: str, **kwargs):
        from fakes import FakeResponse

        self.urls.append(url)
        self.params = kwargs.get("params")
        if "/client_payments/" in url:
            return FakeResponse(
                200, {"result": "successful", "data": {"ClientPayment": self._payment}}
            )
        return FakeResponse(200, self._client)


def test_the_client_resolves_the_payer_with_two_requests():
    """The cost of reaching a payment's customer, made explicit.

    One request for the payment, one for the client that names the payer. The URLs
    are asserted in order because the whole point of doing the join inside the
    adapter is that the application layer never learns this order.
    """
    session = _PaymentSession(REAL_PAYMENT_ROW["ClientPayment"], REAL_CLIENT)
    client = DaftraClient(
        api_key="k", session=session, country_code="20", payments_status="1"
    )
    payment = client.get_payment("244")
    assert payment.customer_name == "Print Home"
    assert payment.customer_phones == ("201022322634",)
    assert [url.split("/api2/")[-1] for url in session.urls] == [
        "client_payments/244.json",
        "clients/1.json",
    ]


def test_a_payment_naming_no_client_costs_one_request():
    """No ``client_id`` means no second hop to make, and no request is invented."""
    session = _PaymentSession({"id": "9", "code": "000009", "amount": "10", "client_id": None})
    client = DaftraClient(api_key="k", session=session, country_code="20", payments_status="1")
    payment = client.get_payment("9")
    assert payment.customer_phones == ()
    assert [url.split("/api2/")[-1] for url in session.urls] == ["client_payments/9.json"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Every envelope the client can be handed.
        ({"data": {"ClientPayment": {"client_id": "1"}}}, "1"),
        ({"data": {"ClientPayment": {"client_id": 1}}}, "1"),
        ({"ClientPayment": {"client_id": "1"}}, "1"),
        ({"id": "1", "client_id": "1"}, "1"),
        # And the four ways of saying "this names nobody". The string ``"None"`` is
        # in that list on purpose: Daftra's PHP-flavoured JSON spells an absent id
        # that way, and passing it through would send a request for customer
        # ``"None"`` and turn a skippable payment into a 404.
        ({"data": {"ClientPayment": {"client_id": None}}}, None),
        ({"data": {"ClientPayment": {"client_id": ""}}}, None),
        ({"data": {"ClientPayment": {"client_id": "None"}}}, None),
        ({"data": {"ClientPayment": {"amount": "10"}}}, None),
        ({"data": {"amount": "10"}}, None),
        ({}, None),
    ],
)
def test_the_payer_id_is_read_from_every_envelope(raw, expected):
    assert DaftraClient._client_id(raw) == expected


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
    assert [p.id for p in payments] == ["244"]


def test_the_listing_reads_client_payments_and_not_invoice_payments():
    """The endpoint is the whole point of the rework, so it is pinned by name.

    ``/invoice_payments.json`` and ``/client_payments.json`` are separate resources
    with disjoint id spaces (125 against 109 on the live account, none shared).
    Announcing the wrong one is not a narrowing — it is announcing a different set
    of money entirely, and it would look perfectly healthy in the summary line.
    """
    from fakes import FakeResponse

    class _Listing(_PaymentSession):
        def get(self, url: str, **kwargs):
            self.urls.append(url)
            self.params = kwargs.get("params")
            return FakeResponse(200, {"data": [REAL_PAYMENT_ROW]})

    session = _Listing()
    DaftraClient(api_key="k", session=session).list_payments()
    assert [url.split("/api2/")[-1] for url in session.urls] == ["client_payments.json"]
    assert not any("invoice_payments" in url for url in session.urls)


def test_no_include_client_credit_is_sent():
    """That flag was an ``/invoice_payments.json`` defect, and this is not that endpoint.

    It is asserted absent deliberately. Carrying a workaround for a bug in a
    resource this pipeline no longer reads would be cargo cult: the flag is
    meaningless here, and a future reader would reasonably assume it was load
    bearing.
    """
    from fakes import FakeResponse

    class _Listing(_PaymentSession):
        def get(self, url: str, **kwargs):
            self.params = kwargs.get("params")
            return FakeResponse(200, {"data": []})

    session = _Listing()
    DaftraClient(api_key="k", session=session).list_payments()
    assert "include_client_credit" not in (session.params or {})


def test_the_source_total_is_kept_for_the_quiet_listing_tripwire():
    """The listing's own count is recorded, and an absent one stays ``None``.

    ``None`` is the load-bearing part: the tripwire compares the source's count
    against what has already been handled, and a source that reports nothing must
    read as "no opinion" rather than "there is nothing there" — otherwise a
    payload without ``pagination`` would read as a count of zero and warn on
    every cycle.
    """
    from fakes import FakeResponse

    class _WithTotal(_PaymentSession):
        def get(self, url: str, **kwargs):
            return FakeResponse(
                200,
                {"data": [REAL_PAYMENT_ROW], "pagination": {"total_results": 109}},
            )

    class _WithoutTotal(_PaymentSession):
        def get(self, url: str, **kwargs):
            return FakeResponse(200, {"data": [REAL_PAYMENT_ROW]})

    counted = DaftraClient(api_key="k", session=_WithTotal())
    assert counted.last_listing_total is None, "no listing has run yet"
    counted.list_payments()
    assert counted.last_listing_total == 109

    silent = DaftraClient(api_key="k", session=_WithoutTotal())
    silent.list_payments()
    assert silent.last_listing_total is None

    # A non-integer count is not a count. ``True`` is an ``int`` in Python and must
    # not pass as "1 payment exists".
    class _Nonsense(_PaymentSession):
        def get(self, url: str, **kwargs):
            return FakeResponse(200, {"data": [], "pagination": {"total_results": True}})

    odd = DaftraClient(api_key="k", session=_Nonsense())
    odd.list_payments()
    assert odd.last_listing_total is None