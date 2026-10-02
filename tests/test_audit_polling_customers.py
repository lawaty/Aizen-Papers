"""Offline pins for the customers half of the polling audit.

Mirrors ``test_audit_polling.py``. That file audits the **invoice** pipeline; the
five engine-level properties it pins are properties of ``DocumentPoller``, so the
customers pipeline inherits all of them and therefore all of their risks. This
file re-establishes each one for the third pipeline, because "it is the same
engine" is exactly the assumption an audit exists to check rather than assume.

Two of the seven are **more** dangerous here, and those are the reason this file
exists at all:

- **Reach is limit x max_pages, and for a welcome that is silent permanent loss.**
  An invoice nobody was told about is an operational problem. A customer nobody was
  welcomed is a relationship, and nothing in the state file distinguishes "not
  reached yet" from "never existed".
- **Listing order is load-bearing, and for clients it is load-bearing *right now*.**
  The invoice listing is newest-first on its own. ``/clients.json`` is **not**: its
  default order is stable but arbitrary — a live account returned ids
  ``[5,1,2,6,4,3]`` — and only ``sort=created&direction=desc`` yields newest-first.
  So for this pipeline the ordering assumption is not an observation about the
  past, it is a live dependency on two request parameters that Daftra silently
  ignores when they are missing or misspelled. Section 5 pins both halves.

The remaining property unique to this pipeline is the **marketising consequence**:
this template is MARKETING category, so a rejected send is not merely a delayed
invoice but an abandoned welcome, and it cannot be rescued by free-form text
outside the 24-hour window.

Fully offline and deterministic. No message is ever sent.
"""

import json

import pytest

from fakes import CapturingSender

from sender.application.poller import CustomerPoller, PollApp
from sender.domain.errors import WhatsAppApiError
from sender.domain.models import KIND_CUSTOMER
from sender.domain.templates import CustomerTemplateBuilder
from sender.infrastructure.state import InMemoryPollStateStore, JsonPollStateStore
from sender.presentation.stubs import Customer, StubCustomerSource

PHONE = "01027693262"
SECOND_PHONE = "011155566677"


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


class ArbitraryOrderSource:
    """A source that reproduces what ``/clients.json`` actually does.

    Two properties, both read-only observed on live accounts and neither of them
    what the engine needs:

    - the default order is a **stable but arbitrary** permutation of the ids (one
      account returned ``[5,1,2,6,4,3]``), not ascending, not descending, and not
      creation order;
    - a newly created record lands at the **tail** of that order, because the
      order is the database's natural one rather than a sort of anything.

    This is what ``/clients.json`` answers with when ``sort=created&direction=desc``
    is missing, so it is the failure mode a regression in the adapter produces.
    """

    def __init__(self, customers, order=None) -> None:
        self._by_id = {str(c.id): c for c in customers}
        self._order = list(order) if order else [str(c.id) for c in customers]
        self.pages_served = 0
        self.fetched_ids: list[str] = []

    def add(self, customer) -> None:
        self._by_id[str(customer.id)] = customer
        self._order.append(str(customer.id))  # natural order: new rows at the tail

    def list_customers(self, limit: int = 10, page: int = 1):
        self.pages_served += 1
        start = (page - 1) * limit
        ids = self._order[start : start + limit]
        self.fetched_ids.extend(ids)
        return [self._by_id[i] for i in ids]

    def get_customer(self, customer_id):
        try:
            return self._by_id[str(customer_id)]
        except KeyError:
            raise ValueError(f"no customer {customer_id!r}") from None

    def get_raw_customer(self, customer_id) -> dict:
        return {}


def _customer(cid: str, phones=(PHONE,), name=None) -> Customer:
    return Customer(
        id=cid,
        number=f"{int(cid):06d}",
        customer_name=name or f"CUST-{cid}",
        customer_phones=tuple(phones),
    )


def _customers(count: int, start: int = 1, phones=(PHONE,)):
    return [_customer(str(i), phones) for i in range(start, start + count)]


def _builder() -> CustomerTemplateBuilder:
    return CustomerTemplateBuilder("aizen_new_customer", "ar_EG", "20")


def _poller(source, sender, state, **kwargs) -> CustomerPoller:
    return CustomerPoller(
        apps=[PollApp(name="app1", source=source)],
        sender=sender,
        builder=_builder(),
        state=state,
        clock=kwargs.pop("clock", None) or FakeClock(),
        **kwargs,
    )


def _run(state_path, source, sender, store_cls=JsonPollStateStore, **kwargs) -> dict:
    """One cycle with a *fresh* poller and store, as a cron ``--once`` run does.

    The only thing carried between cycles is the file on disk, so a fresh store
    per cycle is what makes these tests model the real thing.
    """
    return _poller(source, sender, store_cls(str(state_path)), **kwargs).run_once()


