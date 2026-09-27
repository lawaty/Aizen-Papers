"""The API error hierarchy, plus the predicates that classify an error.

The predicates live in the domain because *which class of failure* an operator can
act on is a business rule, not a transport detail: the poller retries a template
contract failure (an operator can approve the template) and the CLI reacts to it
by retrying with the other builder.
"""

#: Meta's template-contract error class. Every ``132000``-series code means "the
#: payload did not match the template" (132000 parameter mismatch, 132001 the
#: template/translation is not usable, 132012 a missing header DOCUMENT, ...), and
#: the class ends where the next one starts at 133000.
TEMPLATE_ERROR_CODE_RANGE = (132000, 133000)


def is_template_error(exc: object) -> bool:
    """True when *exc* is a Meta template-contract rejection (the 132000-series).

    Deliberately duck-typed on ``.payload``: the caller holds whatever the sender
    raised, and a non-API exception (or one whose body never made it into the
    error) simply is not a template error. That is the same test the CLI applied
    inline when it retried ``send`` with the other builder; it lives here now so
    the poller and the CLI agree on what "the template is the problem" means.
    """
    payload = getattr(exc, "payload", None) or {}
    code = (payload.get("error") or {}).get("code")
    if not isinstance(code, int):
        return False
    low, high = TEMPLATE_ERROR_CODE_RANGE
    return low <= code < high


class ApiError(Exception):
    def __init__(self, service: str, status: int | None, message: str, payload: dict | None = None) -> None:
        detail = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"{service} error{detail}: {message}")
        self.service = service
        self.status = status
        self.payload = payload or {}


class DaftraApiError(ApiError):
    def __init__(self, status: int | None, message: str, payload: dict | None = None) -> None:
        super().__init__("Daftra", status, message, payload)


class WhatsAppApiError(ApiError):
    def __init__(self, status: int | None, message: str, payload: dict | None = None) -> None:
        super().__init__("WhatsApp", status, message, payload)


class WhatsAppSelfSendError(ApiError):
    def __init__(self, own_number: str) -> None:
        super().__init__(
            "WhatsApp",
            None,
            f"recipient is this business's own WhatsApp number ({own_number}), "
            "which Meta rejects as a self-send (#100/131021); use a different recipient",
        )