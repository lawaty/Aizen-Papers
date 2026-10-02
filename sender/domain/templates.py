from __future__ import annotations

import logging
import re
from datetime import date

from .attachments import document_filename, pdf_link
from .models import Invoice
from .phones import normalize_phone
from .ports import InvoiceAttachmentProvider

log = logging.getLogger(__name__)

_PLACEHOLDER_PATTERN = re.compile(r"\{\{(\d)\}\}")
#: The opener (FSI/LRI) and the closing PDI, i.e. the characters the isolation
#: wrapper spends on top of the customer's value.
_ISOLATION_MARK_WIDTH = 2


class InvoiceTemplateBuilder:
    """Base template builder: shared parameter formatting and the freeform text path.

    Concrete subclasses implement build(); see LegacyInvoiceTemplateBuilder
    (document header) and CleanTextTemplateBuilder (body-only).
    """

    _LRI = "\u2066"
    _FSI = "\u2068"
    _PDI = "\u2069"

    def _isolate(self, value: str, opener: str) -> str:
        return f"{opener}{value}{self._PDI}"

    TEMPLATE_BODY = (
        "مَرْحَبًا {{1}}، 👋\n\n"
        "نُحيطُكم عِلمًا بأنَّه تمَّ إصدار فاتورة جديدة من Aizen Paper.\n\n"
        "📄 رقم الفاتورة: {{2}}\n"
        "📅 تاريخ الإصدار: {{3}}\n"
        "💰 إجمالي الفاتورة: {{4}} ج.م\n\n"
        "شُكرًا لثقتكم الغالية، ونَسعد دائمًا باستمرار تعاونكم معنا. 🤝\n\n"
        "Aizen Paper\n"
        "✨ ثِقتكم مَحلُّ تقديرنا دائمًا."
    )

    def __init__(
        self,
        template_name: str,
        language: str = "en",
        country_code: str = "20",
        attachment: InvoiceAttachmentProvider | None = None,
    ) -> None:
        self._name = template_name
        self._lang = language
        self._country_code = country_code
        self._attachment = attachment

    @property
    def attachment(self) -> InvoiceAttachmentProvider | None:
        """The provider that supplies the header document, if any."""
        return self._attachment

    @property
    def attachment_mode(self) -> str | None:
        """``"upload"``/``"link"`` for an active provider, else ``None``."""
        return getattr(self._attachment, "mode", None)

    def build(self, invoice: Invoice, to_phone: str) -> dict:
        raise NotImplementedError("choose a concrete template builder")

    def parameters(self, invoice: Invoice) -> list[dict]:
        return [
            self._text(invoice.customer_name, self._FSI),
            self._text(invoice.number, self._LRI),
            self._text(self._format_date(invoice.issue_date), self._LRI),
            self._text(self._money(invoice.total), self._LRI),
        ]

    def render_text(self, invoice: Invoice) -> str:
        values = [p["text"] for p in self.parameters(invoice)]
        mapping = {str(i + 1): value for i, value in enumerate(values)}
        return _PLACEHOLDER_PATTERN.sub(
            lambda match: mapping.get(match.group(1), match.group(0)), self.TEMPLATE_BODY
        )

    def build_text(self, invoice: Invoice, to_phone: str) -> dict:
        """The free-form TEXT message: the same body, rendered as plain text.

        Two callers share this one builder on purpose — ``send --freeform`` (the
        manual layout-testing path) and the poller's template-error fallback — so
        the Arabic body text exists in exactly one place (``TEMPLATE_BODY``) and
        the fallback can never drift from what a manual test shows.
        """
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_phone(to_phone, self._country_code),
            "type": "text",
            "text": {"body": self.render_text(invoice)},
        }

    def _text(self, value, opener: str, max_length: int = 512) -> dict:
        """One body parameter: sanitize/truncate first, *then* isolate.

        Wrapping before truncating spends part of the 512-character budget on
        marks the customer value does not own and, worse, can cut the closing PDI
        off a long name — an unterminated isolate corrupts the bidi rendering of
        everything after it in the message. The two marks are therefore reserved
        *inside* the budget, so the parameter stays within *max_length*.
        """
        text = self._sanitize(value, max_length - _ISOLATION_MARK_WIDTH)
        return {"type": "text", "text": self._isolate(text, opener)}

    @staticmethod
    def _sanitize(value, max_length: int = 512) -> str:
        if value is None:
            return "-"
        text = " ".join(str(value).split())
        return text[:max_length] or "-"

    @staticmethod
    def _money(value) -> str:
        return f"{value:,.2f}"

    @staticmethod
    def _format_date(value: date | None) -> str:
        return value.strftime("%d/%m/%Y") if value else "N/A"


