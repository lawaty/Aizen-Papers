from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable

from .models import Invoice


class InvoiceSource(Protocol):
    def get_invoice(self, invoice_id: int | str) -> Invoice: ...
    def get_raw_invoice(self, invoice_id: int | str) -> dict: ...
    def list_invoices(self, limit: int = 10, page: int = 1) -> list[Invoice]: ...


@runtime_checkable
class MessageSender(Protocol):
    def send(self, payload: dict) -> dict: ...


@runtime_checkable
class MediaUploader(Protocol):
    """Uploads bytes to the channel and returns an opaque media id.

    The invoice PDF is generated locally (Daftra exposes no PDF export and its
    ``invoice_pdf_url`` is session-gated, so neither we nor Meta can fetch it),
    which means the bytes have to be handed to Meta ourselves. The id returned
    here is what a template header ``document`` object references.

    ``cache_key`` lets an adapter reuse an already-uploaded asset for the same
    invoice instead of uploading the same PDF again.
    """

    def upload_pdf(
        self, pdf: bytes, filename: str, *, cache_key: str | None = None
    ) -> str: ...


@runtime_checkable
class InvoiceAttachmentProvider(Protocol):
    """Builds the Meta ``document`` object for a template header.

    Return either ``{"id": <media_id>}`` (uploaded asset) or
    ``{"link": <public url>}`` (hosted asset), optionally with ``filename``.
    Captions are not supported for the document header parameter, so
    implementations must not send one. Returning ``None`` means "no attachment
    available"; the caller then sends without the header and logs a warning
    rather than failing.
    """

    mode: str

    def build(self, invoice: Invoice) -> dict | None: ...


class PollStateStore(Protocol):
    """Per-app record of which invoices were already handled.

    The poller uses this to decide what is "new" and to persist that decision
    across processes. Implementations own the persistence (JSON file, memory).

    Per app the store keeps:
    - ``seen``: invoice ids already handled (sent, skipped, or permanently
      abandoned);
    - ``pending``: invoice ids with a retryable failure (network/429/5xx) that
      will be retried, each carrying the attempt count, the last error, and the
      next-attempt timestamp (bounded backoff);
    - ``abandoned``: invoice ids given up after a permanent failure (validation,
      missing public_url, template rejection, self-send), each carrying the
      attempt count and the last error so an operator can see and re-drive them;
    - ``last_poll_at``: wall-clock timestamp of the last completed cycle.
    """
    def has_app(self, app_name: str) -> bool: ...
    def app_names(self) -> list[str]: ...
    def seen(self, app_name: str, invoice_id: str) -> bool: ...
    def seen_ids(self, app_name: str) -> list[str]: ...
    def mark_seen(self, app_name: str, invoice_id: str) -> None: ...
    def mark_many_seen(self, app_name: str, invoice_ids: list[str]) -> None: ...
    def last_poll_at(self, app_name: str) -> float | None: ...
    def set_last_poll_at(self, app_name: str, timestamp: float) -> None: ...
    def pending(self, app_name: str) -> dict: ...
    def abandoned(self, app_name: str) -> dict: ...
    def record_pending(
        self,
        app_name: str,
        invoice_id: str,
        *,
        error: str,
        action: str,
        count: int,
        next_attempt_at: float,
    ) -> None: ...
    def record_abandoned(
        self,
        app_name: str,
        invoice_id: str,
        *,
        error: str,
        action: str,
        count: int,
    ) -> None: ...
    def reset_app(self, app_name: str) -> None: ...
    def clear_invoice(self, app_name: str, invoice_id: str) -> None: ...
    def batch(self) -> AbstractContextManager[None]: ...


class Clock(Protocol):
    """Wall/monotonic time and sleep, injected so the poll loop is testable."""

    def monotonic(self) -> float: ...
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...