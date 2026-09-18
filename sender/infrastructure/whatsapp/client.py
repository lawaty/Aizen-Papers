from __future__ import annotations

import json
import time
from typing import Mapping

import requests

from sender.domain.errors import WhatsAppApiError
from sender.domain.phones import normalize_phone
from sender.infrastructure.util import json_or_none


class WhatsAppClient:
    _GRAPH_URL = "https://graph.facebook.com"

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        api_version: str = "v25.0",
        timeout: float = 15.0,
        default_country_code: str = "20",
        session: requests.Session | None = None,
        max_retries: int = 3,
    ) -> None:
        self._url = f"{self._GRAPH_URL}/{api_version}/{phone_number_id}/messages"
        self._timeout = timeout
        self._max_retries = max_retries
        self._country_code = default_country_code
        self._session = session or requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        )

    def send(self, payload: Mapping) -> dict:
        body = dict(payload)
        body["to"] = normalize_phone(body.get("to", ""), self._country_code)
        for attempt in range(self._max_retries + 1):
            try:
                response = self._session.post(
                    self._url, data=json.dumps(body), timeout=self._timeout
                )
            except requests.RequestException as exc:
                raise WhatsAppApiError(None, f"network error: {exc}") from exc
            if response.status_code == 429 and attempt < self._max_retries:
                self._sleep_after_429(response, attempt)
                continue
            if not response.ok:
                raise self._to_error(response)
            return response.json()
        raise WhatsAppApiError(None, "rate limited after retries")

    @staticmethod
    def _sleep_after_429(response: requests.Response, attempt: int) -> None:
        raw = response.headers.get("Retry-After", "")
        delay = float(raw) if raw.replace(".", "", 1).isdigit() else 2**attempt
        time.sleep(max(0.0, delay))

    @staticmethod
    def _to_error(response: requests.Response) -> WhatsAppApiError:
        body = json_or_none(response) or {}
        error = body.get("error", {})
        message = error.get("message") or response.text[:300]
        code = error.get("code")
        details = (error.get("error_data") or {}).get("details", "")
        full = f"{message} [code {code}] {details}".strip()
        return WhatsAppApiError(response.status_code, full, body)