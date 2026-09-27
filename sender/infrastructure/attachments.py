"""The default attachment strategy: render the PDF, upload it, reference the id.

This is the only attachment path that works out of the box, because it is the
only one that does not need a publicly reachable file: the sender owns the bytes
(``infrastructure/pdf.py``) and Meta receives them from here
(``infrastructure/whatsapp/media.py``).

The other strategy, :class:`sender.domain.attachments.HostedLinkProvider`, is
pure domain and needs no infrastructure at all — it just points Meta at a URL.
"""

from __future__ import annotations

import logging
from hashlib import sha256

from sender.domain.attachments import document_filename
from sender.domain.models import Invoice
from sender.domain.ports import MediaUploader
from sender.infrastructure.pdf import render_invoice_pdf

log = logging.getLogger(__name__)

UPLOAD = "upload"


class UploadedMediaProvider:
    """``domain.ports.InvoiceAttachmentProvider`` backed by a real upload.

    Returns ``{"id": <media_id>, "filename": ...}``. A configured *caption* is
    never sent: Meta does not support captions for the document header
    parameter, so it is dropped with a WARNING instead of rejecting the send.
    The cache key is derived from the invoice *and* the digest of the rendered
    PDF, so an invoice whose totals change is uploaded again instead of reusing
    a stale media id.
    """

    mode = UPLOAD

    def __init__(
        self,
        uploader: MediaUploader,
        caption: str = "",
        render=render_invoice_pdf,
        filename_for=document_filename,
    ) -> None:
        self._uploader = uploader
        self._caption = caption
        self._render = render
        self._filename_for = filename_for

    def build(self, invoice: Invoice) -> dict | None:
        filename = self._filename_for(invoice)
        pdf = self._render(invoice)
        if not pdf:
            log.warning(
                "invoice %s rendered an empty PDF; the header document will be omitted",
                invoice.number,
            )
            return None
        media_id = self._uploader.upload_pdf(
            pdf, filename, cache_key=self._cache_key(invoice, pdf)
        )
        if not media_id:
            log.warning(
                "invoice %s: the media uploader returned no id; the header document "
                "will be omitted",
                invoice.number,
            )
            return None
        log.info("invoice %s: attached %s as media id %s", invoice.number, filename, media_id)
        document = {"id": media_id, "filename": filename}
        if self._caption:
            log.warning(
                "invoice %s: a caption was configured, but Meta does not support "
                "captions for the document header parameter; the caption is omitted "
                "so the send is not rejected",
                invoice.number,
            )
        return document

    @staticmethod
    def _cache_key(invoice: Invoice, pdf: bytes) -> str:
        """Key the media id by invoice *identity* and PDF *content*."""
        identity = f"{invoice.id}:{invoice.number}:{len(pdf)}:{sha256(pdf).hexdigest()}"
        return f"invoice-pdf:{identity}"
