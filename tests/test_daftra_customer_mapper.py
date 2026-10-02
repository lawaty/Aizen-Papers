"""Wire-format specs for ``/clients.json`` → :class:`Customer`.

Mirrors ``test_daftra_payment_mapper.py``. The captured rows are real responses
from live accounts, so these tests fail loudly if Daftra's shape ever moves.

Three facts from the live probe shape this file, and each has its own test below
because each fails *silently* if it breaks:

* the envelope wraps every row in its own capitalized ``Client`` key;
* ``created`` is the only "when was this customer created" signal Daftra gives;
* the listing is **not** newest-first unless the request says so, which is pinned
  in ``test_list_customers_requests_newest_first_ordering`` below.
"""

from datetime import date

import pytest

from sender.infrastructure.daftra.client import DaftraClient
from sender.infrastructure.daftra.mapper import DaftraCustomerMapper, _row_label

#: Verbatim from `GET /clients.json` on the production Aizen account, trimmed to
#: the fields the mapper reads. Note the shape that matters: a **company** row —
#: ``business_name`` set, ``first_name``/``last_name`` empty — with an
#: already-E.164 ``phone1`` and ``phone2`` duplicating it.
REAL_COMPANY_ROW = {
    "Client": {
        "id": "1",
        "client_number": "000001",
        "business_name": "Print Home",
        "first_name": "",
        "last_name": "",
        "email": "",
        "phone1": "+201022322634",
        "phone2": "+201022322634",
        "country_code": "EG",
        "created": "2026-09-24 01:10:36",
        "modified": "2026-09-24 01:10:36",
        "suspend": "0",
        "is_offline": "1",
        "type": "3",
        "site_id": "5059700",
        "starting_balance": None,
    }
}

#: A second live account, showing the **individual** shape: no business name, a
#: first and last name, and an empty-string phone that must become ``None``.
REAL_INDIVIDUAL_ROW = {
    "Client": {
        "id": "2",
        "client_number": "",
        "business_name": None,
        "first_name": "خالد",
        "last_name": "الغرباو",
        "email": None,
        "phone1": "",
        "phone2": None,
        "country_code": None,
        "created": "2025-12-10 16:33:01",
        "suspend": "0",
        "is_offline": "0",
        "type": "2",
    }
}

#: A third live row with local-format phone and no name at all.
REAL_BARE_ROW = {
    "Client": {
        "id": "5",
        "client_number": "",
        "business_name": None,
        "first_name": None,
        "last_name": None,
        "phone1": None,
        "phone2": None,
        "created": "2026-01-05 16:34:09",
        "suspend": "0",
        "is_offline": "0",
    }
}


@pytest.fixture
def mapper() -> DaftraCustomerMapper:
    return DaftraCustomerMapper(country_code="20")


