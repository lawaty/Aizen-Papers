"""The invoice-PDF attachment contract for the template header.

Pure domain: no I/O, no HTTP. It owns two things the payload depends on:

1. which URL is the *PDF* of an invoice (``pdf_link``), and
2. the filename convention WhatsApp shows for the document
   (``document_filename``).

The two provider strategies split on **who owns the bytes**:

- :class:`HostedLinkProvider` (here) — the file is already reachable at a public
  URL, so the header references it by ``link``. Pure logic, no dependencies.
- ``UploadedMediaProvider`` (``infrastructure/attachments.py``) — we render the
  PDF and upload it, so the header references an uploaded asset by ``id``.

Both satisfy ``domain.ports.InvoiceAttachmentProvider`` and may return ``None``
when no attachment can be produced; the caller then sends without the header
and logs a warning instead of failing the whole notification.
"""
from __future__ import annotations

import logging
import re

from .models import Invoice
from .ports import InvoiceAttachmentProvider  # noqa: F401  (re-exported for convenience)

log = logging.getLogger(__name__)

#: Anything outside this set is replaced in the filename. The invoice number is
#: attacker-adjacent data (it comes from an external ERP) and the filename ends
#: up in a MediaObject, so path separators must never survive.
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

LINK = "link"
UPLOAD = "upload"
NONE = "none"


def document_filename(invoice: Invoice) -> str:
    """``<invoice number>.pdf``, sanitized (``INV-001`` -> ``INV-001.pdf``)."""
    number = _UNSAFE_FILENAME.sub("_", str(invoice.number or "invoice").strip()) or "invoice"
    return f"{number}.pdf"


def pdf_link(invoice: Invoice) -> str | None:
    """The best available URL for the invoice **PDF**.

    ``Invoice.pdf_url`` is the real thing (``invoice_pdf_url`` in the Daftra
    payload). ``Invoice.public_url`` is only a fallback for sources that expose
    no PDF url at all (older payloads, the offline stub, hand-built models) —
    preferring it would point the header at Daftra's HTML preview, which is a
    page behind a login, not a document.
    """
    return invoice.pdf_url or invoice.public_url


class HostedLinkProvider:
    """``InvoiceAttachmentProvider`` for deployments that can host the PDF.

    Returns ``{"link": <public pdf url>, "filename": ...}``. Meta fetches the
    URL itself, so the file must be publicly reachable without a session — which
    is **not** the case for Daftra's own ``invoice_pdf_url``. That is why this is
    not the default; it exists for operators who put the file on their own
    public host.
    """

    mode = LINK

    def __init__(self, caption: str = "", filename_for=document_filename) -> None:
        self._caption = caption
        self._filename_for = filename_for

    def build(self, invoice: Invoice) -> dict | None:
        link = pdf_link(invoice)
        if not link:
            log.warning(
                "invoice %s has no PDF url; the WhatsApp header document will be omitted",
                invoice.number,
            )
            return None
        document = {"link": link, "filename": self._filename_for(invoice)}
        if self._caption:
            log.warning(
                "invoice %s: a caption was configured, but Meta does not support "
                "captions for the document header parameter; the caption is omitted "
                "so the send is not rejected",
                invoice.number,
            )
        return document


class NoAttachmentProvider:
    """The explicit ``none`` mode: no document, so no header component.

    This exists as a *provider* rather than as a ``None`` provider on purpose.
    ``None`` means "no attachment strategy configured", and the builder then keeps
    its historic behaviour of building a link document from the invoice's URL —
    which is what existing callers and the payload-contract tests rely on. ``none``
    is an operator saying "attach nothing", and it has to be distinguishable from
    that, or the flag would silently do the opposite of what it says.
    """

    mode = NONE

    def build(self, invoice: Invoice) -> dict | None:
        log.info("invoice attachments are disabled; sending without a header document")
        return None