def _greeted(sender) -> set[str]:
    """The customer numbers a message actually went to."""
    return {payload["to"] for payload in sender.payloads}


# --- 1. first-run seeding is a single snapshot --------------------------------


def test_first_run_seeds_every_existing_customer_without_welcoming_them(tmp_path):
    """The behaviour a first deployment depends on: a fresh state must not
    welcome every customer the account has ever had.

    It matters more here than for invoices. An unwanted invoice notification is an
    annoyance; an unwanted welcome to an existing customer is a message telling
    someone they just joined a company they joined years ago.
    """
    path = tmp_path / "poll_customers_state.json"
    source = StubCustomerSource(_customers(3))
    sender = CapturingSender()

    first = _run(path, source, sender)["apps"][0]
    assert first["first_run"] is True
    assert (first["listed"], first["seeded"], first["sent"]) == (3, 3, 0)
    assert sender.payloads == []

    state = JsonPollStateStore(str(path))
    assert sorted(state.seen_ids("app1")) == ["1", "2", "3"]
    # Seeded for good: a later run still sends nothing.
    assert _run(path, source, CapturingSender())["apps"][0]["sent"] == 0


def test_a_customer_created_after_the_seed_snapshot_is_welcomed_next_run(tmp_path):
    """The seed is a snapshot, not a high-water mark that would swallow everything
    up to the newest id it happened to observe."""
    path = tmp_path / "poll_customers_state.json"
    source = StubCustomerSource(_customers(2))
    _run(path, source, CapturingSender())

    source.seed(_customer("3", name="Late Arrival"))
    sender = CapturingSender()
    second = _run(path, source, sender)["apps"][0]

    assert (second["new"], second["sent"]) == (1, 1)
    assert len(sender.payloads) == 1


# --- 2. a customer created mid-cycle is not swallowed --------------------------


def test_a_customer_created_during_the_send_phase_is_not_retired_unsent(tmp_path):
    """State only ever contains ids from the cycle's listing snapshot.

    Written from "everything currently in the source" rather than from the
    snapshot, the newcomer would be silently retired without ever being welcomed.
    """
    path = tmp_path / "poll_customers_state.json"
    source = StubCustomerSource(_customers(1))
    _run(path, source, CapturingSender())

    class _MutatingSender(CapturingSender):
        mutated = False

        def send(self, payload):
            response = super().send(payload)
            if not self.mutated:
                self.mutated = True
                source.seed(_customer("99", name="Mid Cycle"))
            return response

    # ``send_existing`` so the seeded customer is actually sent — otherwise the
    # first run only seeds and no send happens to mutate anything.
    source.seed(_customer("2"))
    first = _MutatingSender()
    _run(path, source, first, send_existing=True)
    assert first.mutated is True
    assert len(first.payloads) == 1

    # The snapshot could not have contained 99, so it must still be unknown.
    assert JsonPollStateStore(str(path)).seen("app1", "99") is False
    second = CapturingSender()
    assert _run(path, source, second)["apps"][0]["sent"] == 1
    assert len(second.payloads) == 1


# --- 3. one atomic write per cycle => at-least-once duplicates -----------------


class ExplodingSaveStore(JsonPollStateStore):
    """A store whose atomic write always fails: a crash after the sends but
    before the single batch-exit persist."""

    def _save(self) -> None:
        raise RuntimeError("crash before the batch-exit state write")


def test_crash_after_send_before_state_write_re_welcomes_next_run(tmp_path):
    """At-least-once, with a real duplicate-send window.

    The seen marks are persisted ONCE, at the batch exit, so a crash between the
    last send and that write loses every mark made in the cycle. The next run
    reads a state file that never learned about them and welcomes the same
    customer again.
    """
    path = tmp_path / "poll_customers_state.json"
    source = StubCustomerSource(_customers(2))
    _run(path, source, CapturingSender())

    source.seed(_customer("3", name="Repeat Me"))
    second = CapturingSender()
    summary = _run(path, source, second, store_cls=ExplodingSaveStore)
    app = summary["apps"][0]

    assert len(second.payloads) == 1, "the send happened"
    assert app["ok"] is False
    assert "crash before the batch-exit state write" in (app["error"] or "")
    # The counts the operator reads are the cycle's real ones, not a zeroed
    # skeleton, so they can still see the customer was messaged.
    assert app["sent"] == 1

    third = CapturingSender()
    assert _run(path, source, third)["apps"][0]["sent"] == 1
    assert len(third.payloads) == 1, "the same customer, welcomed again"


# --- 4. saturation is permanent loss, not a warning ----------------------------


