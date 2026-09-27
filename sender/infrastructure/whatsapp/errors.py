"""Shared formatting for Meta Graph API error bodies.

The Graph API returns the same envelope for every endpoint — messages, media
uploads, template reads — so the code that unpacks it lives here and every
caller raises the same :class:`~sender.domain.errors.WhatsAppApiError` with the
same fields. That matters beyond tidiness: the poller classifies failures from
``error.status`` (see ``application/poller.py``), so a media-upload failure has
to arrive as a properly-shaped error to be retried or abandoned correctly.
"""

from __future__ import annotations

import requests

from sender.domain.errors import WhatsAppApiError
from sender.infrastructure.util import json_or_none


def to_api_error(
    response: requests.Response, context: str = ""
) -> WhatsAppApiError:
    """Turn a non-2xx Graph API response into a :class:`WhatsAppApiError`.

    Keeps every diagnostic Meta offers — message, code, ``error_data.details``
    and the ``fbtrace_id`` that support needs — because the error text is what
    ends up in the poller's ``pending``/``abandoned`` records. *context* names
    the endpoint ("media upload") so the message says which call failed.
    """
    body = json_or_none(response) or {}
    error = body.get("error", {}) if isinstance(body, dict) else {}
    error = error if isinstance(error, dict) else {}
    message = error.get("message") or response.text[:300]
    code = error.get("code")
    details = (error.get("error_data") or {}).get("details", "")
    fb_trace = error.get("fbtrace_id")
    bits = [f"{context}: {message}" if context else str(message)]
    if code is not None:
        bits.append(f"[code {code}]")
    if details:
        bits.append(str(details))
    if fb_trace:
        bits.append(f"[fbtrace {fb_trace}]")
    return WhatsAppApiError(response.status_code, " ".join(bits), body)