class _Session:
    """A requests-shaped session that records the URL and params of each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.headers: dict = {}
        self.response = None

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs.get("params") or {}))
        return self.response

    def request(self, method: str, url: str, **kwargs):
        self.calls.append((url, kwargs.get("params") or {}))
        return self.response


# --- mapping -----------------------------------------------------------------


def test_a_live_company_row_maps_onto_the_model(mapper):
    customer = mapper.to_customer(REAL_COMPANY_ROW)
    assert customer.id == "1"
    assert customer.number == "000001"
    assert customer.customer_name == "Print Home"
    assert customer.customer_phones == ("201022322634",)
    assert customer.created == date(2026, 9, 24)
    assert customer.type == "3"


def test_the_Client_wrapper_is_unwrapped_from_a_listing_row(mapper):
    """The row arrives as ``{"data": [{"Client": {...}}]}`` — one wrapper per row."""
    payload = {"result": "successful", "data": [REAL_COMPANY_ROW]}
    assert [c.id for c in mapper.to_customers(payload)] == ["1"]


def test_the_Client_wrapper_is_unwrapped_from_a_detail_payload(mapper):
    """A single record nests one level differently: ``{"data": {"Client": {...}}}``."""
    payload = {"result": "successful", "data": REAL_COMPANY_ROW}
    assert mapper.to_customer(payload).customer_name == "Print Home"


def test_a_flat_row_is_accepted_too(mapper):
    """The offline stub and hand-written fixtures use the flat shape."""
    flat = dict(REAL_COMPANY_ROW["Client"])
    assert mapper.to_customer(flat).id == "1"


def test_an_individual_row_joins_first_and_last_name(mapper):
    customer = mapper.to_customer(REAL_INDIVIDUAL_ROW)
    assert customer.customer_name == "خالد الغرباو"
    # Empty-string phone must become None, not "" — "" would read as "has a phone"
    # to the poller's `not doc.customer_phones` check only by luck, and would be
    # normalized into a bogus recipient by anything that treated it as truthy.
    assert customer.customer_phones == ()


def test_a_row_with_no_name_falls_back_to_a_neutral_greeting_name(mapper):
    """Never empty: the template greets by name, and skipping would retire the
    customer unseen and lose the welcome permanently."""
    assert mapper.to_customer(REAL_BARE_ROW).customer_name == "Customer"


def test_a_local_phone_is_prefixed_with_the_country_code(mapper):
    row = {"Client": {"id": "1", "phone1": "01027693262"}}
    assert mapper.to_customer(row).customer_phones == ("201027693262",)


def test_an_already_e164_phone_is_left_alone(mapper):
    row = {"Client": {"id": "1", "phone1": "+201022322634"}}
    assert mapper.to_customer(row).customer_phones == ("201022322634",)


def test_a_missing_phone_becomes_none(mapper):
    row = {"Client": {"id": "1", "phone1": None, "phone2": ""}}
    assert mapper.to_customer(row).customer_phones == ()


def test_phone2_comes_first_and_phone1_is_also_a_recipient(mapper):
    """``phone2`` still leads, and ``phone1`` is now reached as well.

    The old contract was "first non-empty wins", because a document had exactly
    one recipient. Both fields being collected means the ordering is no longer a
    choice between the two — it only decides which one is *primary* — and both
    numbers now receive the message. The order is kept anyway: it is what the
    report and the summary show first, and on live rows ``phone2`` is sometimes
    where the reachable number actually is.
    """
    row = {"Client": {"id": "1", "phone1": "+201022322634", "phone2": "+201027693262"}}
    assert mapper.to_customer(row).customer_phones == ("201027693262", "201022322634")


def test_the_same_number_in_both_fields_is_one_recipient(mapper):
    """Two fields holding one number must not become two messages.

    Normalization is what makes this decidable: ``01027693262`` and
    ``+201027693262`` are the same subscriber written two ways, and that is the
    common shape of a record someone filled in by hand.
    """
    row = {"Client": {"id": "1", "phone1": "01027693262", "phone2": "+201027693262"}}
    assert mapper.to_customer(row).customer_phones == ("201027693262",)


def test_an_unusable_number_is_dropped_and_the_usable_one_still_goes_out(mapper, caplog):
    """One bad field must not cost the customer the message entirely.

    Filling both fields but mistyping one is the case this protects: the old
    mapper would have taken the bad value, failed to normalize it, and dropped
    the invoice. Now the good number still goes out and the operator is told
    which field was ignored.
    """
    row = {"Client": {"id": "1", "phone1": "12345", "phone2": "+201027693262"}}
    with caplog.at_level("WARNING", logger="sender.infrastructure.daftra.mapper"):
        phones = mapper.to_customer(row).customer_phones
    assert phones == ("201027693262",)
    assert "phone1" in caplog.text


def test_a_blank_client_number_falls_back_to_the_id(mapper):
    assert mapper.to_customer(REAL_INDIVIDUAL_ROW).number == "2"


def test_a_row_with_no_id_is_rejected(mapper):
    with pytest.raises(ValueError):
        mapper.to_customer({"Client": {"business_name": "No Id"}})


def test_an_empty_listing_maps_to_an_empty_list(mapper):
    assert mapper.to_customers({"result": "successful", "data": []}) == []
    assert mapper.to_customers({}) == []


def test_one_unmappable_row_does_not_lose_the_rest(mapper, caplog):
    """A bad row is skipped and left unseen so the next cycle retries it; the
    rest of the page must still flow."""
    payload = {
        "result": "successful",
        "data": [{"Client": {"business_name": "No id at all"}}, REAL_COMPANY_ROW],
    }
    with caplog.at_level("WARNING"):
        customers = mapper.to_customers(payload)
    assert [c.id for c in customers] == ["1"]
    assert any("customer" in record.getMessage() for record in caplog.records)


def test_the_row_label_identifies_a_bad_client_row():
    """So the operator's log line names *which* customer could not be mapped."""
    assert _row_label(REAL_COMPANY_ROW) == "customer 1"


