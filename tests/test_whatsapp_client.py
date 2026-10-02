"""Offline tests for the WhatsApp client self-send guard and error surfacing."""

import pytest

from sender.domain.errors import WhatsAppApiError, WhatsAppSelfSendError
from sender.infrastructure.whatsapp.client import WhatsAppClient

from fakes import FakeResponse, FakeSession

OWN_NUMBER = "201280805534"


def _client(session: FakeSession, own_number: str = OWN_NUMBER, phone_number_id: str = "1304588702742851") -> WhatsAppClient:
    return WhatsAppClient(
        access_token="token",
        phone_number_id=phone_number_id,
        own_number=own_number,
        session=session,
        timeout=1.0,
    )


def _expect_self_send(client: WhatsAppClient, session: FakeSession, to: str) -> str:
    try:
        client.send({"to": to, "type": "text", "text": {"body": "hi"}})
    except WhatsAppSelfSendError as exc:
        assert "own WhatsApp number" in str(exc)
        assert OWN_NUMBER in str(exc)
        return str(exc)
    raise AssertionError("self-send should have been blocked")


def test_self_send_blocked_pre_post_local_format() -> None:
    session = FakeSession()
    client = _client(session)
    _expect_self_send(client, session, "01280805534")
    assert not session.calls


def test_self_send_blocked_pre_post_e164_format() -> None:
    session = FakeSession()
    client = _client(session)
    _expect_self_send(client, session, OWN_NUMBER)
    assert not session.calls


def test_own_number_resolved_via_graph_when_not_configured() -> None:
    session = FakeSession(responses={"get": FakeResponse(200, {"display_phone_number": "01280805534"})})
    client = _client(session, own_number=None)
    _expect_self_send(client, session, OWN_NUMBER)
    assert session.calls and session.calls[0][0] == "get"


def test_send_to_other_number_passes() -> None:
    session = FakeSession()
    client = _client(session)
    result = client.send({"to": "01027693262", "type": "text", "text": {"body": "hi"}})
    assert result["messages"][0]["id"] == "wamid.OK"
    assert session.calls and session.calls[0][0] == "post"


def test_success_status_with_a_non_json_body_is_retryable() -> None:
    # An HTML error page from a proxy answers 200 and is not JSON. Guarding the
    # parse keeps the failure transient (status None) so the poller retries
    # instead of abandoning a customer invoice over a hiccup.
    session = FakeSession(responses={"post": FakeResponse(200, None)})
    client = _client(session)
    with pytest.raises(WhatsAppApiError) as excinfo:
        client.send({"to": "201027693262", "type": "text", "text": {"body": "hi"}})
    assert excinfo.value.status is None
    assert "not JSON" in str(excinfo.value)


def test_retry_after_sleep_is_capped_so_a_cron_run_does_not_hang(monkeypatch) -> None:
    """Meta can answer ``Retry-After: 3600``; honoring it inside a ``poll --once``
    run would hold the poll-state flock for an hour and block every later
    5-minute cron invocation, so the sleep is capped and the next cycle retries.
    """
    import time

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))
    rate_limited = FakeResponse(429, {"error": {"message": "rate limited", "code": 130429}})
    rate_limited.headers = {"Retry-After": "3600"}
    session = FakeSession(
        {"post": [rate_limited, FakeResponse(200, {"messages": [{"id": "wamid.OK"}]})]}
    )
    client = WhatsAppClient(
        access_token="token",
        phone_number_id="1304588702742851",
        own_number="201280805534",
        session=session,
        max_retries=1,
        max_retry_wait=5.0,
    )
    client.send({"to": "201027693262", "type": "text", "text": {"body": "hi"}})
    assert sleeps == [5.0]


def test_error_collects_meta_fields_and_fbtrace() -> None:
    session = FakeSession(
        responses={
            "post": FakeResponse(
                400,
                {
                    "error": {
                        "message": "Invalid parameter",
                        "type": "OAuthException",
                        "code": 100,
                        "error_subcode": 131021,
                        "error_data": {"details": "Recipient cannot be sender"},
                        "fbtrace_id": "Axyz123",
                    }
                },
            )
        }
    )
    client = _client(session)
    try:
        client.send({"to": "201027693262", "type": "text", "text": {"body": "hi"}})
    except WhatsAppApiError as exc:
        text = str(exc)
        assert "[code 100]" in text
        assert "Recipient cannot be sender" in text
        assert "Axyz123" in text
        assert exc.payload["error"]["code"] == 100
        return
    raise AssertionError("api error expected")