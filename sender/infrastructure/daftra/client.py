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
        api_key: str,
        base_url: str = DAFTRA_BASE_URL,
        timeout: float = 15.0,
        session: requests.Session | None = None,
        bearer_token: str | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._session = session or requests.Session()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        else:
            headers["apikey"] = api_key
        self._session.headers.update(headers)
        self._mapper = DaftraInvoiceMapper()

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
        try:
            response = self._session.request(method, url, timeout=self._timeout, **kwargs)
        except requests.RequestException as exc:
            raise DaftraApiError(None, f"network error: {exc}") from exc
        if not response.ok:
            raise DaftraApiError(response.status_code, response.text[:500], json_or_none(response))
        payload = json_or_none(response)
        if isinstance(payload, dict) and payload.get("result") not in (None, "successful", "success"):
            raise DaftraApiError(
                response.status_code, str(payload.get("message") or payload)[:200], payload
            )
        return payload or {}