# --- client ------------------------------------------------------------------


def test_list_customers_requests_newest_first_ordering():
    """The one test that stops a silent, total failure.

    ``/clients.json`` answers in a stable but *arbitrary* order by default (a live
    account returned ``[5,1,2,6,4,3]`` for ids 1..6). The poller walks pages
    forward and stops at the first already-seen record, so under an arbitrary
    order it stops early on a saturated page and silently never announces a new
    customer further back — no error, no warning, a pipeline that just quietly
    stops working.

    ``sort=created&direction=desc`` is the only combination this endpoint honours
    that yields true newest-first (verified live: ``sort=created`` alone gives
    *oldest* first, ``order=desc`` is ignored). So the exact params are pinned
    here: dropping, renaming or "simplifying" any of them must fail this test.
    """
    from fakes import FakeResponse

    class _Session2:
        headers: dict = {}

        def __init__(self):
            self.params = None
            self.url = None

        def get(self, url: str, **kwargs):
            self.url = url
            self.params = kwargs.get("params")
            return FakeResponse(200, {"data": [REAL_COMPANY_ROW]})

    session = _Session2()
    client = DaftraClient(api_key="k", session=session)
    customers = client.list_customers(limit=5, page=2)

    assert session.params == {
        "page": 2,
        "limit": 5,
        "sort": "created",
        "direction": "desc",
    }
    assert session.url.endswith("/clients.json")
    assert [c.id for c in customers] == ["1"]


def test_list_customers_sends_no_date_filter():
    """Deliberate, and pinned so nobody "optimizes" it back in.

    The endpoint *does* accept ``created_from`` — but its boundary is
    date-granular and inclusive (the time component is truncated), so it cannot
    express a precise watermark, and Daftra **silently ignores** every other
    spelling, answering 200 with the unfiltered set. A misspelled filter would
    look like it worked while scanning everything, which is the worst kind of
    failure. The seen-set is already the watermark.
    """
    from fakes import FakeResponse

    class _Session2:
        headers: dict = {}

        def __init__(self):
            self.params = None

        def get(self, url: str, **kwargs):
            self.params = kwargs.get("params")
            return FakeResponse(200, {"data": []})

    session = _Session2()
    DaftraClient(api_key="k", session=session).list_customers(limit=5)
    assert set(session.params) == {"page", "limit", "sort", "direction"}


def test_get_customer_is_a_single_request():
    """The contrast with payments, which need a second hop to find the payer.

    A client row *is* the customer, so reaching one costs exactly one read.
    """
    from fakes import FakeResponse

    class _Session2:
        headers: dict = {}

        def __init__(self):
            self.urls = []

        def get(self, url: str, **kwargs):
            self.urls.append(url)
            return FakeResponse(200, {"data": REAL_COMPANY_ROW})

    session = _Session2()
    customer = DaftraClient(api_key="k", session=session).get_customer(1)
    assert len(session.urls) == 1, "a client row needs no second hop to find its customer"
    assert session.urls[0].endswith("/clients/1.json")
    assert customer.customer_name == "Print Home"


def test_a_non_numeric_customer_id_is_rejected_before_any_request():
    client = DaftraClient(api_key="k", session=_Session())
    with pytest.raises(ValueError):
        client.get_customer("../etc/passwd")
