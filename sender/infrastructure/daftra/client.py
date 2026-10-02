from __future__ import annotations

from typing import Mapping

import requests

from sender.domain.models import Invoice
from sender.domain.ports import InvoiceSource
from sender.domain.errors import DaftraApiError
from sender.infrastructure.config import DAFTRA_BASE_URL
from sender.infrastructure.util import json_or_none
from .mapper import DaftraInvoiceMapper


class DaftraClient:
    def __init__(
        self,
        api_key: str = "",
        base_url: str = DAFTRA_BASE_URL,
        timeout: float = 15.0,
        session: requests.Session | None = None,
        country_code: str = "20",
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._session = session if session is not None else requests.Session()
        self._api_key = api_key
        if getattr(self._session, "headers", None) is None:
            self._session.headers = {}
        self._remove_auth_headers()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._api_key:
            headers["apikey"] = self._api_key
        self._session.headers.update(headers)
        # The mapper normalizes customer phones, so it needs the deployment's
        # country code, not a baked-in 20.
        self._mapper = DaftraInvoiceMapper(country_code)

    def get_invoice(self, invoice_id: int | str) -> Invoice:
        return self._mapper.to_invoice(self._fetch_invoice(invoice_id))

    def get_raw_invoice(self, invoice_id: int | str) -> dict:
        return self._fetch_invoice(invoice_id)

    def list_invoices(self, limit: int = 10, page: int = 1, filters: Mapping | None = None) -> list[Invoice]:
        params = {"page": page, "limit": limit, **(filters or {})}
        payload = self._request("GET", "/invoices.json", params=params)
        return self._mapper.to_invoices(payload)

    def _fetch_invoice(self, invoice_id: int | str) -> dict:
        sid = str(invoice_id)
        if not sid.isdigit():
            raise ValueError(f"Invalid invoice id: {invoice_id!r}")
        return self._request("GET", f"/invoices/{sid}.json")

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self._base}{path}"
        request_kwargs = dict(kwargs)
        headers = self._clean_auth_headers(request_kwargs.get("headers"))
        if self._api_key:
            headers["apikey"] = self._api_key
        if headers:
            request_kwargs["headers"] = headers

        try:
            response = self._send(method, url, request_kwargs)
        except requests.RequestException as exc:
            raise DaftraApiError(None, f"network error: {exc}") from exc

        status = getattr(response, "status_code", None)
        response_ok = getattr(response, "ok", None)
        if response_ok is None:
            response_ok = status is not None and 200 <= status < 300
        if not response_ok:
            response_text = getattr(response, "text", "") or ""
            raise DaftraApiError(status, response_text[:500], json_or_none(response))
        payload = json_or_none(response)
        if payload is None:
            # A 2xx whose body is not JSON is a proxy/captive-portal/gateway
            # hiccup, not a genuine "no invoices" answer. Reported with a None
            # status (the same shape as a network error) so the poller
            # classifies it as transient and retries, instead of silently
            # looking like an empty listing or being abandoned as permanent.
            raise DaftraApiError(
                None,
                f"response was not JSON (HTTP {status}): "
                f"{getattr(response, 'text', '')[:200]}",
            )
        if isinstance(payload, dict) and payload.get("result") not in (None, "successful", "success"):
            raise DaftraApiError(
                status, str(payload.get("message") or payload)[:200], payload
            )
        return payload or {}

    def _send(self, method: str, url: str, kwargs: dict):
        request = getattr(self._session, "request", None)
        if callable(request):
            return request(method, url, timeout=self._timeout, **kwargs)
        if method.upper() == "GET":
            return self._session.get(url, timeout=self._timeout, **kwargs)
        return self._session.post(url, timeout=self._timeout, **kwargs)

    @staticmethod
    def _is_auth_header(key) -> bool:
        normalized = str(key).lower().replace("-", "").replace("_", "")
        return normalized in {"apikey", "authorization"}

    @classmethod
    def _clean_auth_headers(cls, headers) -> dict:
        return {
            key: value
            for key, value in (headers or {}).items()
            if not cls._is_auth_header(key)
        }

    def _remove_auth_headers(self) -> None:
        headers = getattr(self._session, "headers", None)
        if headers is None:
            return
        for key in list(headers):
            if self._is_auth_header(key):
                del headers[key]
