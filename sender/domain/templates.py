from __future__ import annotations

import logging
import re
from datetime import date

from .attachments import document_filename, pdf_link
from .models import Customer, Invoice, Payment
from .phones import normalize_phone
from .ports import InvoiceAttachmentProvider

log = logging.getLogger(__name__)

_PLACEHOLDER_PATTERN = re.compile(r"\{\{(\d)\}\}")
#: The opener (FSI/LRI) and the closing PDI, i.e. the characters the isolation
#: wrapper spends on top of the customer's value.
_ISOLATION_MARK_WIDTH = 2


class TemplateBuilder:
    """Everything the invoice and payment builders have in common.

    Both pipelines speak to Meta through a *different approved template*, but the
    mechanics of doing so are identical: the same 4 positional body parameters,
    the same ``DD/MM/YYYY`` / ``1,500.00`` formatting, the same whitespace
    sanitizing, the same bidi isolation, and the same free-form text path used by
    ``--freeform`` and by the poller's template-error fallback.

    Keeping that here is the point: those rules are customer-visible, so having
    them spelled once is what stops the second template from drifting away from
    the first. What is deliberately *not* here is the payload shape. ``build`` is
    abstract and each document type owns its own components, because the two
    approved templates genuinely differ: ``aizen_invoice`` has a DOCUMENT header
    (hence the two invoice builders) while ``aizen_new_payment`` has none.

    A payment builder is therefore *not* an ``InvoiceTemplateBuilder``: the CLI's
    builder-swap retry and the attachment plumbing test for the invoice
    builders specifically, and a payment builder must never be mistaken for one.
    """

    _LRI = "⁦"
    _FSI = "⁨"
    _PDI = "⁩"

    #: The template body as free-form text, with ``{{n}}`` placeholders. Each
    #: concrete builder declares its own next to its parameter logic, so a
    #: template re-approval or a placeholder reorder is a one-file diff. Empty
    #: here: this class does not speak for any specific Meta template.
    TEMPLATE_BODY = ""

    def __init__(
        self,
        template_name: str,
        language: str = "en",
        country_code: str = "20",
    ) -> None:
        self._name = template_name
        self._lang = language
        self._country_code = country_code

    def _isolate(self, value: str, opener: str) -> str:
        return f"{opener}{value}{self._PDI}"

    def build(self, document, to_phone: str) -> dict:
        raise NotImplementedError("choose a concrete template builder")

    def parameters(self, document) -> list[dict]:
        raise NotImplementedError("implement the template's ordered parameters")

    def render_text(self, document) -> str:
        values = [p["text"] for p in self.parameters(document)]
        mapping = {str(i + 1): value for i, value in enumerate(values)}
        return _PLACEHOLDER_PATTERN.sub(
            lambda match: mapping.get(match.group(1), match.group(0)), self.TEMPLATE_BODY
        )

    def build_text(self, document, to_phone: str) -> dict:
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
            "text": {"body": self.render_text(document)},
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


class InvoiceTemplateBuilder(TemplateBuilder):
    """Invoice template builder: the 4 invoice parameters and the attachment port.

    Concrete subclasses implement build(); see LegacyInvoiceTemplateBuilder
    (document header) and CleanTextTemplateBuilder (body-only).
    """

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
        super().__init__(template_name, language, country_code)
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


