"""The payments pipeline's behaviour when its template is unusable.

Mirrors ``test_freeform_fallback.py``. The invoice pipeline has a header document
to reuse when a template send is rejected; a payment template has no header, so
the fallback here is always the plain-text one. That difference is the whole
point of these tests: the shared engine's fallback must pick the right one for a
pipeline whose payload shape has no header at all.
"""

from sender.application.poller import PaymentPoller, PollApp
from sender.domain.errors import WhatsAppApiError
from sender.domain.templates import PaymentTemplateBuilder
from sender.infrastructure.state import InMemoryPollStateStore
from sender.presentation.cli import _format_poll_summary
from sender.presentation.stubs import StubPaymentSource, make_stub_payment


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def monotonic(self) -> float:
        return self.value

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _template_error() -> WhatsAppApiError:
    return WhatsAppApiError(132001, "Template does not exist", {"error": {"code": 132001}})


class TemplateErrorSender:
    """Fails the template send, then accepts the free-form one."""

    def __init__(self, template_error: Exception) -> None:
        self.template_error = template_error
        self.payloads: list[dict] = []

    def send(self, payload: dict) -> dict:
        self.payloads.append(payload)
        if len(self.payloads) == 1:
            raise self.template_error
        return {"messages": [{"id": "wamid.FALLBACK"}]}


class AlwaysFailingSender:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.payloads: list[dict] = []

    def send(self, payload: dict) -> dict:
        self.payloads.append(payload)
        raise self.error


def _app(payment=None) -> PollApp:
    return PollApp("app1", StubPaymentSource([payment or make_stub_payment()], status=None))


def _poller(apps, sender, state=None, **kwargs) -> PaymentPoller:
    return PaymentPoller(
        apps=apps,
        sender=sender,
        builder=PaymentTemplateBuilder("aizen_new_payment", "ar_EG", "20"),
        state=state or InMemoryPollStateStore(),
        clock=FakeClock(),
        **kwargs,
    )


def test_a_template_error_falls_back_to_the_freeform_text():
    """There is no header document on this template, so the fallback is text —
    never a document message built from a header that was never there."""
    builder = PaymentTemplateBuilder("aizen_new_payment", "ar_EG", "20")
    sender = TemplateErrorSender(_template_error())
    state = InMemoryPollStateStore()
    summary = _poller(
        [_app()], sender, state=state, send_existing=True
    ).run_once()["apps"][0]

    assert summary["sent"] == 1
    assert summary["fallback_sends"] == 1
    assert len(sender.payloads) == 2
    assert sender.payloads[0]["type"] == "template"
    assert sender.payloads[1]["type"] == "text"
    assert sender.payloads[1] == builder.build_text(make_stub_payment(), "201027693262")
    # It counts as delivered and is retired, so the customer is not told twice.
    assert state.seen("app1", "1")


def test_the_fallback_row_records_the_template_error_not_the_fallbacks():
    recorded = []

    class _Recorder:
        def record(self, outcome):
            recorded.append(outcome)

    _poller(
        [_app()],
        TemplateErrorSender(_template_error()),
        recorder=_Recorder(),
        send_existing=True,
    ).run_once()
    assert len(recorded) == 1
    assert recorded[0].fallback is True
    assert recorded[0].status == "sent"
    assert "132001" in recorded[0].error


def test_a_disabled_fallback_keeps_the_payment_pending():
    """A bridge must not consume payments: with the fallback off, the payment
    stays pending on the template error so approving the template brings it back."""
    state = InMemoryPollStateStore()
    summary = _poller(
        [_app()],
        AlwaysFailingSender(_template_error()),
        state=state,
        freeform_fallback=False,
        send_existing=True,
    ).run_once()["apps"][0]
    assert summary["failed"] == 1
    assert summary["abandoned"] == 0
    assert not state.seen("app1", "1")
    assert state.pending("app1")


def test_a_failing_fallback_keeps_the_payment_pending_on_the_template_error(caplog):
    state = InMemoryPollStateStore()
    sender = AlwaysFailingSender(_template_error())
    with caplog.at_level("WARNING"):
        summary = _poller(
            [_app()], sender, state=state, send_existing=True
        ).run_once()["apps"][0]
    assert summary["failed"] == 1
    assert not state.seen("app1", "1")
    assert state.pending("app1")
    assert any("132001" in r.message and "131047" in r.message for r in caplog.records)


def test_a_non_template_error_is_still_abandoned_and_never_retried():
    state = InMemoryPollStateStore()
    error = WhatsAppApiError(400, "bad request", {"error": {"code": 131047}})
    summary = _poller(
        [_app()], AlwaysFailingSender(error), state=state, send_existing=True
    ).run_once()["apps"][0]
    # 131047 is a quality signal: retrying it feeds what Meta is penalising.
    assert summary["abandoned"] == 1
    assert state.seen("app1", "1")


def test_a_dry_run_never_sends_anything_including_a_fallback():
    sender = TemplateErrorSender(_template_error())
    summary = _poller([_app()], sender, dry_run=True, send_existing=True).run_once()["apps"][0]
    assert sender.payloads == []
    assert summary["would_send"] == 1


def test_the_fallback_is_logged_loudly_because_the_customer_may_not_receive_it(caplog):
    with caplog.at_level("WARNING"):
        _poller([_app()], TemplateErrorSender(_template_error()), send_existing=True).run_once()
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("131047" in message for message in warnings)


def test_the_summary_prints_a_payment_fallback():
    summary = _poller(
        [_app()], TemplateErrorSender(_template_error()), send_existing=True
    ).run_once()
    printed = _format_poll_summary(summary)
    assert "via free-form text fallback" in printed
    assert "000001" in printed