def test_burst_beyond_limit_times_max_pages_is_permanently_missed(tmp_path, caplog):
    """Reach is limit x max_pages, and the rows below it are unreachable forever.

    Paging stops as soon as a page's LAST row is already seen, so once the
    newest of a burst are handled, page 1 is entirely seen territory and the cycle
    never looks further back. The oldest of the burst are not "delayed" — they are
    never fetched again, so they are never welcomed, however many cycles run.

    Moot at 1-6 clients per live account, and the first thing to bite the day a
    tenant imports a customer list.
    """
    path = tmp_path / "poll_customers_state.json"
    source = StubCustomerSource(_customers(50))
    first_sender = CapturingSender()

    with caplog.at_level("WARNING"):
        first = _run(path, source, first_sender, limit=10, max_pages=5)["apps"][0]
    assert (first["seeded"], first["sent"]) == (50, 0)

    for customer in _customers(60, start=51):
        source.seed(customer)
    burst_oldest = [f"CUST-{i}" for i in range(51, 61)]
    burst_newest = [f"CUST-{i}" for i in range(61, 111)]

    second_sender = CapturingSender()
    second = _run(
        path, source, second_sender, limit=10, max_pages=5, send_existing=True, max_sends_per_run=0
    )["apps"][0]
    assert second["sent"] == 50
    greeted = {p["template"]["components"][0]["parameters"][0]["text"].strip("\u2068\u2069")
               for p in second_sender.payloads}
    assert set(burst_newest) == greeted
    assert set(burst_oldest).isdisjoint(greeted)

    third_sender = CapturingSender()
    third = _run(path, source, third_sender, limit=10, max_pages=5, send_existing=True)["apps"][0]
    assert (third["listed"], third["new"], third["sent"]) == (10, 0, 0)
    assert third_sender.payloads == []


# --- 5. the ordering assumption is load-bearing, and is live here --------------


def test_an_arbitrary_listing_order_silently_never_welcomes_a_new_customer():
    """The customers-specific severity of the invoice audit's section 5.

    For invoices this is documentation: the live listing is newest-first, so the
    assumption currently holds. For clients it does not hold on its own — the
    endpoint's default order is a stable but arbitrary permutation, and a new
    record lands at its tail.

    Paging stops at the first page whose last row is already seen, so with the
    default order a brand-new customer sitting behind a seen record on page 1 is
    never fetched and never welcomed. No error, no warning, no state that could
    recover it — and unlike an invoice, nothing anywhere would show up as missing.
    """
    source = ArbitraryOrderSource(_customers(6), order=["5", "1", "2", "6", "4", "3"])
    state = InMemoryPollStateStore()

    # First run seeds everything, exactly as it would in production.
    first = _poller(source, CapturingSender(), state).run_once()["apps"][0]
    assert first["seeded"] == 6
    assert sorted(state.seen_ids("app1")) == ["1", "2", "3", "4", "5", "6"]

    # A new customer signs up. Under the arbitrary default order it lands last.
    source.add(_customer("7", name="Never Welcomed"))
    pages_before = source.pages_served
    sender = CapturingSender()
    second = _poller(source, sender, state, limit=4, max_pages=5).run_once()["apps"][0]

    assert source.pages_served - pages_before == 1, "paging stopped on a seen row"
    assert (second["new"], second["sent"]) == (0, 0)
    assert sender.payloads == []
    assert "7" not in source.fetched_ids
    assert state.seen("app1", "7") is False


def test_the_newest_first_order_is_requested_and_pinned():
    """The mitigation for the section above, pinned at the seam that can break it.

    ``DaftraClient.list_customers`` must send ``sort=created&direction=desc``, and
    it must send exactly that: the endpoint silently ignores ``order=desc``,
    ``sort=-created`` and every unknown parameter, answering 200 with whatever it
    felt like. A dropped or misspelled parameter therefore reproduces the silent
    total failure above while every test that stubs the source still passes.

    This assertion is deliberately on the **request params**, not on the resulting
    order, because the order is the adapter's private business and the params are
    the contract with Daftra.
    """
    from sender.infrastructure.daftra.client import DaftraClient
    from fakes import FakeResponse

    captured: dict = {}

    class _Session:
        headers: dict = {}

        def get(self, url: str, **kwargs):
            captured.update(kwargs.get("params") or {})
            return FakeResponse(200, {"data": []})

    DaftraClient(api_key="k", session=_Session()).list_customers(limit=5, page=2)
    assert captured == {"page": 2, "limit": 5, "sort": "created", "direction": "desc"}


# --- 6. the 0-byte lock file is an anchor, not a stale lock --------------------