class PaymentTemplateBuilder(TemplateBuilder):
    """Builder for ``aizen_new_payment``: the payment-confirmation template.

    The approved template has a BODY and a FOOTER and **no header at all**, so
    unlike the invoice path there is exactly one legal payload shape and hence no
    builder to choose between at runtime:

    - the footer carries no variables, so Meta fills it from the template and the
      payload sends nothing for it;
    - with no header there is no document to attach, so this builder takes no
      ``InvoiceAttachmentProvider`` and the payments pipeline never renders or
      uploads a PDF. That is also why the payments path does not consult
      ``TemplateRegistry`` — that exists solely to decide between the two invoice
      builders, and there is nothing here to decide.

    The default language is ``ar_EG`` because that is the only language the
    template is approved in; the invoice template's default is ``en``. Getting
    this wrong is a ``132001`` at send time, so it is pinned by a test.

    The four parameters are the payment's own facts in the order the body reads
    them: who, which receipt, when, how much.
    """

    #: The live body of ``aizen_new_payment`` as plain text, for
    #: ``send-payment --freeform`` and for the poller's template-error fallback.
    #:
    #: It deliberately does **not** carry the U+2068/U+2069 isolation marks that
    #: the approved template embeds around its placeholders. This string is the
    #: input to :meth:`TemplateBuilder.render_text`, which substitutes values
    #: that :meth:`TemplateBuilder._text` has *already* isolated; keeping the
    #: template's own marks would wrap every value twice (FSI FSI … PDI PDI) and
    #: corrupt the bidi rendering. The invoice body is mark-free for the same
    #: reason. The template Meta actually serves is Meta's own text and is not
    #: affected by this constant.
    PAYMENT_TEMPLATE_BODY = (
        "*تأكيد استلام نقدية | Aizen Paper*\n\n"
        "مَرْحَبًا {{1}}، 👋\n\n"
        "نُحيطكم عِلمًا بأنَّه تمَّ تسجيل دفعة جديدة على حسابكم لدى Aizen Paper.\n\n"
        "🧾 رقم العملية: {{2}}\n"
        "📅 تاريخ الدفع: {{3}}\n"
        "💰 مبلغ الدفعة: {{4}} ج.م\n\n"
        "تم تحديث رصيد حسابكم بنجاح.\n\n"
        "شُكرًا لسدادكم، ونسعد دائمًا باستمرار تعاوننا معكم."
    )
    TEMPLATE_BODY = PAYMENT_TEMPLATE_BODY

    def __init__(
        self,
        template_name: str = "aizen_new_payment",
        language: str = "ar_EG",
        country_code: str = "20",
    ) -> None:
        super().__init__(template_name, language, country_code)

    def parameters(self, payment: Payment) -> list[dict]:
        return [
            self._text(payment.customer_name, self._FSI),
            self._text(payment.number, self._LRI),
            self._text(self._format_date(payment.payment_date), self._LRI),
            self._text(self._money(payment.amount), self._LRI),
        ]

    def build(self, payment: Payment, to_phone: str) -> dict:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_phone(to_phone, self._country_code),
            "type": "template",
            "template": {
                "name": self._name,
                "language": {"code": self._lang},
                "components": [
                    {"type": "body", "parameters": self.parameters(payment)},
                ],
            },
        }


class CustomerTemplateBuilder(TemplateBuilder):
    """Builder for ``aizen_new_customer``: the new-customer welcome template.

    Structurally the same situation as :class:`PaymentTemplateBuilder` — an
    approved template with a BODY and a FOOTER and **no header**, so one legal
    payload shape, no builder to choose, no document to attach and no
    ``TemplateRegistry`` lookup — but the payload is the simplest of the three:
    **exactly one** parameter, the customer's name.

    It subclasses :class:`TemplateBuilder` directly and never
    :class:`InvoiceTemplateBuilder`, deliberately: the CLI's invoice builder-swap
    retry identifies an invoice builder by type, and a welcome builder that looked
    like an invoice builder could be picked up by a path that has no business
    touching it.

    The default language is ``ar_EG`` — the only language this template is
    approved in — and the category is **MARKETING**, not UTILITY. That matters
    outside this class: it is why the customers pipeline's free-form fallback
    defaults to off (see ``WHATSAPP_CUSTOMER_FREEFORM_FALLBACK``).
    """

    #: The live body of ``aizen_new_customer`` as plain text, for
    #: ``send-customer --freeform`` and for the poller's template-error fallback.
    #:
    #: Verbatim from the approved template, and for the same reason
    #: :attr:`PaymentTemplateBuilder.PAYMENT_TEMPLATE_BODY` is: it carries **no**
    #: U+2068/U+2069 isolation marks, because :meth:`TemplateBuilder._text` has
    #: already isolated each value before :meth:`TemplateBuilder.render_text`
    #: substitutes it, and keeping the template's marks would wrap every value
    #: twice and corrupt the bidi rendering.
    CUSTOMER_TEMPLATE_BODY = (
        "*أهلًا بكم في Aizen Paper 👋*\n\n"
        "مَرْحَبًا {{1}}،\n\n"
        "يسعدنا انضمامكم إلى عملاء Aizen Paper، ونرحب ببداية تعاون مثمر ومستمر معكم. 🤝\n\n"
        "تم تسجيل حسابكم بنجاح في نظامنا، ونتطلع دائمًا لتقديم أفضل خدمة لكم."
    )
    TEMPLATE_BODY = CUSTOMER_TEMPLATE_BODY

    def __init__(
        self,
        template_name: str = "aizen_new_customer",
        language: str = "ar_EG",
        country_code: str = "20",
    ) -> None:
        super().__init__(template_name, language, country_code)

    def parameters(self, customer: Customer) -> list[dict]:
        # One parameter, and the greeting is the whole message: the customer's
        # name is the only variable in the approved body.
        return [self._text(customer.customer_name, self._FSI)]

    def build(self, customer: Customer, to_phone: str) -> dict:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_phone(to_phone, self._country_code),
            "type": "template",
            "template": {
                "name": self._name,
                "language": {"code": self._lang},
                "components": [
                    {"type": "body", "parameters": self.parameters(customer)},
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
