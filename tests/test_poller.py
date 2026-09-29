"""Offline specs for the poller use case and the poll state store."""

import json

import pytest

from fakes import CapturingSender, FakeResponse, FakeSession
from sender.application.poller import InvoicePoller, PollApp
from sender.domain.errors import DaftraApiError, WhatsAppApiError, WhatsAppSelfSendError
from sender.domain.templates import LegacyInvoiceTemplateBuilder
from sender.infrastructure.daftra.client import DaftraClient
from sender.infrastructure.daftra.mapper import DaftraInvoiceMapper
from sender.infrastructure.state import (
    InMemoryPollStateStore,
    JsonPollStateStore,
    PollStateLock,
)
from sender.infrastructure.util import write_json_atomic
from sender.presentation.stubs import StubInvoiceSource, default_stub_invoices, make_stub_invoice


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


class FailOnInvoiceSender:
    """Fails sends for the given invoice numbers with a configurable error."""

    def __init__(self, fail_numbers, error=None) -> None:
        self.fail_numbers = set(fail_numbers)
        self.error = error or WhatsAppApiError(500, "boom")
        self.payloads: list[dict] = []

    def send(self, payload: dict) -> dict:
        self.payloads.append(payload)
        number = payload["template"]["components"][1]["parameters"][1]["text"].strip("\u2066\u2068\u2069")
        if number in self.fail_numbers:
            raise self.error
        return {"messages": [{"id": "wamid.FAKE"}]}


class FailingSource:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def list_invoices(self, limit: int = 10, page: int = 1):
        raise self.error

    def get_invoice(self, invoice_id):
        raise self.error

    def get_raw_invoice(self, invoice_id):
        raise self.error


