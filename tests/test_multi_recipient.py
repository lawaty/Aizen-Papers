"""Fan-out to every number on a customer, and finishing a partial delivery.

Daftra keeps two phone fields on a client and either may be filled, so a document
is addressed to a *set* of numbers rather than to one. These tests pin the three
things that makes hard:

1. every distinct usable number is reached, and a repeated number is one message;
2. a document that reached only some of its numbers is **not** retired — it is
   finished by a later cycle, which must not message the numbers that already
   got it;
3. the per-run send cap bounds the number of POSTs, so it can bite *between* two
   recipients of the same document, and that must defer rather than drop.

Everything here runs against the shared ``DocumentPoller`` engine, so the same
guarantees hold for the payment and customer pipelines, which is why the
payment/customer cases are exercised too.
"""

from __future__ import annotations

import pytest

from fakes import CapturingSender
from sender.application.poller import (
    CustomerPoller,
    InvoicePoller,
    PaymentPoller,
    PollApp,
)
from sender.domain.errors import WhatsAppApiError
from sender.domain.models import KIND_CUSTOMER, KIND_PAYMENT
from sender.domain.templates import (
    CustomerTemplateBuilder,
    LegacyInvoiceTemplateBuilder,
    PaymentTemplateBuilder,
)
from sender.infrastructure.state import InMemoryPollStateStore
from sender.presentation.stubs import (
    StubCustomerSource,
    StubInvoiceSource,
    StubPaymentSource,
    make_stub_customer,
    make_stub_invoice,
    make_stub_payment,
)

PRIMARY = "01027693262"      # normalizes to 201027693262
SECONDARY = "01115556677"    # normalizes to 201115556677


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def monotonic(self) -> float:
        return self.value

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class FailingOnNumberSender:
    """Succeeds for everyone except the numbers it was told to fail.

    Failing *by recipient* is what makes the partial-delivery cases expressible:
    an invoice-level sender cannot distinguish "the second number was rejected"
    from "the invoice was rejected".
    """

    def __init__(self, failing: set[str], error: Exception | None = None) -> None:
        self.failing = set(failing)
        self.error = error or WhatsAppApiError(503, "upstream unavailable")
        self.payloads: list[dict] = []

    def send(self, payload: dict) -> dict:
        self.payloads.append(payload)
        if payload["to"] in self.failing:
            raise self.error
        return {"messages": [{"id": "wamid.FAKE"}]}


def _builder():
    return LegacyInvoiceTemplateBuilder(
        template_name="aizen_invoice", language="en", country_code="20"
    )


def _two_number_invoice() -> object:
    return make_stub_invoice(customer_phones=(PRIMARY, SECONDARY))


def _invoice_app(invoice=None) -> PollApp:
    return PollApp(name="app1", source=StubInvoiceSource([invoice or _two_number_invoice()]))


def _poller(app, sender, state, clock, **kwargs) -> InvoicePoller:
    return InvoicePoller(
        apps=[app], sender=sender, builder=_builder(), state=state, clock=clock, **kwargs
    )


def _sent_to(sender) -> list[str]:
    return [payload["to"] for payload in sender.payloads]


# --- every usable number is reached -----------------------------------------


def test_an_invoice_with_two_numbers_is_sent_to_both():
    """The headline behaviour: two filled fields, two messages."""
    sender = CapturingSender()
    state = InMemoryPollStateStore()
    poller = _poller(_invoice_app(), sender, state, FakeClock(), send_existing=True)

    app = poller.run_once()["apps"][0]

    assert _sent_to(sender) == ["201027693262", "201115556677"]
    assert app["sent"] == 1, "one document, even though two messages went out"
    assert state.seen("app1", "1") is True


def test_one_filled_number_still_sends_once():
    sender = CapturingSender()
    poller = _poller(
        _invoice_app(make_stub_invoice(customer_phones=(PRIMARY,))),
        sender, InMemoryPollStateStore(), FakeClock(), send_existing=True,
    )

    app = poller.run_once()["apps"][0]

    assert _sent_to(sender) == ["201027693262"]
    assert app["sent"] == 1