class LegacyInvoiceTemplateBuilder(InvoiceTemplateBuilder):
    """Builder for the document-header version of aizen_invoice (pre-review)."""

    def build(self, invoice: Invoice, to_phone: str) -> dict:
        components: list[dict] = []
        header = self.header_parameter(invoice)
        if header:
            # A header component with an empty ``parameters`` list cannot match the
            # template's DOCUMENT slot, so the component is dropped entirely rather
            # than sent hollow. On a template that requires a document Meta then
            # answers with a parameter mismatch, which the CLI already retries with
            # the other builder.
            components.append({"type": "header", "parameters": header})
        components.append({"type": "body", "parameters": self.parameters(invoice)})
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_phone(to_phone, self._country_code),
            "type": "template",
            "template": {
                "name": self._name,
                "language": {"code": self._lang},
                "components": components,
            },
        }

    def header_parameter(self, invoice: Invoice) -> list[dict]:
        """The DOCUMENT parameter of the legacy template's header component.

        An attached provider wins: it can reference an uploaded media id, which
        is the only form that actually works with a session-gated source. When a
        provider is configured but cannot produce a document (or fails while
        trying), the send degrades to a header-less message with a WARNING
        rather than taking the whole notification down — a poll cycle must never
        die on an attachment. With no provider at all the builder falls back to
        the historic link built from the invoice's PDF url.
        """
        document = self._attachment_document(invoice)
        if document is not None:
            return [{"type": "document", "document": document}]
        if self._attachment is None:
            return [{"type": "document", "document": self._document(invoice)}]
        return []

    def _attachment_document(self, invoice: Invoice) -> dict | None:
        if self._attachment is None:
            return None
        try:
            document = self._attachment.build(invoice)
        except Exception as exc:  # noqa: BLE001 - any adapter failure degrades
            log.warning(
                "invoice %s: the %s attachment provider failed (%s); sending the "
                "message without a header document",
                invoice.number, getattr(self._attachment, "mode", "?"), exc,
            )
            return None
        if not document:
            log.warning(
                "invoice %s: the %s attachment provider produced no document; "
                "sending the message without a header document",
                invoice.number, getattr(self._attachment, "mode", "?"),
            )
            return None
        return document

    def _document(self, invoice: Invoice) -> dict:
        link = pdf_link(invoice)
        if not link:
            raise ValueError(
                f"Invoice {invoice.number} has no public URL for the template header "
                "document (neither pdf_url nor public_url is set)"
            )
        return {"link": link, "filename": document_filename(invoice)}


class CleanTextTemplateBuilder(InvoiceTemplateBuilder):
    """Builder for the approved TEXT-header/footer template (no media header)."""

    def build(self, invoice: Invoice, to_phone: str) -> dict:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_phone(to_phone, self._country_code),
            "type": "template",
            "template": {
                "name": self._name,
                "language": {"code": self._lang},
                "components": [
                    {"type": "body", "parameters": self.parameters(invoice)},
                ],
            },
        }


def build_fallback_document(recipient: str, document: dict) -> dict:
    """The free-form DOCUMENT message used while the template is unusable.

    A template send rejected with a ``132000``-series code never delivered
    anything, but the header document it referenced is *still uploaded and still
    live* — the media id in it is the sender's own upload. So the fallback
    reuses that id instead of rendering and uploading the PDF a second time.

    *document* is that header document object (``{"id": ..., "filename": ...}``)
    and *recipient* is the already-normalized E.164 recipient, so this stays a
    pure function of the two.

    No ``caption``: caption support on a free-form document message is unverified,
    and the repo already has to drop the caption from a template header for the
    same reason (see ``domain.ports.InvoiceAttachmentProvider``). Guessing here
    would turn a fallback that works into one Meta rejects outright.
    """
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "document",
        "document": {
            "id": document["id"],
            "filename": document["filename"],
        },
    }
