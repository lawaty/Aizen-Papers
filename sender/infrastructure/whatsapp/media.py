"""Uploads the invoice PDF to Meta and returns the ``media_id`` to reference.

Daftra v2 has no PDF export endpoint and its ``invoice_pdf_url`` is session-gated
(it redirects to a login page), so neither Meta nor this process can fetch the
file from Daftra. The bytes are rendered locally (``infrastructure/pdf.py``) and
handed to Meta here, which yields the id that a template header's ``document``
object points at.

Protocol (Graph API ``/{version}/{phone-number-id}/media``):

- a single ``POST`` with ``multipart/form-data``: the required
  ``messaging_product`` field plus the PDF as the ``file`` part (with its
  filename and ``Content-Type: application/pdf``). The response carries the new
  media id.

Only ``requests`` is used, with the same session/timeout/retry conventions as
``whatsapp/client.py``. Errors surface as :class:`WhatsAppApiError` so the
poller classifies an upload failure exactly like a send failure.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from sender.domain.errors import WhatsAppApiError
from sender.infrastructure.util import json_or_none
from sender.infrastructure.whatsapp.errors import to_api_error

log = logging.getLogger(__name__)

_GRAPH_URL = "https://graph.facebook.com"
_PDF_MIME = "application/pdf"


class MetaMediaUploader:
    """``domain.ports.MediaUploader`` backed by the Graph API media endpoint.

    Uploaded media ids are single-use and expire, so they are cached by
    *cache_key* to avoid re-uploading a byte-identical PDF when the same invoice
    is previewed and then sent, or when a retry re-builds the payload.
    """

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        api_version: str = "v25.0",
        timeout: float = 60.0,
        session: requests.Session | None = None,
        max_retries: int = 3,
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        self._base = f"{_GRAPH_URL}/{api_version}/{phone_number_id}/media"
        self._session = session or requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {access_token}"})
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()

    def upload_pdf(
        self, pdf: bytes, filename: str, *, cache_key: str | None = None
    ) -> str:
        """Upload *pdf* and return its Meta media id.

        The id is remembered under *cache_key* and returned unchanged on a
        repeat call, so the same invoice does not consume a second upload.
        """
        if not pdf:
            raise WhatsAppApiError(None, "refusing to upload an empty PDF")
        if cache_key:
            with self._lock:
                cached = self._cache.get(cache_key)
            if cached:
                log.debug("reusing cached Meta media id %s for %s", cached, cache_key)
                return cached

        media_id = self._upload(pdf, filename)
        if cache_key:
            with self._lock:
                self._cache[cache_key] = media_id
        return media_id

    # --- protocol ----------------------------------------------------------------

    def _upload(self, pdf: bytes, filename: str) -> str:
        """POST the PDF as a multipart ``file`` part and return the media id.

        The Graph API documents this endpoint as ``multipart/form-data`` with a
        ``messaging_product`` field and the file bytes as the ``file`` part; the
        filename and MIME type ride on that part. The response is ``{"id": ...}``.
        """
        response = self._post(
            self._base,
            data={"messaging_product": "whatsapp"},
            files={"file": (filename, pdf, _PDF_MIME)},
        )
        body = self._json(response)
        media_id = str(body.get("id") or "").strip()
        if not media_id:
            raise WhatsAppApiError(
                response.status_code,
                "media upload returned no media id to reference, so the PDF cannot "
                f"be attached (response keys: {sorted(body)[:6]})",
                body,
            )
        log.debug("uploaded %s (%d bytes) as media id %s", filename, len(pdf), media_id)
        return media_id

    # --- transport --------------------------------------------------------------

    def _post(
        self, url: str, *, data: dict, files: dict
    ) -> requests.Response:
        """POST a multipart body, retrying only HTTP 429.

        Transport errors are *not* retried here: they raise immediately as a
        :class:`WhatsAppApiError` with ``status=None``, which the poller already
        classifies as retryable and retries at cycle level with its own bounded
        backoff — where the invoice is still tracked as pending. Retrying them
        inside the upload would only hide a failing endpoint behind a few silent
        attempts.
        """
        for attempt in range(self._max_retries + 1):
            try:
                response = self._session.post(
                    url, data=data, files=files, timeout=self._timeout
                )
            except requests.RequestException as exc:
                raise WhatsAppApiError(None, f"media upload network error: {exc}") from exc
            if response.status_code == 429 and attempt < self._max_retries:
                time.sleep(2**attempt)
                continue
            if not response.ok:
                raise to_api_error(response, "media upload")
            return response
        raise WhatsAppApiError(None, "media upload rate limited after retries")

    @staticmethod
    def _json(response: requests.Response) -> dict:
        """The response body as a dict, whatever the API decided to send back."""
        body = json_or_none(response)
        return body if isinstance(body, dict) else {}