def test_the_same_number_in_both_fields_is_one_message():
    """Two fields holding one number must not message one person twice."""
    sender = CapturingSender()
    poller = _poller(
        _invoice_app(make_stub_invoice(customer_phones=(PRIMARY, "+201027693262"))),
        sender, InMemoryPollStateStore(), FakeClock(), send_existing=True,
    )

    poller.run_once()

    assert _sent_to(sender) == ["201027693262"]


def test_a_second_number_needs_no_extra_source_request():
    """A listing row carrying one number is complete enough to send from.

    ``_needs_detail`` used to be satisfied by "has a phone"; it must stay that
    way, or a two-number account would cost a detail fetch on every cycle for
    information the listing already had.
    """
    app = _invoice_app()
    poller = _poller(app, CapturingSender(), InMemoryPollStateStore(), FakeClock())

    assert poller._needs_detail(make_stub_invoice(customer_phones=(PRIMARY,))) is False
    assert poller._needs_detail(make_stub_invoice(customer_phones=())) is True


# --- a partial delivery is finished, not dropped and not repeated ------------


def test_a_failed_second_number_keeps_the_document_unseen_and_is_retried_to_only_that_number():
    """The core guarantee: nobody is messaged twice, nobody is skipped.

    The first number succeeded, so a naive retry would send the invoice to it
    again. The document must also stay unseen, because marking it seen would
    retire the second number forever.
    """
    sender = FailingOnNumberSender({"201115556677"})
    state = InMemoryPollStateStore()
    clock = FakeClock()
    poller = _poller(_invoice_app(), sender, state, clock, send_existing=True)

    first = poller.run_once()["apps"][0]

    assert _sent_to(sender) == ["201027693262", "201115556677"]
    assert state.seen("app1", "1") is False, "must not be retired while a number is outstanding"
    assert state.delivered("app1", "1") == ["201027693262"]
    assert first["failed"] == 1, "reported as needing another cycle, not as sent"
    assert first["invoices"][0]["status"] == "failed"

    # The backoff defers the retry; a later cycle with time advanced picks it up.
    clock.value += 10_000
    sender.failing.clear()
    poller._sends_this_cycle = 0
    second = poller.run_once()["apps"][0]

    assert _sent_to(sender) == ["201027693262", "201115556677", "201115556677"]
    assert state.seen("app1", "1") is True
    assert state.delivered("app1", "1") == [], "nothing left behind once retired"
    assert second["sent"] == 1


def test_a_permanently_failing_number_gives_the_whole_document_up():
    """A permanent rejection on one number must not re-send the other forever.

    It is abandoned rather than retried, which is also what retires the document
    and clears the delivered record.
    """
    sender = FailingOnNumberSender(
        {"201115556677"}, error=WhatsAppApiError(400, "invalid recipient")
    )
    state = InMemoryPollStateStore()
    poller = _poller(_invoice_app(), sender, state, FakeClock(), send_existing=True)

    app = poller.run_once()["apps"][0]

    assert _sent_to(sender) == ["201027693262", "201115556677"]
    assert app["abandoned"] == 1
    assert state.seen("app1", "1") is True
    assert state.delivered("app1", "1") == []


def test_an_already_fully_delivered_document_is_retired_without_resending():
    """The resume path must terminate.

    A delivered record with every number in it means a previous cycle finished the
    work and died before retiring the document; re-sending would duplicate every
    message in the fan-out.
    """
    sender = CapturingSender()
    state = InMemoryPollStateStore()
    state.record_delivered("app1", "1", ["201027693262", "201115556677"])
    poller = _poller(_invoice_app(), sender, state, FakeClock(), send_existing=True)

    poller.run_once()

    assert sender.payloads == []
    assert state.seen("app1", "1") is True


def test_a_dry_run_delivers_nothing_and_writes_no_state():
    """Invariant 2 still holds with several recipients: no delivered record."""
    sender = CapturingSender()
    state = InMemoryPollStateStore()
    poller = _poller(
        _invoice_app(), sender, state, FakeClock(), send_existing=True, dry_run=True
    )

    app = poller.run_once()["apps"][0]

    assert sender.payloads == []
    assert app["would_send"] == 1
    assert state.delivered("app1", "1") == []
    assert state.seen("app1", "1") is False