def test_preexisting_zero_byte_lock_file_does_not_block_a_new_poller(tmp_path):
    """Each pipeline locks its own ``<state>.lock``, and the file existing proves
    nothing about whether a poller holds it: ``flock`` is advisory and owned by
    the open file description, so the kernel releases it when the holder exits.
    An operator that sees the file must not delete it as a "stale lock"."""
    from sender.infrastructure.state import PollStateLock

    state_path = tmp_path / "poll_customers_state.json"
    lock_file = tmp_path / "poll_customers_state.json.lock"
    lock_file.write_bytes(b"")
    assert lock_file.exists() and lock_file.stat().st_size == 0

    lock = PollStateLock(str(state_path))
    lock.acquire()
    lock.release()

    held = PollStateLock(str(state_path))
    held.acquire()
    try:
        with pytest.raises(RuntimeError, match="another poller is already running"):
            PollStateLock(str(state_path)).acquire()
    finally:
        held.release()


# --- 7. the MARKETING consequence --------------------------------------------


def test_a_template_rejection_costs_twice_as_much_when_the_fallback_is_forced_on():
    """The property that is specific to this template's category, and the reason
    ``WHATSAPP_CUSTOMER_FREEFORM_FALLBACK`` defaults to off.

    ``aizen_new_customer`` is MARKETING and every welcome goes to a brand-new
    number, which is by definition outside the 24-hour customer-service window
    where free-form text is the only thing deliverable. So when the template is
    rejected with a ``132000``-series code, the free-form rescue is **also**
    rejected — with ``131047``, "outside window" — and the customer is pending on
    the template error either way.

    Enabling the fallback therefore buys nothing and doubles the POSTs against
    Meta. That is the whole argument for the separate knob, pinned here with the
    real two-step failure rather than asserted in prose.
    """

    class _ChainFailingSender(CapturingSender):
        """Template sends rejected 132000; the free-form rescue rejected 131047."""

        def send(self, payload):
            self.payloads.append(payload)
            if payload["type"] == "template":
                raise WhatsAppApiError(
                    132000, "template parameter mismatch", {"error": {"code": 132000}}
                )
            raise WhatsAppApiError(
                131047, "outside the customer service window", {"error": {"code": 131047}}
            )

    def _run_with(fallback: bool):
        state = InMemoryPollStateStore()
        sender = _ChainFailingSender()
        app = _poller(
            StubCustomerSource(_customers(2)), sender, state,
            send_existing=True, freeform_fallback=fallback,
        ).run_once()["apps"][0]
        return app, sender, state

    off_app, off_sender, off_state = _run_with(False)
    assert [p["type"] for p in off_sender.payloads] == ["template", "template"]
    # Nothing was consumed: pending on the template error, so approving the
    # template is what makes the next attempt succeed.
    assert (off_app["sent"], off_app["abandoned"]) == (0, 0)
    assert sorted(off_state.pending("app1")) == ["1", "2"]

    on_app, on_sender, on_state = _run_with(True)
    assert [p["type"] for p in on_sender.payloads] == [
        "template", "text", "template", "text",
    ], "each customer burns a second, doomed free-form POST"
    # Identical outcome, double the cost.
    assert (on_app["sent"], on_app["abandoned"]) == (0, 0)
    assert sorted(on_state.pending("app1")) == ["1", "2"]


def test_a_rejected_number_is_abandoned_and_never_welcomed():
    """A brand-new number may simply not be on WhatsApp yet. That is terminal and
    un-rescuable by any fallback, so the customer is abandoned once and left
    visible for an operator rather than retried forever."""
    class _NotOnWhatsApp(CapturingSender):
        def send(self, payload):
            self.payloads.append(payload)
            raise WhatsAppApiError(
                131047, "not on WhatsApp", {"error": {"code": 131047}}
            )

    state = InMemoryPollStateStore()
    sender = _NotOnWhatsApp()
    app = _poller(
        StubCustomerSource(_customers(2)), sender, state,
        send_existing=True, freeform_fallback=True,
    ).run_once()["apps"][0]

    assert (app["sent"], app["abandoned"], app["pending"]) == (0, 2, 0)
    # 131047 is not a template rejection, so the free-form rescue is never even
    # attempted: one POST each, not two.
    assert len(sender.payloads) == 2
    assert sorted(state.abandoned("app1")) == ["1", "2"]


def test_the_summary_reports_the_customer_kind():
    """So an operator reading a cycle is not told they looked at invoices."""
    state = InMemoryPollStateStore()
    app = _poller(
        StubCustomerSource(_customers(1)), CapturingSender(), state, send_existing=True
    ).run_once()["apps"][0]
    assert app["kind"] == KIND_CUSTOMER


def test_a_multi_number_customer_is_welcomed_once_per_number():
    """The fan-out is per *number*, and the numbers are the client's, not the
    document's: a customer who gave two numbers is contacted on both."""
    source = StubCustomerSource([_customer("1", phones=(PHONE, SECOND_PHONE))])
    sender = CapturingSender()
    app = _poller(source, sender, InMemoryPollStateStore(), send_existing=True).run_once()["apps"][0]

    assert app["sent"] == 1, "one document"
    assert _greeted(sender) == {"201027693262", "2011155566677"}, "two POSTs"