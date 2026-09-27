"""Offline specs for the template registry (status -> builder auto-switch)."""

import json

import pytest

from fakes import FakeResponse, FakeSession
from sender.domain.errors import WhatsAppApiError
from sender.domain.templates import CleanTextTemplateBuilder, LegacyInvoiceTemplateBuilder
from sender.infrastructure.whatsapp.template_registry import TemplateRegistry


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RecordingSession(FakeSession):
    def get(self, url: str, **kwargs) -> FakeResponse:
        if not hasattr(self, "urls"):
            self.urls = []
        self.urls.append(url)
        return super().get(url, **kwargs)


LEGACY_COMPONENTS = [
    {"type": "HEADER", "format": "DOCUMENT", "example": {"header_handle": ["https://x/INV-001.pdf"]}},
    {
        "type": "BODY",
        "text": "مرحبا {{1}}، رقم {{2}}، تاريخ {{3}}، إجمالي {{4}}",
        "example": {"body_text": [["Print Home", "18A", "18/09/2026", "2,500"]]},
    },
    {"type": "FOOTER", "text": "Aizen Paper"},
]

CLEAN_COMPONENTS = [
    {"type": "HEADER", "format": "TEXT", "text": "فاتورة مبيعات | Aizen Paper"},
    {
        "type": "BODY",
        "text": "مرحبا {{1}}، رقم {{2}}، تاريخ {{3}}، إجمالي {{4}}",
        "example": {"body_text": [["Print Home", "18A", "18/09/2026", "2,500"]]},
    },
    {"type": "FOOTER", "text": "Aizen Paper"},
]

VARIABLE_HEADER_COMPONENTS = [
    {"type": "HEADER", "format": "TEXT", "text": "فاتورة رقم {{1}}"},
    {
        "type": "BODY",
        "text": "مرحبا {{2}}، رقم {{3}}، تاريخ {{4}}، إجمالي {{5}}",
        "example": {"body_text": [["Print Home", "18A", "18/09/2026", "2,500"]]},
    },
]


def _graph_data(status: str, components: list[dict], language: str = "en") -> dict:
    return {"data": [{"name": "aizen_invoice", "status": status, "language": language, "components": components}]}


def _registry(session: FakeSession, path, clock: FakeClock, ttl: float = 300.0) -> TemplateRegistry:
    return TemplateRegistry(
        access_token="tok",
        waba_id="waba123",
        api_version="v25.0",
        template_name="aizen_invoice",
        language="en",
        timeout=5.0,
        session=session,
        cache_path=str(path),
        ttl_seconds=ttl,
        clock=clock,
    )


def _seed_cache(path, overrides: dict) -> None:
    data = {
        "template": "aizen_invoice",
        "fetched_at": 0.0,
        "status": "PENDING",
        "fingerprint": "header=DOCUMENT;header_vars=0;body_vars=4;footer=True",
        "header_vars": 0,
        "clean": False,
        "approved_structure": "legacy",
        "active_builder": "legacy",
        "legacy_deprecated": False,
    }
    data.update(overrides)
    path.write_text(json.dumps(data))


def test_pending_template_selects_the_legacy_builder(tmp_path):
    session = RecordingSession({"get": FakeResponse(200, _graph_data("PENDING", LEGACY_COMPONENTS))})
    registry = _registry(session, tmp_path / "state.json", FakeClock())
    snapshot = registry.current()
    assert snapshot["status"] == "PENDING"
    assert snapshot["clean"] is False
    assert snapshot["active_builder"] == "legacy"
    assert snapshot["approved_structure"] is None
    assert isinstance(registry.choose_builder("aizen_invoice", "en", "20"), LegacyInvoiceTemplateBuilder)


def test_approved_clean_template_selects_the_clean_builder_and_persists_cache(tmp_path):
    path = tmp_path / "state.json"
    session = RecordingSession({"get": FakeResponse(200, _graph_data("APPROVED", CLEAN_COMPONENTS))})
    registry = _registry(session, path, FakeClock())
    snapshot = registry.current()
    assert snapshot["status"] == "APPROVED"
    assert snapshot["clean"] is True
    assert snapshot["active_builder"] == "new"
    assert isinstance(registry.choose_builder("aizen_invoice", "en", "20"), CleanTextTemplateBuilder)
    cached = json.loads(path.read_text())
    assert cached["template"] == "aizen_invoice"
    assert cached["status"] == "APPROVED"
    assert cached["approved_structure"] == "new"
    assert cached["active_builder"] == "new"
    assert cached["fingerprint"] == "header=TEXT;header_vars=0;body_vars=4;footer=True"