# --- the cap bounds POSTs, so it can bite between two recipients -------------


def test_each_recipient_costs_one_unit_of_the_send_cap():
    """Two numbers is two POSTs, so the cap must see two — otherwise it stops
    bounding what actually reaches Meta."""
    sender = CapturingSender()
    poller = _poller(
        _invoice_app(), sender, InMemoryPollStateStore(), FakeClock(),
        send_existing=True, max_sends_per_run=2,
    )

    app = poller.run_once()["apps"][0]

    assert _sent_to(sender) == ["201027693262", "201115556677"]
    assert app["sent"] == 1


def test_the_cap_between_two_recipients_defers_the_rest_without_losing_either():
    """The cap can land mid-document. The second number must survive the cycle.

    It stays unseen and its number is remembered, so the next cycle sends only
    what is left — and the summary must call it deferred, not failed.
    """
    sender = CapturingSender()
    state = InMemoryPollStateStore()
    poller = _poller(
        _invoice_app(), sender, state, FakeClock(),
        send_existing=True, max_sends_per_run=1,
    )

    app = poller.run_once()["apps"][0]

    assert _sent_to(sender) == ["201027693262"]
    assert state.seen("app1", "1") is False
    assert state.delivered("app1", "1") == ["201027693262"]
    assert app["deferred_by_cap"] == 1
    assert app["new"] == 0, "a deferred document was not handled this cycle"
    assert app["failed"] == 0
    assert app["invoices"][0]["status"] == "deferred_by_cap"
    assert app["invoices"][0]["remaining"] == ["201115556677"]

    # A later cycle with a bigger budget finishes the job, and only the remainder.
    poller._max_sends_per_run = 10
    poller._sends_this_cycle = 0
    poller.run_once()

    assert _sent_to(sender) == ["201027693262", "201115556677"]
    assert state.seen("app1", "1") is True


# --- the guarantee is the engine's, so the twin pipelines get it too ---------


def test_a_payment_reaches_both_of_the_linked_clients_numbers():
    """A payment reads its phones from the linked invoice's client, so it fans out
    exactly as the invoice does — the shared engine, not a special case."""
    sender = CapturingSender()
    payment = make_stub_payment(customer_phones=(PRIMARY, SECONDARY))
    app = PollApp(name="app1", source=StubPaymentSource([payment]))
    poller = PaymentPoller(
        apps=[app], sender=sender,
        builder=PaymentTemplateBuilder(
            template_name="aizen_new_payment", language="ar_EG", country_code="20"
        ),
        state=InMemoryPollStateStore(), clock=FakeClock(), send_existing=True,
    )

    result = poller.run_once()["apps"][0]

    assert _sent_to(sender) == ["201027693262", "201115556677"]
    assert result["sent"] == 1
    assert result["kind"] == KIND_PAYMENT


def test_a_new_customer_welcome_reaches_both_numbers():
    sender = CapturingSender()
    customer = make_stub_customer(customer_phones=(PRIMARY, SECONDARY))
    app = PollApp(name="app1", source=StubCustomerSource([customer]))
    poller = CustomerPoller(
        apps=[app], sender=sender,
        builder=CustomerTemplateBuilder(
            template_name="aizen_new_customer", language="ar_EG", country_code="20"
        ),
        state=InMemoryPollStateStore(), clock=FakeClock(), send_existing=True,
    )

    result = poller.run_once()["apps"][0]

    assert _sent_to(sender) == ["201027693262", "201115556677"]
    assert result["kind"] == KIND_CUSTOMER


def test_a_report_row_is_written_per_recipient():
    """The report is the audit trail of what reached customers, so one number is
    one row — an operator reading it must be able to see both sends."""
    rows: list = []

    class RecordingRecorder:
        """The recorder port, reduced to capturing what it is handed."""

        def record(self, outcome) -> None:
            rows.append(outcome)

    store = RecordingRecorder()
    sender = CapturingSender()
    poller = _poller(
        _invoice_app(), sender, InMemoryPollStateStore(), FakeClock(),
        send_existing=True, recorder=store,
    )

    poller.run_once()

    assert [row.customer_phone for row in rows] == ["201027693262", "201115556677"]
    assert {row.invoice_id for row in rows} == {"1"}
    assert all(row.ok for row in rows)