class ListRowWithoutAPhoneSource:
    """Lists a row with no phone, so the poller has to fetch the detail — and the
    fetch can fail however the test needs it to."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def list_invoices(self, limit: int = 10, page: int = 1):
        return [make_stub_invoice(customer_phone=None)]

    def get_invoice(self, invoice_id):
        raise self.error

    def get_raw_invoice(self, invoice_id):
        raise self.error


class DaftraFakeSession(FakeSession):
    def __init__(self, list_payload: dict, get_payload: dict) -> None:
        self.list_payload = list_payload
        self.get_payload = get_payload
        self.headers: dict = {}
        self.urls: list[str] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.urls.append(url)
        if url.endswith("/invoices.json"):
            return FakeResponse(200, self.list_payload)
        return FakeResponse(200, self.get_payload)


def _builder() -> LegacyInvoiceTemplateBuilder:
    return LegacyInvoiceTemplateBuilder(template_name="aizen_invoice", language="en", country_code="20")


def _stub_app(name: str = "app1", invoices=None) -> PollApp:
    return PollApp(name=name, source=StubInvoiceSource(invoices if invoices is not None else default_stub_invoices()))


def _poller(apps, sender=None, state=None, clock=None, **kwargs) -> InvoicePoller:
    return InvoicePoller(
        apps=apps,
        sender=sender,
        builder=_builder(),
        state=state or InMemoryPollStateStore(),
        clock=clock or FakeClock(),
        **kwargs,
    )


def _list_payload(invoice_id: str, number: str) -> dict:
    return {"result": "successful", "data": [{"Invoice": {"id": invoice_id, "no": number}}]}


def _list_payload_with_phone(invoice_id: str, number: str, phone: str) -> dict:
    return {
        "result": "successful",
        "data": [
            {
                "Invoice": {
                    "id": invoice_id,
                    "no": number,
                    "invoice_html_url": "https://demo.daftra.com/invoices/1.pdf",
                    "Client": {"business_name": "Acme", "phone1": phone},
                }
            }
        ],
    }


def _get_payload(invoice_id: str, number: str, phone: str) -> dict:
    return {
        "result": "successful",
        "data": {
            "Invoice": {
                "id": invoice_id,
                "no": number,
                "summary_total": 100,
                "invoice_html_url": "https://demo.daftra.com/invoices/1.pdf",
                "Client": {"business_name": "Acme", "phone1": phone},
            }
        },
    }


# --- detection & sending ---


def test_new_invoice_is_sent_to_the_customer_phone():
    sender = CapturingSender()
    poller = _poller([_stub_app()], sender=sender, send_existing=True)
    summary = poller.run_once()
    app = summary["apps"][0]
    assert app["sent"] == 2
    assert app["skipped_no_phone"] == 1
    # The stub lists newest-first like Daftra, so INV-002 is sent before INV-001.
    assert sender.payloads[0]["to"] == "201234567890"
    assert sender.payloads[1]["to"] == "201027693262"


def test_seen_invoice_is_not_resent_on_the_next_cycle():
    sender = CapturingSender()
    poller = _poller([_stub_app()], sender=sender, send_existing=True)
    poller.run_once()
    assert len(sender.payloads) == 2
    poller.run_once()
    assert len(sender.payloads) == 2


def test_stub_source_can_simulate_new_invoices_between_cycles():
    source = StubInvoiceSource(default_stub_invoices())
    sender = CapturingSender()
    poller = _poller([PollApp("app1", source)], sender=sender, send_existing=True)
    poller.run_once()
    assert len(sender.payloads) == 2
    source.add_new_invoice(customer_name="Sara Ali", customer_phone="01111111111")
    poller.run_once()
    assert len(sender.payloads) == 3
    assert sender.payloads[2]["to"] == "201111111111"


# --- state persistence ---


def test_state_is_persisted_and_a_second_instance_does_not_resent(tmp_path):
    path = tmp_path / "poll_state.json"
    source = StubInvoiceSource(default_stub_invoices())
    sender = CapturingSender()
    state = JsonPollStateStore(str(path))
    poller = _poller([PollApp("app1", source)], sender=sender, state=state, send_existing=True)
    poller.run_once()
    assert len(sender.payloads) == 2
    sender2 = CapturingSender()
    state2 = JsonPollStateStore(str(path))
    poller2 = _poller([PollApp("app1", source)], sender=sender2, state=state2, send_existing=True)
    poller2.run_once()
    assert sender2.payloads == []


def test_state_is_keyed_per_app(tmp_path):
    path = tmp_path / "poll_state.json"
    state = JsonPollStateStore(str(path))
    state.mark_seen("app1", "1")
    assert state.seen("app1", "1") is True
    assert state.seen("app2", "1") is False


def test_state_file_shape_and_reload(tmp_path):
    path = tmp_path / "poll_state.json"
    state = JsonPollStateStore(str(path))
    state.mark_seen("app1", "1")
    state.record_pending("app1", "2", error="network down", action="send", count=2, next_attempt_at=200.0)
    state.record_abandoned("app1", "3", error="no public url", action="build", count=1)
    state.set_last_poll_at("app1", 123.0)
    data = json.loads(path.read_text())
    assert data["apps"]["app1"]["seen"] == ["1"]
    assert data["apps"]["app1"]["pending"]["2"]["count"] == 2
    assert data["apps"]["app1"]["pending"]["2"]["next_attempt_at"] == 200.0
    assert data["apps"]["app1"]["abandoned"]["3"]["error"] == "no public url"
    assert data["apps"]["app1"]["last_poll_at"] == 123.0
    reloaded = JsonPollStateStore(str(path))
    assert reloaded.seen("app1", "1") is True
    assert reloaded.last_poll_at("app1") == 123.0
    assert reloaded.pending("app1")["2"]["count"] == 2
    assert reloaded.abandoned("app1")["3"]["action"] == "build"


def test_legacy_failed_field_is_dropped_on_load(tmp_path):
    path = tmp_path / "poll_state.json"
    path.write_text(json.dumps({"apps": {"app1": {"seen": ["1"], "failed": {"1": 3}, "last_poll_at": None}}}))
    state = JsonPollStateStore(str(path))
    state.mark_seen("app1", "2")
    data = json.loads(path.read_text())
    assert "failed" not in data["apps"]["app1"]


def test_corrupt_state_file_starts_empty(tmp_path, caplog):
    path = tmp_path / "poll_state.json"
    path.write_text("{not json")
    with caplog.at_level("WARNING"):
        state = JsonPollStateStore(str(path))
    assert state.has_app("app1") is False
    assert any("corrupt" in record.message for record in caplog.records)


def test_binary_garbage_state_file_starts_empty_with_a_warning(tmp_path, caplog):
    # UnicodeDecodeError is a ValueError, so `except OSError` never caught it and
    # a binary state file took the whole poller down on construction.
    path = tmp_path / "poll_state.json"
    path.write_bytes(b"\x00\xff\xfe\x01binary garbage")
    with caplog.at_level("WARNING"):
        state = JsonPollStateStore(str(path))
    assert state.has_app("app1") is False
    assert state.app_names() == []
    assert any("not valid UTF-8" in record.message for record in caplog.records)


def test_a_corrupt_state_file_is_preserved_aside_not_replaced(tmp_path, caplog):
    """A corrupt file is evidence of a durability problem; it must not be silently
    replaced by a fresh empty state, or the operator never learns what happened."""
    path = tmp_path / "poll_state.json"
    path.write_text("{not json")
    with caplog.at_level("ERROR"):
        state = JsonPollStateStore(str(path))
    assert state.has_app("app1") is False
    backup = tmp_path / "poll_state.json.corrupt"
    assert backup.exists()
    assert backup.read_text() == "{not json"
    assert not path.exists()
    assert any("preserved" in record.message for record in caplog.records)


def test_a_state_save_failure_raises_instead_of_being_swallowed(tmp_path, monkeypatch):
    """Silently losing a state write is how a run re-sends a whole customer
    history: the failure has to surface as a failed cycle, not a WARNING."""
    from sender.infrastructure import state as state_module

    def _boom(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(state_module, "write_json_atomic", _boom)
    store = JsonPollStateStore(str(tmp_path / "poll_state.json"))
    with pytest.raises(OSError, match="disk full"):
        store.mark_seen("app1", "1")


def test_state_bounds_seen_ids(tmp_path):
    path = tmp_path / "poll_state.json"
    state = JsonPollStateStore(str(path), max_seen=3)
    state.mark_many_seen("app1", ["1", "2", "3", "4", "5"])
    assert state.seen("app1", "1") is True
    assert state.seen("app1", "5") is False


def test_state_bounds_pending_and_abandoned(tmp_path):
    path = tmp_path / "poll_state.json"
    state = JsonPollStateStore(str(path), max_seen=2)
    for i in range(5):
        state.record_pending("app1", str(i), error="e", action="send", count=1, next_attempt_at=1.0)
        state.record_abandoned("app1", str(i), error="e", action="build", count=1)
    assert len(state.pending("app1")) == 2
    assert len(state.abandoned("app1")) == 2


def test_mark_seen_clears_pending_and_abandoned(tmp_path):
    path = tmp_path / "poll_state.json"
    state = JsonPollStateStore(str(path))
    state.record_pending("app1", "1", error="e", action="send", count=1, next_attempt_at=1.0)
    state.record_abandoned("app1", "2", error="e", action="build", count=1)
    state.mark_seen("app1", "1")
    state.mark_seen("app1", "2")
    assert state.pending("app1") == {}
    assert state.abandoned("app1") == {}


def test_reset_app_removes_the_whole_entry(tmp_path):
    path = tmp_path / "poll_state.json"
    state = JsonPollStateStore(str(path))
    state.mark_seen("app1", "1")
    state.mark_seen("app2", "1")
    state.reset_app("app1")
    assert state.has_app("app1") is False
    assert state.has_app("app2") is True


def test_clear_invoice_removes_seen_pending_and_abandoned(tmp_path):
    path = tmp_path / "poll_state.json"
    state = JsonPollStateStore(str(path))
    state.mark_seen("app1", "1")
    state.record_pending("app1", "2", error="e", action="send", count=1, next_attempt_at=1.0)
    state.record_abandoned("app1", "3", error="e", action="build", count=1)
    state.clear_invoice("app1", "1")
    state.clear_invoice("app1", "2")
    state.clear_invoice("app1", "3")
    assert state.seen("app1", "1") is False
    assert state.pending("app1") == {}
    assert state.abandoned("app1") == {}


def test_batch_defer_saves_to_one_write(tmp_path):
    path = tmp_path / "poll_state.json"
    state = JsonPollStateStore(str(path))
    with state.batch():
        state.mark_seen("app1", "1")
        state.mark_seen("app1", "2")
        state.set_last_poll_at("app1", 5.0)
        assert not path.exists()  # nothing written yet
    data = json.loads(path.read_text())
    assert data["apps"]["app1"]["seen"] == ["2", "1"]
    assert data["apps"]["app1"]["last_poll_at"] == 5.0


# --- first-run safety ---


def test_first_run_seeds_without_sending():
    sender = CapturingSender()
    poller = _poller([_stub_app()], sender=sender)
    summary = poller.run_once()
    app = summary["apps"][0]
    assert app["first_run"] is True
    assert app["seeded"] == 3
    assert app["sent"] == 0
    assert sender.payloads == []


def test_send_existing_sends_on_the_first_run():
    sender = CapturingSender()
    poller = _poller([_stub_app()], sender=sender, send_existing=True)
    summary = poller.run_once()
    app = summary["apps"][0]
    assert app["first_run"] is True
    assert app["seeded"] == 0
    assert app["sent"] == 2
    assert len(sender.payloads) == 2


def test_first_run_with_a_saturated_first_page_still_seeds_without_sending():
    # A full first page (>= limit) makes the pager peek at the state, and that
    # read creates the app entry as a side effect. first_run has to be captured
    # before the listing, otherwise this app looks established, skips the seeding
    # and the next cycle treats its whole history as new.
    sender = CapturingSender()
    poller = _poller([_stub_app()], sender=sender, limit=3)
    summary = poller.run_once()
    app = summary["apps"][0]
    assert app["first_run"] is True
    assert app["seeded"] == 3
    assert app["sent"] == 0
    assert sender.payloads == []


# --- no-phone handling ---


def test_invoice_without_phone_is_skipped_and_marked_seen(caplog):
    sender = CapturingSender()
    poller = _poller([_stub_app()], sender=sender, send_existing=True)
    with caplog.at_level("WARNING"):
        summary = poller.run_once()
    app = summary["apps"][0]
    assert app["skipped_no_phone"] == 1
    assert any("INV-003" in record.message and "no usable WhatsApp phone" in record.message for record in caplog.records)
    poller.run_once()
    assert len(sender.payloads) == 2


def test_invoice_with_an_unusable_phone_is_skipped_and_marked_seen():
    source = StubInvoiceSource([make_stub_invoice(customer_phone="not-a-phone")])
    sender = CapturingSender()
    poller = _poller([PollApp("app1", source)], sender=sender, send_existing=True)
    summary = poller.run_once()
    app = summary["apps"][0]
    assert app["skipped_no_phone"] == 1
    assert sender.payloads == []
    poller.run_once()
    assert sender.payloads == []


# --- failure classification & retry policy ---


def test_retryable_send_failure_is_retried_and_never_given_up():
    sender = FailOnInvoiceSender({"INV-002"}, error=WhatsAppApiError(500, "boom"))
    clock = FakeClock()
    poller = _poller([_stub_app()], sender=sender, send_existing=True, clock=clock)
    summary = poller.run_once()
    app = summary["apps"][0]
    assert app["sent"] == 1
    assert app["failed"] == 1
    assert app["abandoned"] == 0
    # Advance well past the growing backoff so every cycle retries; the invoice
    # is never given up.
    for _ in range(5):
        clock.value += 10000
        poller.run_once()
    assert len(sender.payloads) == 7  # INV-001 once, INV-002 retried every cycle
    assert poller._state.abandoned("app1") == {}


def test_pending_invoice_is_deferred_during_backoff():
    sender = FailOnInvoiceSender({"INV-002"}, error=WhatsAppApiError(500, "boom"))
    clock = FakeClock()
    poller = _poller([_stub_app()], sender=sender, send_existing=True, clock=clock)
    poller.run_once()
    assert len(sender.payloads) == 2  # INV-001 sent, INV-002 failed
    summary = poller.run_once()  # clock still 0; INV-002 is in backoff
    app = summary["apps"][0]
    assert app["pending"] == 1
    assert app["failed"] == 0
    assert len(sender.payloads) == 2  # no retry yet


def test_network_failure_is_retryable():
    sender = FailOnInvoiceSender({"INV-002"}, error=WhatsAppApiError(None, "network error: timeout"))
    clock = FakeClock()
    poller = _poller([_stub_app()], sender=sender, send_existing=True, clock=clock)
    poller.run_once()
    clock.value += 61
    poller.run_once()
    assert len(sender.payloads) == 3  # INV-002 failed, INV-001 sent, INV-002 retried


def test_rate_limit_failure_is_retryable():
    sender = FailOnInvoiceSender({"INV-002"}, error=WhatsAppApiError(429, "rate limited"))
    clock = FakeClock()
    poller = _poller([_stub_app()], sender=sender, send_existing=True, clock=clock)
    poller.run_once()
    clock.value += 61
    poller.run_once()
    assert len(sender.payloads) == 3


def test_permanent_failure_is_abandoned_immediately():
    sender = FailOnInvoiceSender({"INV-002"}, error=WhatsAppApiError(400, "template mismatch [code 132012]"))
    poller = _poller([_stub_app()], sender=sender, send_existing=True)
    summary = poller.run_once()
    app = summary["apps"][0]
    assert app["sent"] == 1
    assert app["abandoned"] == 1
    assert app["failed"] == 0
    poller.run_once()
    assert len(sender.payloads) == 2  # INV-002 is not retried


def test_self_send_failure_is_permanent():
    sender = FailOnInvoiceSender({"INV-002"}, error=WhatsAppSelfSendError("201280805534"))
    poller = _poller([_stub_app()], sender=sender, send_existing=True)
    summary = poller.run_once()
    assert summary["apps"][0]["abandoned"] == 1
    poller.run_once()
    assert len(sender.payloads) == 2


def test_build_failure_with_missing_public_url_is_abandoned():
    source = StubInvoiceSource([make_stub_invoice(public_url=None)])
    sender = CapturingSender()
    poller = _poller([PollApp("app1", source)], sender=sender, send_existing=True)
    summary = poller.run_once()
    app = summary["apps"][0]
    assert app["abandoned"] == 1
    assert sender.payloads == []
    poller.run_once()
    assert sender.payloads == []


def test_abandoned_invoice_is_recorded_in_state(tmp_path):
    path = tmp_path / "poll_state.json"
    sender = FailOnInvoiceSender({"INV-002"}, error=WhatsAppApiError(400, "bad"))
    state = JsonPollStateStore(str(path))
    poller = _poller([_stub_app()], sender=sender, state=state, send_existing=True)
    poller.run_once()
    assert state.seen("app1", "2") is True
    assert state.abandoned("app1")["2"]["count"] == 1
    assert state.pending("app1") == {}


def test_retryable_failure_is_recorded_as_pending_in_state(tmp_path):
    path = tmp_path / "poll_state.json"
    sender = FailOnInvoiceSender({"INV-002"}, error=WhatsAppApiError(500, "boom"))
    state = JsonPollStateStore(str(path))
    poller = _poller([_stub_app()], sender=sender, state=state, send_existing=True)
    poller.run_once()
    assert state.seen("app1", "2") is False
    pending = state.pending("app1")["2"]
    assert pending["count"] == 1
    assert pending["next_attempt_at"] > 0


def test_one_app_failing_does_not_stop_the_other():
    failing = PollApp("broken", FailingSource(DaftraApiError(401, "unauthorized")))
    working = _stub_app(name="healthy")
    sender = CapturingSender()
    poller = _poller([failing, working], sender=sender, send_existing=True)
    summary = poller.run_once()
    assert summary["apps"][0]["ok"] is False
    assert summary["apps"][1]["ok"] is True
    assert summary["all_failed"] is False
    assert len(sender.payloads) == 2


def test_all_apps_failing_marks_the_cycle_failed():
    failing1 = PollApp("broken1", FailingSource(DaftraApiError(401, "unauthorized")))
    failing2 = PollApp("broken2", FailingSource(DaftraApiError(500, "boom")))
    poller = _poller([failing1, failing2], sender=CapturingSender())
    summary = poller.run_once()
    assert summary["all_failed"] is True


def test_unexpected_error_in_one_app_does_not_stop_the_other_apps(caplog):
    # A KeyError is not one of the classified API/ValueError/RuntimeError
    # failures, so it used to escape the cycle and take the remaining tenants
    # (and the poll loop) with it.
    broken = PollApp("broken", FailingSource(KeyError("invoices")))
    working = _stub_app(name="healthy")
    sender = CapturingSender()
    poller = _poller([broken, working], sender=sender, send_existing=True)
    with caplog.at_level("ERROR"):
        summary = poller.run_once()
    assert summary["apps"][0]["ok"] is False
    assert summary["apps"][0]["error"]
    assert summary["apps"][0]["listed"] == 0
    assert summary["apps"][0]["sent"] == 0
    assert summary["apps"][1]["ok"] is True
    assert summary["all_failed"] is False
    assert len(sender.payloads) == 2
    assert any("unexpected failure while polling" in record.message for record in caplog.records)


def test_keyboard_interrupt_is_not_swallowed_by_the_per_app_guard():
    poller = _poller(
        [PollApp("broken", FailingSource(KeyboardInterrupt())), _stub_app(name="healthy")],
        sender=CapturingSender(),
        once=True,
    )
    summary = poller.run()
    assert summary["interrupted"] is True


# --- retry backoff bounds ---


def test_backoff_for_a_huge_attempt_count_returns_max_backoff():
    # 2 ** (count - 1) overflows from attempt 1025 on, and that unhandled
    # OverflowError inside a failure path used to kill the whole poll loop.
    poller = _poller([_stub_app()], sender=CapturingSender())
    assert poller._backoff_for(1) == 60.0
    assert poller._backoff_for(3) == 240.0
    assert poller._backoff_for(1025) == 3600.0
    assert poller._backoff_for(100000) == 3600.0


# --- two apps, each with its own client ---


def test_two_apps_run_sequentially_each_with_its_own_client():
    session1 = DaftraFakeSession(_list_payload("1", "INV-001"), _get_payload("1", "INV-001", "01027693262"))
    session2 = DaftraFakeSession(_list_payload("2", "INV-002"), _get_payload("2", "INV-002", "01234567890"))
    client1 = DaftraClient(api_key="key1", base_url="https://one.daftra.com/api2", timeout=5, session=session1)
    client2 = DaftraClient(api_key="key2", base_url="https://two.daftra.com/api2", timeout=5, session=session2)
    sender = CapturingSender()
    poller = _poller(
        [PollApp("one", client1), PollApp("two", client2)],
        sender=sender,
        send_existing=True,
    )
    summary = poller.run_once()
    assert [app["app"] for app in summary["apps"]] == ["one", "two"]
    assert session1.headers["apikey"] == "key1"
    assert session2.headers["apikey"] == "key2"
    assert session1.urls[0].startswith("https://one.daftra.com/api2")
    assert session2.urls[0].startswith("https://two.daftra.com/api2")
    assert sender.payloads[0]["to"] == "201027693262"
    assert sender.payloads[1]["to"] == "201234567890"


def test_list_payload_without_phone_fetches_the_authoritative_detail():
    session = DaftraFakeSession(_list_payload("1", "INV-001"), _get_payload("1", "INV-001", "01027693262"))
    client = DaftraClient(api_key="key1", base_url="https://one.daftra.com/api2", timeout=5, session=session)
    sender = CapturingSender()
    poller = _poller([PollApp("one", client)], sender=sender, send_existing=True)
    summary = poller.run_once()
    assert summary["apps"][0]["sent"] == 1
    assert len(session.urls) == 2
    assert session.urls[0].endswith("/invoices.json")
    assert session.urls[1].endswith("/invoices/1.json")
    assert sender.payloads[0]["to"] == "201027693262"


def test_list_payload_with_phone_avoids_a_get_invoice_call():
    session = DaftraFakeSession(
        _list_payload_with_phone("1", "INV-001", "01027693262"),
        _get_payload("1", "INV-001", "01027693262"),
    )
    client = DaftraClient(api_key="key1", base_url="https://one.daftra.com/api2", timeout=5, session=session)
    sender = CapturingSender()
    poller = _poller([PollApp("one", client)], sender=sender, send_existing=True)
    summary = poller.run_once()
    assert summary["apps"][0]["sent"] == 1
    assert len(session.urls) == 1
    assert sender.payloads[0]["to"] == "201027693262"


def test_invoice_with_no_phone_even_after_detail_is_skipped():
    session = DaftraFakeSession(_list_payload("1", "INV-001"), _get_payload("1", "INV-001", ""))
    client = DaftraClient(api_key="key1", base_url="https://one.daftra.com/api2", timeout=5, session=session)
    sender = CapturingSender()
    poller = _poller([PollApp("one", client)], sender=sender, send_existing=True)
    summary = poller.run_once()
    assert summary["apps"][0]["skipped_no_phone"] == 1
    assert sender.payloads == []


# --- phone normalization follows the configured country code ---


def test_mapper_normalizes_with_the_configured_country_code():
    raw = _list_payload_with_phone("1", "INV-001", "0501234567")
    # The mapper used to hardcode 20, so a Saudi local number was read as an
    # Egyptian one and the poller then re-normalized the already-wrong digits.
    assert DaftraInvoiceMapper(country_code="966").to_invoices(raw)[0].customer_phone == "966501234567"
    assert DaftraInvoiceMapper().to_invoices(raw)[0].customer_phone == "20501234567"


def test_daftra_client_hands_its_country_code_to_the_mapper():
    session = DaftraFakeSession(
        _list_payload_with_phone("1", "INV-001", "0501234567"),
        _get_payload("1", "INV-001", "0501234567"),
    )
    client = DaftraClient(
        api_key="key1", base_url="https://one.daftra.com/api2", timeout=5,
        session=session, country_code="966",
    )
    assert client.get_invoice("1").customer_phone == "966501234567"


# --- dry run ---


def test_dry_run_builds_payloads_but_never_calls_the_sender():
    sender = CapturingSender()
    poller = _poller([_stub_app()], sender=sender, send_existing=True, dry_run=True)
    summary = poller.run_once()
    app = summary["apps"][0]
    assert app["would_send"] == 2
    assert app["sent"] == 0
    assert sender.payloads == []


def test_dry_run_does_not_mutate_state(tmp_path):
    path = tmp_path / "poll_state.json"
    state = JsonPollStateStore(str(path))
    poller = _poller([_stub_app()], sender=CapturingSender(), state=state, send_existing=True, dry_run=True)
    poller.run_once()
    assert state.seen_ids("app1") == []
    assert not path.exists()  # a dry run must not even create the state file
    assert state.pending("app1") == {}
    assert state.abandoned("app1") == {}
    assert state.last_poll_at("app1") is None


def test_dry_run_does_not_mark_no_phone_invoices_seen(tmp_path):
    path = tmp_path / "poll_state.json"
    state = JsonPollStateStore(str(path))
    poller = _poller([_stub_app()], sender=CapturingSender(), state=state, send_existing=True, dry_run=True)
    summary = poller.run_once()
    assert summary["apps"][0]["skipped_no_phone"] == 1
    assert state.seen_ids("app1") == []


def test_dry_run_does_not_abandon_an_invoice_whose_payload_failed_to_build(tmp_path):
    # --dry-run is documented as non-mutating; a build failure on a dry run
    # would otherwise mark the invoice seen and record it as abandoned, quietly
    # retiring it for every real run afterwards.
    path = tmp_path / "poll_state.json"
    source = StubInvoiceSource([make_stub_invoice(public_url=None)])
    state = JsonPollStateStore(str(path))
    poller = _poller(
        [PollApp("app1", source)], sender=CapturingSender(), state=state,
        send_existing=True, dry_run=True,
    )
    summary = poller.run_once()
    assert summary["apps"][0]["abandoned"] == 1
    assert state.seen_ids("app1") == []
    assert state.abandoned("app1") == {}
    assert not path.exists()


def test_dry_run_does_not_record_a_pending_backoff_for_a_retryable_failure(tmp_path):
    path = tmp_path / "poll_state.json"
    source = ListRowWithoutAPhoneSource(DaftraApiError(500, "boom"))
    state = JsonPollStateStore(str(path))
    poller = _poller(
        [PollApp("app1", source)], sender=CapturingSender(), state=state,
        send_existing=True, dry_run=True,
    )
    summary = poller.run_once()
    assert summary["apps"][0]["failed"] == 1
    assert state.pending("app1") == {}
    assert state.seen_ids("app1") == []
    assert not path.exists()


# --- catch-up paging (D5) ---


def test_burst_of_new_invoices_is_caught_up_across_pages():
    invoices = [make_stub_invoice(id=str(i), number=f"INV-{i:03d}", customer_phone="01027693262") for i in range(1, 16)]
    source = StubInvoiceSource(invoices)
    sender = CapturingSender()
    poller = _poller([PollApp("app1", source)], sender=sender, send_existing=True, limit=10)
    summary = poller.run_once()
    app = summary["apps"][0]
    assert app["listed"] == 15
    assert app["sent"] == 15


def test_saturated_listing_logs_a_warning(caplog):
    invoices = [make_stub_invoice(id=str(i), number=f"INV-{i:03d}", customer_phone="01027693262") for i in range(1, 16)]
    source = StubInvoiceSource(invoices)
    poller = _poller([PollApp("app1", source)], sender=CapturingSender(), send_existing=True, limit=10)
    with caplog.at_level("WARNING"):
        poller.run_once()
    assert any("came back full" in record.message for record in caplog.records)


def test_a_full_page_of_already_seen_invoices_does_not_warn_about_saturation(caplog):
    """The saturation warning is about *unhandled* backlog, not about page size.

    Once a busy tenant has more invoices than ``--limit``, page 1 is full on
    every single cycle for ever, all of it already seen — warning each time
    trains the operator to ignore the line and buries the real signal (the
    ``max_pages`` warning, which fires only when unseen rows are actually being
    dropped).
    """
    invoices = [make_stub_invoice(id=str(i), number=f"INV-{i:03d}", customer_phone="01027693262") for i in range(1, 16)]
    source = StubInvoiceSource(invoices)
    state = InMemoryPollStateStore()
    poller = _poller(
        [PollApp("app1", source)], sender=CapturingSender(), state=state,
        send_existing=True, limit=10,
    )
    poller.run_once()  # first cycle handles the 15 invoices
    assert len(state.seen_ids("app1")) == 15

    caplog.clear()
    with caplog.at_level("WARNING"):
        quiet = poller.run_once()

    assert quiet["apps"][0]["listed"] == 10  # the page really did come back full
    assert quiet["apps"][0]["sent"] == 0
    assert not any("came back full" in record.message for record in caplog.records)

    # A fresh invoice on the same full page brings the warning back.
    source.add_new_invoice(customer_phone="01027693262")
    caplog.clear()
    with caplog.at_level("WARNING"):
        poller.run_once()
    assert any("came back full" in record.message for record in caplog.records)


def test_paging_stops_at_seen_territory():
    invoices = [make_stub_invoice(id=str(i), number=f"INV-{i:03d}", customer_phone="01027693262") for i in range(1, 16)]
    source = StubInvoiceSource(invoices)
    state = InMemoryPollStateStore()
    state.mark_many_seen("app1", [str(i) for i in range(1, 6)])  # oldest 5 already handled
    sender = CapturingSender()
    poller = _poller([PollApp("app1", source)], sender=sender, state=state, send_existing=True, limit=10)
    summary = poller.run_once()
    app = summary["apps"][0]
    assert app["listed"] == 15
    assert app["sent"] == 10  # only the 10 unseen ones


# --- loop bounds ---


def test_once_runs_a_single_cycle():
    clock = FakeClock()
    sender = CapturingSender()
    poller = _poller([_stub_app()], sender=sender, send_existing=True, clock=clock, once=True)
    summary = poller.run()
    assert summary["apps"][0]["sent"] == 2
    assert clock.sleeps == []


def test_max_cycles_bounds_the_loop():
    clock = FakeClock()
    sender = CapturingSender()
    poller = _poller([_stub_app()], sender=sender, send_existing=True, clock=clock, max_cycles=2)
    summary = poller.run()
    assert clock.sleeps == [60.0]
    assert len(sender.payloads) == 2
    assert summary["apps"][0]["sent"] == 0  # the last cycle found nothing new


def test_timeout_bounds_the_loop():
    clock = FakeClock()
    sender = CapturingSender()
    poller = _poller([_stub_app()], sender=sender, send_existing=True, clock=clock, timeout=10.0)
    summary = poller.run()
    assert clock.sleeps == [10.0]
    assert summary["apps"][0]["sent"] == 2


def test_interval_is_honoured_between_cycles():
    clock = FakeClock()
    sender = CapturingSender()
    poller = _poller([_stub_app()], sender=sender, send_existing=True, clock=clock, interval=5.0, max_cycles=2)
    poller.run()
    assert clock.sleeps == [5.0]


# --- stub fixture persistence (D3) ---


def test_stub_source_persists_to_a_fixture_file(tmp_path):
    path = tmp_path / "stub_invoices.json"
    source = StubInvoiceSource(path=str(path))
    assert len(source.list_invoices(limit=10)) == 3
    assert path.exists()
    source.add_new_invoice(customer_name="Sara Ali", customer_phone="01111111111")
    reloaded = StubInvoiceSource(path=str(path))
    assert len(reloaded.list_invoices(limit=10)) == 4
    assert reloaded.get_invoice("4").customer_name == "Sara Ali"


def test_stub_source_without_path_stays_in_memory():
    source = StubInvoiceSource(default_stub_invoices())
    source.add_new_invoice()
    assert len(source.list_invoices(limit=10)) == 4


def test_stub_source_round_trips_money_and_dates(tmp_path):
    path = tmp_path / "stub_invoices.json"
    source = StubInvoiceSource(path=str(path))
    reloaded = StubInvoiceSource(path=str(path))
    invoice = reloaded.get_invoice("1")
    assert str(invoice.total) == "1500.00"
    assert invoice.issue_date.isoformat() == "2026-09-01"


# --- atomic write helper (D7) ---


def test_write_json_atomic_uses_a_pid_qualified_temp_and_cleans_up(tmp_path):
    target = tmp_path / "state.json"
    write_json_atomic(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    leftovers = [p for p in tmp_path.iterdir() if p.name != "state.json"]
    assert leftovers == []


# --- concurrency lock (D7) ---


def test_poll_state_lock_excludes_a_second_holder(tmp_path):
    path = tmp_path / "poll_state.json"
    lock1 = PollStateLock(str(path))
    lock1.acquire()
    lock2 = PollStateLock(str(path))
    with pytest.raises(RuntimeError, match="another poller is already running"):
        lock2.acquire()
    lock1.release()
    lock2.acquire()
    lock2.release()


def test_poll_state_lock_is_released_on_context_exit(tmp_path):
    path = tmp_path / "poll_state.json"
    with PollStateLock(str(path)):
        pass
    lock = PollStateLock(str(path))
    lock.acquire()
    lock.release()