def test_approved_variable_text_header_never_selects_the_clean_builder(tmp_path):
    session = RecordingSession({"get": FakeResponse(200, _graph_data("APPROVED", VARIABLE_HEADER_COMPONENTS))})
    registry = _registry(session, tmp_path / "state.json", FakeClock())
    snapshot = registry.current()
    assert snapshot["header_vars"] == 1
    assert snapshot["clean"] is False
    assert snapshot["active_builder"] == "legacy"


def test_approved_document_header_keeps_the_legacy_builder(tmp_path):
    session = RecordingSession({"get": FakeResponse(200, _graph_data("APPROVED", LEGACY_COMPONENTS))})
    registry = _registry(session, tmp_path / "state.json", FakeClock())
    snapshot = registry.current()
    assert snapshot["status"] == "APPROVED"
    assert snapshot["clean"] is False
    assert snapshot["active_builder"] == "legacy"
    assert snapshot["approved_structure"] == "legacy"


def test_fresh_approved_cache_is_served_without_a_graph_call(tmp_path):
    clock = FakeClock()
    session = RecordingSession({"get": FakeResponse(200, _graph_data("APPROVED", CLEAN_COMPONENTS))})
    registry = _registry(session, tmp_path / "state.json", clock)
    registry.current()
    clock.advance(10)
    first_calls = len(session.calls)
    snapshot = registry.current()
    assert snapshot["active_builder"] == "new"
    assert len(session.calls) == first_calls


def test_stale_cache_triggers_a_refetch(tmp_path):
    path = tmp_path / "state.json"
    _seed_cache(path, {"status": "APPROVED", "clean": True, "approved_structure": "new", "active_builder": "new"})
    clock = FakeClock()
    clock.advance(301)
    session = RecordingSession({"get": FakeResponse(200, _graph_data("APPROVED", CLEAN_COMPONENTS))})
    registry = _registry(session, path, clock)
    registry.current()
    assert len(session.calls) == 1


def test_pending_status_is_always_refetched_even_when_fresh(tmp_path):
    path = tmp_path / "state.json"
    _seed_cache(path, {})
    clock = FakeClock()
    clock.advance(5)
    session = RecordingSession({"get": FakeResponse(200, _graph_data("PENDING", LEGACY_COMPONENTS))})
    registry = _registry(session, path, clock)
    snapshot = registry.current()
    assert len(session.calls) == 1
    assert snapshot["status"] == "PENDING"
    assert isinstance(registry.choose_builder("aizen_invoice", "en", "20"), LegacyInvoiceTemplateBuilder)


def test_transition_to_approved_marks_legacy_deprecated_once(tmp_path):
    path = tmp_path / "state.json"
    _seed_cache(path, {})
    clock = FakeClock()
    clock.advance(500)
    session = RecordingSession({"get": FakeResponse(200, _graph_data("APPROVED", CLEAN_COMPONENTS))})
    registry = _registry(session, path, clock)
    snapshot = registry.current()
    assert snapshot["legacy_deprecated"] is True
    assert snapshot["noticed_at"] == 500.0
    clock.advance(10)
    again = registry.current(force=True)
    assert again["legacy_deprecated"] is True
    assert again["noticed_at"] == 500.0


def test_clean_approval_without_cache_history_marks_legacy_deprecated(tmp_path):
    session = RecordingSession({"get": FakeResponse(200, _graph_data("APPROVED", CLEAN_COMPONENTS))})
    registry = _registry(session, tmp_path / "state.json", FakeClock())
    snapshot = registry.current()
    assert snapshot["approved_structure"] == "new"
    assert snapshot["legacy_deprecated"] is True
    assert snapshot["noticed_at"] == 0.0


def test_edited_clean_template_in_review_keeps_the_clean_builder(tmp_path):
    path = tmp_path / "state.json"
    _seed_cache(path, {"status": "APPROVED", "clean": True, "approved_structure": "new", "active_builder": "new", "legacy_deprecated": True, "noticed_at": 10.0})
    session = RecordingSession({"get": FakeResponse(200, _graph_data("PENDING", CLEAN_COMPONENTS))})
    registry = _registry(session, path, FakeClock())
    snapshot = registry.refresh()
    assert snapshot["status"] == "PENDING"
    assert snapshot["approved_structure"] == "new"
    assert snapshot["active_builder"] == "new"
    assert snapshot["legacy_deprecated"] is True