# --- the manual commands fan out too, and --to still overrides ---------------


#: Already-normalized, because the service layer deliberately does *not* normalize:
#: it builds payloads from the model and hands them to the WhatsApp client, which
#: normalizes at the boundary. Normalizing twice here would hide that split.
SERVICE_PRIMARY = "201027693262"
SERVICE_SECONDARY = "201115556677"


def _service(sender, invoice=None):
    from sender.application.services import InvoiceNotificationService

    return InvoiceNotificationService(
        source=StubInvoiceSource(
            [invoice or make_stub_invoice(customer_phones=(SERVICE_PRIMARY, SERVICE_SECONDARY))]
        ),
        sender=sender,
        builder=LegacyInvoiceTemplateBuilder(
            template_name="aizen_invoice", language="en", country_code="20"
        ),
    )


def test_a_manual_send_delivers_to_both_numbers():
    sender = CapturingSender()

    result = _service(sender).send_invoice("1")

    assert _sent_to(sender) == [SERVICE_PRIMARY, SERVICE_SECONDARY]
    assert result["recipients"] == [SERVICE_PRIMARY, SERVICE_SECONDARY]
    assert len(result["payloads"]) == 2
    assert len(result["responses"]) == 2


def test_an_explicit_to_sends_to_that_number_alone():
    """``--to`` is the operator overriding the record, so it stays literal — and
    stays singular, however many numbers the customer has on file."""
    sender = CapturingSender()

    result = _service(sender).send_invoice("1", to_phone="+201234567890")

    assert _sent_to(sender) == ["201234567890"]
    assert result["recipients"] == ["+201234567890"]


def test_a_manual_dry_run_builds_a_payload_per_number_and_sends_nothing():
    sender = CapturingSender()

    result = _service(sender).send_invoice("1", dry_run=True)

    assert sender.payloads == []
    assert result["dry_run"] is True
    assert result["recipients"] == [SERVICE_PRIMARY, SERVICE_SECONDARY]
    assert [payload["to"] for payload in result["payloads"]] == [
        SERVICE_PRIMARY, SERVICE_SECONDARY,
    ]


def test_the_builder_retry_does_not_remessage_a_delivered_number():
    """The retry that recovers from a template rejection must not duplicate.

    ``exclude`` is how the CLI achieves that: the numbers the first attempt
    delivered to are passed back in, so only the outstanding one is re-sent.
    """
    from sender.domain.errors import WhatsAppApiError

    class RejectsFirstSend:
        """Fails the first POST with a template error, then accepts."""

        def __init__(self) -> None:
            self.payloads: list[dict] = []

        def send(self, payload: dict) -> dict:
            self.payloads.append(payload)
            if len(self.payloads) == 1:
                raise WhatsAppApiError(400, "template mismatch", {"error": {"code": 132000}})
            return {"messages": [{"id": "wamid.FAKE"}]}

    sender = RejectsFirstSend()
    service = _service(sender)

    with pytest.raises(WhatsAppApiError):
        service.send_invoice("1")

    assert service.delivered_recipients == ()

    result = service.send_invoice("1", exclude=service.delivered_recipients)

    assert result["recipients"] == [SERVICE_PRIMARY, SERVICE_SECONDARY]


def test_a_send_that_fails_part_way_reports_only_what_reached_the_customer():
    """``delivered_recipients`` is read in the retry's except branch, so it must
    name the numbers Meta accepted — not the one it refused."""
    from sender.domain.errors import WhatsAppApiError

    sender = FailingOnNumberSender({SERVICE_SECONDARY})
    service = _service(sender)

    with pytest.raises(WhatsAppApiError):
        service.send_invoice("1")

    assert service.delivered_recipients == ("201027693262",)
