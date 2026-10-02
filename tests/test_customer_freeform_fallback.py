"""The customers pipeline's free-form fallback, pinned.

Mirrors ``test_payment_freeform_fallback.py``, and exists mostly to pin the one
deliberate **difference** between the three pipelines: the customers pipeline
leaves the free-form fallback off by default.

The reason is structural, not stylistic. A welcome goes to a brand-new number,
which is by definition outside WhatsApp's 24-hour customer-service window — and
that window is the only place free-form text is deliverable at all. So a fallback
here could never rescue a send; it could only burn a doomed extra request and log
a confusing second error. With it off, a rejected template leaves the customer
**pending** instead, and they flow on once the template is fixed.
"""

from fakes import FailingSender
from sender.application.poller import CustomerPoller, PollApp
from sender.domain.errors import WhatsAppApiError, is_template_error
from sender.domain.templates import CustomerTemplateBuilder
from sender.infrastructure.state import InMemoryPollStateStore
from sender.presentation.stubs import StubCustomerSource, default_stub_customers


class FakeClock:
    def monotonic(self) -> float:
        return 0.0

    def now(self) -> float:
        return 0.0

    def sleep(self, seconds: float) -> None:
        return None


def _template_error() -> WhatsAppApiError:
    """A 132000-series rejection: the template itself is unusable."""
    return WhatsAppApiError(132000, "Template param count mismatch", {"error": {"code": 132000}})


def _poller(sender, state=None, **kwargs) -> CustomerPoller:
    return CustomerPoller(
        apps=[PollApp(name="app1", source=StubCustomerSource(default_stub_customers()))],
        sender=sender,
        builder=CustomerTemplateBuilder("aizen_new_customer", "ar_EG", "20"),
        state=state or InMemoryPollStateStore(),
        clock=FakeClock(),
        **kwargs,
    )


def _app_result(summary):
    return summary["apps"][0]


# --- the default: no fallback, stay pending ----------------------------------


def test_the_default_leaves_a_rejected_template_pending_and_never_abandoned():
    """The whole point of the customers pipeline's default. Nothing is consumed,
    so fixing the template and re-running delivers the welcome."""
    state = InMemoryPollStateStore()
    sender = FailingSender(_template_error())
    app = _app_result(_poller(sender, state=state, send_existing=True).run_once())
    # ``failed`` is the cycle counter for "did not go out, will be retried"; the
    # authoritative answer is the state store below.
    assert (app["sent"], app["failed"], app["abandoned"]) == (0, 2, 0)
    assert sorted(state.pending("app1")) == ["1", "2"]
    assert not state.abandoned("app1")
    assert not state.seen("app1", "1"), "must not be consumed"


def test_a_customer_left_pending_is_welcomed_once_the_template_is_fixed():
    """The reason pending beats abandoned: the customer is not lost."""
    state = InMemoryPollStateStore()
    _poller(FailingSender(_template_error()), state=state, send_existing=True).run_once()

    class _Fixed:
        def __init__(self):
            self.payloads = []

        def send(self, payload):
            self.payloads.append(payload)
            return {"messages": [{"id": "wamid.OK"}]}

    fixed = _Fixed()
    # The retry is gated on the backoff window, so clear the pending attempts to
    # stand in for "enough time has passed".
    for record in list(state.pending("app1").values()):
        record["next_attempt_at"] = 0
    app = _app_result(_poller(fixed, state=state).run_once())
    assert app["sent"] == 2
    assert len(fixed.payloads) == 2


def test_a_non_template_failure_is_abandoned_even_without_a_fallback():
    """A rejected number is not going to fix itself, so retrying forever would be
    wrong regardless of the fallback setting."""
    sender = FailingSender(WhatsAppApiError(131047, "outside window", {"error": {"code": 131047}}))
    app = _app_result(_poller(sender, send_existing=True).run_once())
    assert app["abandoned"] == 2


# --- when an operator does enable it -----------------------------------------


def test_an_explicitly_enabled_fallback_does_send_free_form_text():
    """The setting still works, for the operator who has a reason (a verified
    opt-in list, or a 24h window they control)."""
    class _FirstCallFails:
        def __init__(self):
            self.payloads = []

        def send(self, payload):
            self.payloads.append(payload)
            if len(self.payloads) == 1:
                raise _template_error()
            return {"messages": [{"id": "wamid.FALLBACK"}]}

    sender = _FirstCallFails()
    app = _app_result(
        _poller(sender, send_existing=True, freeform_fallback=True).run_once()
    )
    # Both customers are delivered: 000002 via the free-form body after its
    # template send was rejected, 000001 via the template on its first attempt.
    assert (app["sent"], app["failed"]) == (2, 0)
    assert [p["type"] for p in sender.payloads] == ["template", "text", "template"]
    assert "أهلًا بكم في Aizen Paper" in sender.payloads[1]["text"]["body"]
    assert sender.payloads[1]["to"] == "201234567890"


def test_a_failing_fallback_keeps_the_customer_pending_on_the_template_error():
    """Both attempts failing must not retire the customer — the template error is
    the actionable one."""

    class _AlwaysFails:
        def __init__(self):
            self.payloads = []

        def send(self, payload):
            self.payloads.append(payload)
            raise _template_error()

    state = InMemoryPollStateStore()
    sender = _AlwaysFails()
    app = _app_result(
        _poller(sender, state=state, send_existing=True, freeform_fallback=True).run_once()
    )
    assert (app["sent"], app["failed"], app["abandoned"]) == (0, 2, 0)
    assert sorted(state.pending("app1")) == ["1", "2"]
    assert not state.abandoned("app1")


def test_a_dry_run_never_attempts_a_fallback():
    """A dry run that quietly sent a free-form message would be the worst possible
    bug in this feature: it looks like a rehearsal and is not."""

    class _Counts:
        def __init__(self):
            self.payloads = []

        def send(self, payload):
            self.payloads.append(payload)
            raise AssertionError("a dry run must not send anything")

    sender = _Counts()
    _poller(sender, dry_run=True, send_existing=True, freeform_fallback=True).run_once()
    assert sender.payloads == []


def test_the_template_error_helper_recognises_the_code():
    """Guards the branch above: if this stopped matching, the fallback would stop
    firing and the pending behaviour above would pass for the wrong reason."""
    assert is_template_error(_template_error())