def test_query_filters_the_template_by_language(tmp_path):
    data = {
        "data": [
            {"name": "aizen_invoice", "status": "APPROVED", "language": "ar", "components": CLEAN_COMPONENTS},
            {"name": "aizen_invoice", "status": "APPROVED", "language": "en", "components": CLEAN_COMPONENTS},
        ]
    }
    session = RecordingSession({"get": FakeResponse(200, data)})
    registry = _registry(session, tmp_path / "state.json", FakeClock())
    snapshot = registry.current()
    assert snapshot["active_builder"] == "new"
    params = session.calls[0][1]["params"]
    assert "language" in params["fields"]


def test_fail_open_serves_the_last_cached_snapshot(tmp_path):
    path = tmp_path / "state.json"
    _seed_cache(path, {"fetched_at": 100.0, "status": "APPROVED", "clean": True, "approved_structure": "new", "active_builder": "new"})
    clock = FakeClock()
    clock.advance(400)
    session = RecordingSession({"get": FakeResponse(400, {"error": {"code": 1, "message": "nope"}})})
    registry = _registry(session, path, clock)
    snapshot = registry.current()
    assert snapshot["active_builder"] == "new"
    assert snapshot["status"] == "APPROVED"


def test_fail_open_without_cache_falls_back_to_the_legacy_builder(tmp_path):
    session = RecordingSession({"get": FakeResponse(500, None)})
    registry = _registry(session, tmp_path / "state.json", FakeClock())
    snapshot = registry.current()
    assert snapshot["active_builder"] == "legacy"
    assert isinstance(registry.choose_builder("aizen_invoice", "en", "20"), LegacyInvoiceTemplateBuilder)


def test_forced_refresh_raises_when_the_live_query_fails(tmp_path):
    session = RecordingSession({"get": FakeResponse(400, {"error": {"code": 1, "message": "nope"}})})
    registry = _registry(session, tmp_path / "state.json", FakeClock())
    with pytest.raises(WhatsAppApiError):
        registry.refresh()


def test_unknown_template_name_raises_on_query(tmp_path):
    session = RecordingSession({"get": FakeResponse(200, {"data": []})})
    registry = _registry(session, tmp_path / "state.json", FakeClock())
    with pytest.raises(ValueError, match="No template named"):
        registry.query()


def test_unknown_template_fails_open_to_the_legacy_builder_on_current(tmp_path):
    session = RecordingSession({"get": FakeResponse(200, {"data": []})})
    registry = _registry(session, tmp_path / "state.json", FakeClock())
    snapshot = registry.current()
    assert snapshot["active_builder"] == "legacy"
    assert isinstance(registry.choose_builder("aizen_invoice", "en", "20"), LegacyInvoiceTemplateBuilder)


def test_query_targets_the_message_templates_endpoint(tmp_path):
    session = RecordingSession({"get": FakeResponse(200, _graph_data("PENDING", LEGACY_COMPONENTS))})
    registry = _registry(session, tmp_path / "state.json", FakeClock())
    registry.query()
    assert session.urls[0].endswith("/waba123/message_templates")
    params = session.calls[0][1]["params"]
    assert params["name"] == "aizen_invoice"
    assert "status" in params["fields"]
    assert "components" in params["fields"]
    assert session.headers["Authorization"] == "Bearer tok"


def test_template_query_error_keeps_the_meta_code_and_fbtrace(tmp_path):
    # The query used to raise the raw response text, dropping exactly the
    # diagnostics a 132001-class parameter mismatch has to be diagnosed with.
    session = RecordingSession(
        {
            "get": FakeResponse(
                400,
                {
                    "error": {
                        "message": "Invalid parameter",
                        "type": "OAuthException",
                        "code": 132001,
                        "error_data": {"details": "Template parameter count mismatch"},
                        "fbtrace_id": "Abc123",
                    }
                },
            )
        }
    )
    registry = _registry(session, tmp_path / "state.json", FakeClock())
    with pytest.raises(WhatsAppApiError) as excinfo:
        registry.query()
    text = str(excinfo.value)
    assert "template query" in text
    assert "[code 132001]" in text
    assert "Abc123" in text
    assert excinfo.value.payload["error"]["code"] == 132001


def test_binary_garbage_cache_is_treated_as_a_miss_not_a_crash(tmp_path):
    # UnicodeDecodeError is a ValueError, so `except OSError` never caught it and
    # a binary cache file killed the template check instead of re-querying.
    path = tmp_path / "state.json"
    path.write_bytes(b"\x00\xff\xfe\x01binary garbage")
    session = RecordingSession({"get": FakeResponse(200, _graph_data("APPROVED", CLEAN_COMPONENTS))})
    registry = _registry(session, path, FakeClock())
    snapshot = registry.current()
    assert snapshot["active_builder"] == "new"
    assert json.loads(path.read_text())["active_builder"] == "new"
