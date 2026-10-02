"""Offline specs for the customers polling pipeline.

Mirrors ``test_payment_poller.py``. The point of these tests is *not* that
customers get their own coverage — it is that the shared engine's invariants hold
for the third caller as well.

The one thing that genuinely differs from the other two pipelines is pinned here:
a client listing row carries its own name and phone, so the inherited
``_needs_detail`` default means a normal row needs **no** detail fetch at all.
That is the opposite of payments (always one) and it is the property that makes
this pipeline cheaper — so it is asserted directly rather than assumed.
"""

from fakes import CapturingSender, FailingSender
from sender.application.poller import CustomerPoller, DocumentPoller, PollApp
from sender.domain.errors import DaftraApiError, WhatsAppApiError
from sender.domain.models import KIND_CUSTOMER
from sender.domain.templates import CustomerTemplateBuilder
from sender.infrastructure.state import InMemoryPollStateStore
from sender.presentation.stubs import (
    StubCustomerSource,
    default_stub_customers,
    make_stub_customer,
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

    def list_customers(self, limit: int = 10, page: int = 1):
        raise self.error

    def get_customer(self, customer_id):
        raise self.error

    def get_raw_customer(self, customer_id):
        raise self.error


class CountingCustomerSource:
    """Lists whatever it is given and counts the detail fetches."""

    def __init__(self, listed, detail=None) -> None:
        self.listed = listed
        self.detail = detail
        self.detail_calls: list[str] = []

    def list_customers(self, limit: int = 10, page: int = 1):
        return list(self.listed)

    def get_customer(self, customer_id):
        self.detail_calls.append(str(customer_id))
        return self.detail

    def get_raw_customer(self, customer_id):
        return {}


class RecordingRecorder:
    def __init__(self) -> None:
        self.rows: list = []

    def record(self, outcome) -> None:
        self.rows.append(outcome)


def _builder() -> CustomerTemplateBuilder:
    return CustomerTemplateBuilder(
        template_name="aizen_new_customer", language="ar_EG", country_code="20"
    )


def _app(name: str = "app1", customers=None) -> PollApp:
    return PollApp(
        name=name,
        source=StubCustomerSource(
            customers if customers is not None else default_stub_customers()
        ),
    )


def _poller(apps, sender=None, state=None, clock=None, **kwargs) -> CustomerPoller:
    return CustomerPoller(
        apps=apps,
        sender=sender,
        builder=_builder(),
        state=state or InMemoryPollStateStore(),
        clock=clock or FakeClock(),
        **kwargs,
    )


def _app_result(summary):
    return summary["apps"][0]


def _greeted(sender: CapturingSender) -> set[str]:
    """The names that went out, with the bidi isolation marks stripped."""
    names = set()
    for payload in sender.payloads:
        text = payload["template"]["components"][0]["parameters"][0]["text"]
        names.add(text.strip("⁨⁩"))
    return names


# --- the happy path ----------------------------------------------------------


def test_a_new_customer_is_welcomed_on_the_number_on_the_row():
    sender = CapturingSender()
    app = _app_result(_poller([_app()], sender=sender, send_existing=True).run_once())
    assert (app["listed"], app["new"], app["sent"]) == (3, 3, 2)
    # 000003 is the no-phone fixture, so it is skipped and retired, not retried.
    assert app["skipped_no_phone"] == 1
    assert {p["to"] for p in sender.payloads} == {"201027693262", "201234567890"}
    assert _greeted(sender) == {"Print Home", "Mona Farouk"}


def test_the_summary_reports_the_customer_kind():
    """So ``_format_poll_summary`` and the log line say "customer", not "invoice"."""
    assert _app_result(_poller([_app()], sender=CapturingSender()).run_once())["kind"] == KIND_CUSTOMER


def test_a_seen_customer_is_not_welcomed_again():
    state = InMemoryPollStateStore()
    sender = CapturingSender()
    _poller([_app()], sender=sender, state=state, send_existing=True).run_once()
    assert len(sender.payloads) == 2
    again = _app_result(_poller([_app()], sender=CapturingSender(), state=state).run_once())
    assert (again["new"], again["sent"]) == (0, 0)


def test_the_first_run_seeds_customers_without_sending():
    """The highest-risk behaviour in the feature: a fresh state must not welcome
    every customer the account has ever had."""
    state = InMemoryPollStateStore()
    sender = CapturingSender()
    app = _app_result(_poller([_app()], sender=sender, state=state).run_once())
    assert app["first_run"] is True
    assert app["seeded"] == 3
    assert app["sent"] == 0
    assert sender.payloads == []
    # Seeded for good: the customers that were already there are now retired.
    assert len(state.seen_ids("app1")) == 3
    again = _app_result(_poller([_app()], sender=CapturingSender(), state=state).run_once())
    assert again["sent"] == 0


def test_only_a_newly_added_customer_is_welcomed():
    source = StubCustomerSource(default_stub_customers())
    state = InMemoryPollStateStore()
    sender = CapturingSender()
    poller = _poller([PollApp(name="app1", source=source)], sender=sender, state=state)
    poller.run_once()
    source.add_new_customer(customer_name="New Co", customer_phone="01099887766")
    app = _app_result(poller.run_once())
    assert (app["new"], app["sent"]) == (1, 1)
    assert _greeted(sender) == {"New Co"}


# --- the detail-fetch policy, which is what makes this pipeline cheap --------


def test_the_detail_rule_is_inherited_not_reimplemented():
    """If this ever stops being the base rule, the payments subclass's always-fetch
    policy would be a copy-paste hazard rather than a deliberate override."""
    assert CustomerPoller._needs_detail is DocumentPoller._needs_detail
    assert "_needs_detail" not in CustomerPoller.__dict__


def test_a_listing_row_that_already_has_a_phone_needs_no_detail_fetch():
    poller = _poller([])
    assert poller._needs_detail(make_stub_customer(customer_phone="01027693262")) is False


def test_a_listing_row_without_a_phone_triggers_exactly_one_detail_fetch():
    poller = _poller([])
    assert poller._needs_detail(make_stub_customer(customer_phone=None)) is True


def test_a_normal_run_makes_no_detail_calls_at_all():
    """The contrast with payments, which always need a second hop to find the payer."""
    source = CountingCustomerSource([make_stub_customer(id="1", customer_phone="01027693262")])
    _poller([PollApp(name="app1", source=source)], sender=CapturingSender()).run_once()
    assert source.detail_calls == []


# --- failures ----------------------------------------------------------------


def test_a_retryable_failure_is_retried_and_never_given_up():
    """A customer whose send hit a transient error must still get their welcome
    once the template/Meta recovers, so the record stays pending."""
    error = WhatsAppApiError(503, "upstream", {"error": {"code": 5}})
    state = InMemoryPollStateStore()
    sender = FailingSender(error)
    app = _app_result(_poller([_app()], sender=sender, state=state, send_existing=True).run_once())
    assert (app["sent"], app["abandoned"]) == (0, 0)
    assert not state.seen("app1", "2"), "a retryable failure must never retire the customer"
    assert state.pending("app1")


def test_a_permanent_failure_is_abandoned_immediately():
    sender = FailingSender(WhatsAppApiError(131047, "outside window", {"error": {"code": 131047}}))
    state = InMemoryPollStateStore()
    app = _app_result(_poller([_app()], sender=sender, state=state, send_existing=True).run_once())
    assert (app["sent"], app["pending"], app["abandoned"]) == (0, 0, 2)
    assert state.abandoned("app1"), "and must stay visible for an operator"


def test_a_listing_failure_does_not_stop_the_other_apps():
    apps = [
        PollApp(name="broken", source=FailingSource(DaftraApiError(500, "boom"))),
        _app("ok"),
    ]
    by_app = {
        app["app"]: app
        for app in _poller(apps, sender=CapturingSender(), send_existing=True).run_once()["apps"]
    }
    assert by_app["broken"]["ok"] is False
    assert by_app["ok"]["sent"] == 2


def test_a_customer_with_no_phone_is_skipped_and_retired():
    """Nobody can be reached about a customer with no number, so it is skipped and
    marked seen rather than retried forever."""
    state = InMemoryPollStateStore()
    app = _app_result(
        _poller([_app()], sender=CapturingSender(), state=state, send_existing=True).run_once()
    )
    assert app["skipped_no_phone"] == 1
    assert "3" in state.seen_ids("app1")


# --- caps and dry runs -------------------------------------------------------


def test_the_send_cap_leaves_the_rest_unseen_rather_than_dropping_it():
    state = InMemoryPollStateStore()
    sender = CapturingSender()
    app = _app_result(
        _poller(
            [_app()], sender=sender, state=state, max_sends_per_run=1, send_existing=True
        ).run_once()
    )
    # 000003 is unphoneable, so only two are sendable and the cap takes one of them.
    assert (app["listed"], app["new"], app["sent"], app["skipped_no_phone"]) == (3, 2, 1, 1)
    # Each later cycle takes exactly one more; nothing is dropped, only deferred.
    assert _app_result(_poller([_app()], sender=sender, state=state).run_once())["sent"] == 1
    assert _app_result(_poller([_app()], sender=sender, state=state).run_once())["sent"] == 0


def test_a_zero_cap_disables_the_limit():
    sender = CapturingSender()
    app = _app_result(
        _poller([_app()], sender=sender, max_sends_per_run=0, send_existing=True).run_once()
    )
    assert app["sent"] == 2


def test_a_dry_run_sends_nothing_and_writes_no_state():
    sender = CapturingSender()
    state = InMemoryPollStateStore()
    app = _app_result(
        _poller([_app()], sender=sender, state=state, dry_run=True, send_existing=True).run_once()
    )
    assert app["sent"] == 0
    assert app["would_send"] == 2
    assert sender.payloads == []
    assert not state.seen_ids("app1"), "a dry run must not even record them as seen"


# --- reporting ---------------------------------------------------------------


def test_a_sent_customer_is_recorded_with_the_customer_kind():
    recorder = RecordingRecorder()
    _poller([_app()], sender=CapturingSender(), send_existing=True, recorder=recorder).run_once()
    assert recorder.rows
    assert {row.kind for row in recorder.rows} == {KIND_CUSTOMER}
    sent = [row for row in recorder.rows if row.status == "sent"]
    assert {row.invoice_number for row in sent} == {"000001", "000002"}
    assert {row.customer_name for row in sent} == {"Print Home", "Mona Farouk"}
    # The report's date column carries the account creation date, which is what
    # makes a "new customer" claim checkable after the fact.
    assert all(row.issue_date is not None for row in sent)


def test_a_dry_run_records_nothing():
    recorder = RecordingRecorder()
    _poller(
        [_app()], sender=CapturingSender(), dry_run=True, send_existing=True, recorder=recorder
    ).run_once()
    assert recorder.rows == []
