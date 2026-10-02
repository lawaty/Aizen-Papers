from __future__ import annotations

import json
import logging
import time
from typing import Mapping

import requests

from sender.domain.errors import WhatsAppApiError, WhatsAppSelfSendError
from sender.domain.phones import normalize_phone
from sender.infrastructure.util import json_or_none
from sender.infrastructure.whatsapp.errors import to_api_error

log = logging.getLogger(__name__)

#: Stands in for "we asked Meta and could not find out", so a failed lookup is
#: remembered instead of re-issued on every send. It is not a dialable string, so
#: it can only ever *disable* the self-send guard and never trip it.
_OWN_NUMBER_UNKNOWN = "\x00own-number-unknown"


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
        own_number: str | None = None,
        max_retry_wait: float = 30.0,
    ) -> None:
        self._url = f"{self._GRAPH_URL}/{api_version}/{phone_number_id}/messages"
        self._api_version = api_version
        self._phone_number_id = phone_number_id
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_retry_wait = max_retry_wait
        self._country_code = default_country_code
        self._own_number: str | None = (
            normalize_phone(own_number, default_country_code) if own_number else None
        )
        self._session = session or requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        )

    def send(self, payload: Mapping) -> dict:
        body = dict(payload)
        body["to"] = normalize_phone(body.get("to", ""), self._country_code)
        self._assert_not_self_send(body["to"])
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
            body_json = json_or_none(response)
            if body_json is None:
                # A 2xx whose body is not JSON is a proxy/captive-portal hiccup,
                # not a rejected message. Unguarded, response.json() raised a
                # ValueError that the poller classified PERMANENT and abandoned
                # the invoice; status None is the shape it retries.
                raise WhatsAppApiError(
                    None,
                    f"response was not JSON (HTTP {response.status_code}): "
                    f"{response.text[:200]}",
                )
            return body_json
        raise WhatsAppApiError(None, "rate limited after retries")

    def _assert_not_self_send(self, recipient: str) -> None:
        if self._own_number is None:
            self._own_number = self._fetch_own_number()
        # A sentinel is never equal to a normalized recipient (it contains a NUL),
        # so an unresolved lookup disables the guard and can never trip it.
        if self._own_number and recipient == self._own_number:
            raise WhatsAppSelfSendError(self._own_number)

    def _fetch_own_number(self) -> str | None:
        if self._own_number is not None:
            # Either resolved on an earlier call, or already known to be
            # unresolvable — either way, do not ask Meta again.
            return self._own_number
        url = f"{self._GRAPH_URL}/{self._api_version}/{self._phone_number_id}?fields=display_phone_number"
        try:
            response = self._session.get(url, timeout=self._timeout)
            if not response.ok:
                # Checked explicitly: ``json_or_none`` only guards the *parse*, so
                # a 500 carrying a JSON error body would otherwise read as a
                # successful answer that simply had no number in it.
                raise WhatsAppApiError(
                    response.status_code, f"could not read our own number: {response.text[:200]}"
                )
            own = (json_or_none(response) or {}).get("display_phone_number", "")
            if not own:
                # We asked, and got no answer — the same consequence as an error,
                # so it is reported the same way.
                log.warning(
                    "the Graph API returned no display_phone_number; the self-send "
                    "guard is off for the rest of this run"
                )
                return _OWN_NUMBER_UNKNOWN
            return normalize_phone(own, self._country_code)
        except (ValueError, WhatsAppApiError, requests.RequestException) as exc:
            # Returning None here silently disabled the guard: the caller cannot
            # tell "we do not know our own number" from "we could not ask", and
            # that difference is the guard's whole purpose. Say so — and return
            # the sentinel, so a broken lookup costs one request per process
            # rather than one per send.
            log.warning(
                "could not read our own WhatsApp number (%s); the self-send guard is "
                "off for the rest of this run",
                exc,
            )
            return _OWN_NUMBER_UNKNOWN

    def _sleep_after_429(self, response: requests.Response, attempt: int) -> None:
        raw = response.headers.get("Retry-After", "")
        delay = float(raw) if raw.replace(".", "", 1).isdigit() else 2**attempt
        # Meta can ask for a very long wait (``Retry-After: 3600``). Honoring it
        # inside a ``poll --once`` run would hold the poll-state flock for an
        # hour and block every later 5-minute cron invocation on it, so the wait
        # is capped: the next cron cycle retries anyway, and the lock stays free.
        time.sleep(max(0.0, min(delay, self._max_retry_wait)))

    @staticmethod
    def _to_error(response: requests.Response) -> WhatsAppApiError:
        return to_api_error(response)
