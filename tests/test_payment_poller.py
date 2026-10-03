"""Offline specs for the payments polling pipeline.

Mirrors ``test_poller.py``. The point of these tests is *not* that payments get
their own coverage — it is that the shared engine's invariants hold for the second
caller as well. Anything that differs between the pipelines (a payment listing
row has no customer, so the detail is always fetched) is pinned here too.
"""

from decimal import Decimal

import pytest

from fakes import CapturingSender, FakeResponse, FakeSession
from sender.application.poller import PaymentPoller, PollApp
from sender.domain.errors import DaftraApiError, WhatsAppApiError
from sender.domain.templates import PaymentTemplateBuilder
from sender.infrastructure.state import InMemoryPollStateStore
from sender.presentation.stubs import (
    StubPaymentSource,
    default_stub_payments,
    make_stub_payment,
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class FailingSource:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def list_payments(self, limit: int = 10, page: int = 1):
        raise self.error

    def get_payment(self, payment_id):
        raise self.error

    def get_raw_payment(self, payment_id):
        raise self.error


class CountingPaymentSource:
    """A source that lists whatever it is given and counts the detail fetches."""

    def __init__(self, listed, detail) -> None:
        self.listed = listed
        self.detail = detail
        self.detail_calls: list[str] = []

    def list_payments(self, limit: int = 10, page: int = 1):
        return list(self.listed)

    def get_payment(self, payment_id):
        self.detail_calls.append(str(payment_id))
        return self.detail

    def get_raw_payment(self, payment_id):
        return {}


def _builder() -> PaymentTemplateBuilder:
    return PaymentTemplateBuilder(
        template_name="aizen_new_payment", language="ar_EG", country_code="20"
    )


def _app(name: str = "app1", payments=None) -> PollApp:
    return PollApp(
        name=name,
        source=StubPaymentSource(
            payments if payments is not None else default_stub_payments(), status=None
        ),
    )


def _poller(apps, sender=None, state=None, clock=None, **kwargs) -> PaymentPoller:
    return PaymentPoller(
        apps=apps,
        sender=sender,
        builder=_builder(),
        state=state or InMemoryPollStateStore(),
        clock=clock or FakeClock(),
        **kwargs,
    )


def _sent_payments(sender: CapturingSender) -> list[tuple[str, str]]:
    """Every payment that went out, as (customer, reference code).

    The listing is newest-first, so the order the poller sends in is the reverse
    of the fixture order; these tests ask *what* went out, not in which order.
    """
    sent = []
    for payload in sender.payloads:
        params = payload["template"]["components"][0]["parameters"]
        values = [p["text"] for p in params]
        sent.append(
            (values[0].strip("\u2068\u2069"), values[1].strip("\u2066\u2069"))
        )
    return sent


# -- the three invariants ------------------------------------------------------


def test_a_new_payment_is_sent_to_the_payer_phone():
    sender = CapturingSender()
    poller = _poller([_app()], sender=sender, send_existing=True)
    summary = poller.run_once()
    app = summary["apps"][0]
    assert (app["listed"], app["new"], app["sent"]) == (3, 3, 2)
    # 000003 is the no-phone fixture, so it is skipped and retired, not retried.
    assert app["skipped_no_phone"] == 1
    assert {p["to"] for p in sender.payloads} == {"201027693262", "201234567890"}
    assert set(_sent_payments(sender)) == {("Ahmed Hassan", "000001"), ("Mona Farouk", "000002")}


def test_a_seen_payment_is_not_resent_on_the_next_cycle():
    state = InMemoryPollStateStore()
    sender = CapturingSender()
    _poller([_app()], sender=sender, state=state, send_existing=True).run_once()
    assert len(sender.payloads) == 2
    again = _poller([_app()], sender=CapturingSender(), state=state).run_once()
    assert again["apps"][0]["sent"] == 0
    assert again["apps"][0]["new"] == 0


def test_the_first_run_seeds_payments_without_sending():
    """The highest-risk behaviour in the feature: a fresh state must not announce
    every payment ever recorded."""
    sender = CapturingSender()
    state = InMemoryPollStateStore()
    summary = _poller([_app()], sender=sender, state=state).run_once()
    app = summary["apps"][0]
    assert app["first_run"] is True
    assert app["seeded"] == 3
    assert app["sent"] == 0
    assert sender.payloads == []
    # Seeded, so the next cycle has nothing to do.
    assert _poller([_app()], sender=sender, state=state).run_once()["apps"][0]["sent"] == 0


def test_send_existing_confirms_payments_on_the_first_run():
    sender = CapturingSender()
    summary = _poller([_app()], sender=sender, send_existing=True).run_once()
    assert summary["apps"][0]["sent"] == 2
    assert len(sender.payloads) == 2


def test_a_dry_run_builds_payloads_but_never_calls_the_sender():
    sender = CapturingSender()
    summary = _poller([_app()], sender=sender, dry_run=True, send_existing=True).run_once()
    app = summary["apps"][0]
    assert app["would_send"] == 2
    assert app["sent"] == 0
    assert sender.payloads == []


def test_a_dry_run_does_not_mutate_state(tmp_path):
    """Not even the file: a rehearsal must leave nothing for a real run to trip on."""
    from sender.infrastructure.state import JsonPollStateStore

    path = tmp_path / "payments_state.json"
    poller = _poller(
        [_app()],
        sender=CapturingSender(),
        state=JsonPollStateStore(str(path)),
        dry_run=True,
        send_existing=True,
    )
    poller.run_once()
    assert not path.exists()


def test_a_retryable_send_failure_is_retried_and_never_given_up():
    sender = CapturingSender()
    state = InMemoryPollStateStore()
    clock = FakeClock(1000.0)
    error = WhatsAppApiError(503, "upstream", {"error": {"code": 5}})
    poller = PaymentPoller(
        apps=[_app(payments=[make_stub_payment()])],
        sender=_FailingSender(error),
        builder=_builder(),
        state=state,
        clock=clock,
        send_existing=True,
    )
    first = poller.run_once()["apps"][0]
    assert (first["failed"], first["abandoned"]) == (1, 0)
    assert not state.seen("app1", "1"), "a retryable failure must never retire the payment"
    assert state.pending("app1"), "it must be recorded for a later cycle"
    # Still pending (backoff not elapsed), so a second cycle defers rather than resends.
    second = poller.run_once()["apps"][0]
    assert second["pending"] == 1
    assert second["sent"] == 0


def test_a_permanent_failure_is_abandoned_immediately():
    state = InMemoryPollStateStore()
    sender = _FailingSender(WhatsAppApiError(131047, "outside window", {"error": {"code": 131047}}))
    poller = PaymentPoller(
        apps=[_app(payments=[make_stub_payment()])],
        sender=sender,
        builder=_builder(),
        state=state,
        clock=FakeClock(1000.0),
        send_existing=True,
    )
    summary = poller.run_once()["apps"][0]
    assert (summary["failed"], summary["abandoned"]) == (0, 1)
    assert state.seen("app1", "1"), "a permanent failure must not be retried forever"
    assert state.abandoned("app1"), "and must stay visible for an operator"


# -- what is specific to payments ---------------------------------------------


def test_the_detail_is_always_fetched_because_the_listing_has_no_payer():
    """Daftra's payment rows name no client, so reaching the customer costs a
    second request every time. Pinned so the cost is a decision, not a surprise."""
    payment = make_stub_payment()
    source = CountingPaymentSource([payment], payment)
    poller = PaymentPoller(
        apps=[PollApp(name="app1", source=source)],
        sender=CapturingSender(),
        builder=_builder(),
        state=InMemoryPollStateStore(),
        clock=FakeClock(1000.0),
        send_existing=True,
    )
    poller.run_once()
    assert source.detail_calls == ["1"]


def test_a_failed_detail_fetch_keeps_the_payment_for_a_retry():
    state = InMemoryPollStateStore()
    source = CountingPaymentSource(
        [make_stub_payment()], None
    )
    poller = PaymentPoller(
        apps=[PollApp(name="app1", source=_FailingDetail(source, DaftraApiError(None, "network")))],
        sender=CapturingSender(),
        builder=_builder(),
        state=state,
        clock=FakeClock(1000.0),
        send_existing=True,
    )
    summary = poller.run_once()["apps"][0]
    assert summary["failed"] == 1
    assert not state.seen("app1", "1")
    assert state.pending("app1")


def test_a_deleted_linked_invoice_abandons_the_payment():
    """A 404 from the invoice hop is permanent: retrying cannot bring the invoice
    back, so the payment is abandoned once and left visible rather than retried
    forever against a record that will never resolve."""
    state = InMemoryPollStateStore()
    poller = PaymentPoller(
        apps=[PollApp(name="app1", source=_FailingDetail(
            CountingPaymentSource([make_stub_payment()], None), DaftraApiError(404, "gone")
        ))],
        sender=CapturingSender(),
        builder=_builder(),
        state=state,
        clock=FakeClock(1000.0),
        send_existing=True,
    )
    summary = poller.run_once()["apps"][0]
    assert summary["abandoned"] == 1
    assert state.abandoned("app1")
    assert not state.pending("app1")


def test_a_payment_without_a_phone_is_skipped_and_marked_seen(caplog):
    state = InMemoryPollStateStore()
    poller = _poller(
        [_app(payments=[make_stub_payment(customer_phones=(), number="000009")])],
        sender=CapturingSender(),
        state=state,
        send_existing=True,
    )
    with caplog.at_level("WARNING"):
        summary = poller.run_once()["apps"][0]
    assert summary["skipped_no_phone"] == 1
    assert state.seen("app1", "1")
    assert any("000009" in r.message and "no usable WhatsApp phone" in r.message for r in caplog.records)


def test_the_summary_says_payment_and_carries_the_kind():
    summary = _poller([_app()], sender=CapturingSender(), send_existing=True).run_once()
    app = summary["apps"][0]
    assert app["kind"] == "payment"
    # The reference code is what an operator looks up, not the internal id.
    assert {row["number"] for row in app["invoices"]} == {"000001", "000002", "000003"}


def test_an_unexpected_error_in_one_app_does_not_stop_the_others():
    healthy = _app(name="healthy")
    sender = CapturingSender()
    poller = _poller(
        [PollApp(name="broken", source=FailingSource(DaftraApiError(401, "unauthorized"))), healthy],
        sender=sender,
        send_existing=True,
    )
    summary = poller.run_once()
    assert summary["apps"][0]["ok"] is False
    assert summary["apps"][1]["sent"] == 2


def test_the_send_cap_leaves_payments_unseen_for_a_later_cycle():
    state = InMemoryPollStateStore()
    payments = [
        make_stub_payment(id=str(i), number=f"00000{i}", customer_name=f"Customer {i}")
        for i in range(1, 6)
    ]
    sender = CapturingSender()
    poller = _poller(
        [_app(payments=payments)],
        sender=sender,
        state=state,
        max_sends_per_run=2,
        send_existing=True,
    )
    first = poller.run_once()["apps"][0]
    assert first["sent"] == 2
    assert first["deferred_by_cap"] == 3
    # The listing is newest-first, so a capped cycle sends the *newest* payments
    # and the oldest are the ones left behind — and left behind means still unseen.
    assert state.seen("app1", "5") and state.seen("app1", "4")
    assert not state.seen("app1", "1"), "the cap must not retire what it deferred"
    second = _poller([_app(payments=payments)], sender=CapturingSender(), state=state).run_once()
    assert second["apps"][0]["sent"] == 3


def test_a_zero_cap_disables_the_limit():
    payments = [make_stub_payment(id=str(i), number=f"00000{i}") for i in range(1, 13)]
    sender = CapturingSender()
    poller = _poller(
        [_app(payments=payments)], sender=sender, max_sends_per_run=0, send_existing=True
    )
    assert poller.run_once()["apps"][0]["sent"] == 12
    assert len(sender.payloads) == 12


def test_a_successful_send_is_recorded_with_the_payment_kind():
    from sender.domain.models import KIND_PAYMENT

    recorded = []

    class _Recorder:
        def record(self, outcome):
            recorded.append(outcome)

    poller = PaymentPoller(
        apps=[_app(payments=[make_stub_payment()])],
        sender=CapturingSender(),
        builder=_builder(),
        state=InMemoryPollStateStore(),
        clock=FakeClock(1000.0),
        recorder=_Recorder(),
        send_existing=True,
    )
    poller.run_once()
    assert len(recorded) == 1
    outcome = recorded[0]
    assert outcome.kind == KIND_PAYMENT
    # The payment's own facts, mapped onto the shared report row.
    assert outcome.invoice_number == "000001"
    assert outcome.total == Decimal("1500.00")
    assert outcome.issue_date is not None
    assert outcome.wamid == "wamid.FAKE"


def test_a_dry_run_records_nothing():
    from sender.domain.models import SENT  # noqa: F401 - documents the intent

    recorded = []

    class _Recorder:
        def record(self, outcome):
            recorded.append(outcome)

    PaymentPoller(
        apps=[_app(payments=[make_stub_payment()])],
        sender=CapturingSender(),
        builder=_builder(),
        state=InMemoryPollStateStore(),
        clock=FakeClock(1000.0),
        recorder=_Recorder(),
        dry_run=True,
        send_existing=True,
    ).run_once()
    assert recorded == []


class _FailingSender:
    """Fails every send with a fixed error."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.payloads: list[dict] = []

    def send(self, payload: dict) -> dict:
        self.payloads.append(payload)
        raise self.error


class _FailingDetail:
    """Lists fine, but the detail fetch fails — the two-hop adapter's failure mode."""

    def __init__(self, inner, error: Exception) -> None:
        self._inner = inner
        self.error = error

    def list_payments(self, limit: int = 10, page: int = 1):
        return self._inner.list_payments(limit=limit, page=page)

    def get_payment(self, payment_id):
        raise self.error

    def get_raw_payment(self, payment_id):
        return {}


def test_the_listing_is_newest_first_and_paged():
    """Same contract as the invoice listing, because the paging walk depends on it."""
    source = StubPaymentSource(
        [make_stub_payment(id=str(i), number=f"00000{i}") for i in range(1, 6)], status=None
    )
    assert [p.number for p in source.list_payments(limit=2, page=1)] == ["000005", "000004"]
    assert [p.number for p in source.list_payments(limit=2, page=2)] == ["000003", "000002"]


def test_the_stub_listing_honours_the_status_filter():
    """Daftra narrows server-side, so the stub narrows too — a rehearsal must page
    over the same rows production would."""
    payments = [
        make_stub_payment(id="1", number="000001", status="1"),
        make_stub_payment(id="2", number="000002", status="2"),
    ]
    assert [p.number for p in StubPaymentSource(payments).list_payments()] == ["000001"]


class _TotalReportingSource:
    """A source that also reports how many rows the account holds in total.

    Mirrors ``DaftraClient.last_listing_total``, which the poller reads with
    ``getattr`` so a source without an opinion is simply never asked.
    """

    def __init__(self, listed, total) -> None:
        self._listed = list(listed)
        self.last_listing_total = total
        self.detail_calls: list[str] = []
        self.pages: list[tuple[int, int]] = []

    def list_payments(self, limit: int = 10, page: int = 1):
        # Slices rather than returning everything: the poller's paging walk reasons
        # about a page that runs out, so a source that ignored ``limit`` would make
        # it re-read the same rows and inflate the seen-set.
        self.pages.append((limit, page))
        start = (page - 1) * limit
        return self._listed[start : start + limit]

    def get_payment(self, payment_id):
        self.detail_calls.append(str(payment_id))
        return self._listed[0]

    def get_raw_payment(self, payment_id):
        return {}


def _poller_for_source(source, state=None, sender=None, **kwargs) -> PaymentPoller:
    return PaymentPoller(
        apps=[PollApp(name="app1", source=source)],
        sender=sender or CapturingSender(),
        builder=_builder(),
        state=state or InMemoryPollStateStore(),
        clock=FakeClock(),
        **kwargs,
    )


def test_a_listing_that_has_gone_empty_for_an_established_app_is_reported(caplog):
    """An empty page is not a quiet day once documents have been handled.

    Every already-handled document exists in the account, so a source offering
    none of them has changed under us — a filter, the endpoint, or the account.
    Reported, because the alternative is the exact failure this pipeline had: a
    healthy-looking ``new 0``, a zero exit code, and nothing in the log.
    """
    state = InMemoryPollStateStore()
    payments = [make_stub_payment(id=str(i), number=f"00000{i}") for i in (1, 2)]
    _poller_for_source(_TotalReportingSource(payments, 2), state=state).run_once()
    assert state.seen_ids("app1"), "the first cycle should have handled something"

    caplog.clear()
    gone = _TotalReportingSource([], 0)
    _poller_for_source(gone, state=state).run_once()

    assert "came back empty" in caplog.text
    assert "app1" in caplog.text


def test_a_quiet_cycle_on_an_established_app_is_not_reported(caplog):
    """A page of already-seen documents is the steady state and must stay silent.

    The tripwire's whole value is that it is rare. If an ordinary cycle with
    nothing new logged a warning, the real one would be buried in a warning that
    fires every five minutes.
    """
    state = InMemoryPollStateStore()
    payments = [make_stub_payment(id=str(i), number=f"00000{i}") for i in (1, 2)]
    _poller_for_source(_TotalReportingSource(payments, 2), state=state).run_once()

    caplog.clear()
    _poller_for_source(_TotalReportingSource(payments, 2), state=state).run_once()

    assert "came back empty" not in caplog.text
    assert "narrowed" not in caplog.text


def test_an_established_app_whose_first_cycle_finds_nothing_is_not_reported(caplog):
    """No documents handled yet means an empty page is just an empty account."""
    state = InMemoryPollStateStore()
    _poller_for_source(_TotalReportingSource([], 0), state=state).run_once()
    assert "came back empty" not in caplog.text


def test_a_listing_that_has_narrowed_below_what_was_handled_is_reported(caplog):
    """The regression that hid for weeks: the endpoint showing less, not more.

    ``/invoice_payments.json`` answered with one of a hundred and twenty-five rows
    for months because ``client_credit`` payments are excluded by default. Nothing
    about the page itself looks wrong — it is full, it is all already seen, it is
    newest-first — so only the source's own count reveals that the account did
    not shrink.
    """
    state = InMemoryPollStateStore()
    payments = [make_stub_payment(id=str(i), number=f"00000{i}") for i in (1, 2, 3)]
    _poller_for_source(_TotalReportingSource(payments, 3), state=state).run_once()
    assert len(state.seen_ids("app1")) == 3

    caplog.clear()
    # Same rows, but the source now claims the account holds fewer than we handled.
    _poller_for_source(_TotalReportingSource(payments, 1), state=state).run_once()

    assert "narrowed" in caplog.text
    assert "app1" in caplog.text


def test_a_source_count_that_is_off_by_one_is_not_a_narrowing(caplog):
    """Daftra's ``total_results`` disagrees with itself; that must stay quiet.

    Found in production: the same tenant, same filter, same rows answered
    ``total_results=126`` at ``limit=500`` and ``125`` at ``limit=10``. Compared
    exactly, that warns every five minutes on a healthy pipeline — and a warning
    that always fires is the one an operator learns to skip, which would have
    hidden the actual fault this check was written for.
    """
    state = InMemoryPollStateStore()
    payments = [make_stub_payment(id=str(i), number=f"00000{i}") for i in range(1, 7)]
    _poller_for_source(_TotalReportingSource(payments, 6), state=state).run_once()
    assert len(state.seen_ids("app1")) == 6

    caplog.clear()
    _poller_for_source(_TotalReportingSource(payments, 5), state=state).run_once()

    assert "narrowed" not in caplog.text


@pytest.mark.parametrize("handled", [20, 126])
def test_a_narrowing_to_a_fraction_of_the_account_still_reports(handled, caplog):
    """The tolerance must not swallow the fault it was added for.

    126 rows collapsing to 1 is the shape of the exclusion this guards against,
    and it has to survive a 5% band. The larger fixture also pins the band against
    the paging cap: with the production ``limit`` of 10 and ``max_pages`` of 5 a
    single cycle never sees more than 50 rows, so a narrow regression has to be
    scaled to what one cycle can actually hold.
    """
    state = InMemoryPollStateStore()
    limit = max(10, handled)
    payments = [make_stub_payment(id=str(i), number=f"00000{i}") for i in range(1, handled + 1)]
    _poller_for_source(
        _TotalReportingSource(payments, handled), state=state, limit=limit
    ).run_once()
    assert len(state.seen_ids("app1")) == handled, "the first cycle must have seeded them"

    caplog.clear()
    _poller_for_source(_TotalReportingSource(payments, 1), state=state, limit=limit).run_once()

    assert "narrowed" in caplog.text


def test_a_source_with_no_total_opinion_is_never_reported(caplog):
    """``None`` means "no opinion", and no opinion must not warn.

    The offline stub and any test double do not report a total. Reading that as a
    count of zero would warn on every rehearsal, which is how a tripwire stops
    being read.
    """
    state = InMemoryPollStateStore()
    payments = [make_stub_payment(id=str(i), number=f"00000{i}") for i in (1, 2)]
    plain = _app("app1", payments)
    _poller_for_source(plain.source, state=state).run_once()

    caplog.clear()
    _poller_for_source(plain.source, state=state).run_once()

    assert "narrowed" not in caplog.text
    assert "came back empty" not in caplog.text
    assert len(StubPaymentSource(payments, status=None).list_payments()) == 2