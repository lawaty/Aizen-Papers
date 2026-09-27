"""Offline specs for the Daftra HTTP client's transport error handling."""

import pytest

from sender.domain.errors import DaftraApiError
from sender.infrastructure.daftra.client import DaftraClient
from tests.fakes import FakeResponse, FakeSession


def test_a_non_json_2xx_body_is_retryable_not_an_empty_listing():
    """A proxy/captive-portal 2xx that is not JSON must not look like an empty
    invoice list — that is how an outage becomes invisible to the cron poller.

    The error carries ``status=None`` (the same shape as a network error), so the
    poller classifies it as transient and retries instead of reporting "0 new".
    """
    session = FakeSession({"get": FakeResponse(200, None)})
    client = DaftraClient(api_key="key", base_url="https://acme.daftra.com/api2", session=session)
    with pytest.raises(DaftraApiError) as excinfo:
        client.list_invoices()
    assert excinfo.value.status is None
    assert "not JSON" in str(excinfo.value)


def test_a_json_body_still_lists_invoices():
    session = FakeSession({"get": FakeResponse(200, {"data": [{"Invoice": {"id": "1", "no": "000001"}}]})})
    client = DaftraClient(api_key="key", base_url="https://acme.daftra.com/api2", session=session)
    invoices = client.list_invoices()
    assert [invoice.id for invoice in invoices] == ["1"]


def test_a_5xx_still_raises_the_typed_error():
    session = FakeSession({"get": FakeResponse(503, None)})
    client = DaftraClient(api_key="key", base_url="https://acme.daftra.com/api2", session=session)
    with pytest.raises(DaftraApiError) as excinfo:
        client.list_invoices()
    assert excinfo.value.status == 503