from __future__ import annotations

from datetime import date

from .models import Invoice
from .phones import normalize_phone


class InvoiceTemplateBuilder:
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

    def __init__(self, template_name: str, language: str = "en_EG", country_code: str = "20") -> None:
        self._name = template_name
        self._lang = language
        self._country_code = country_code

    def build(self, invoice: Invoice, to_phone: str) -> dict:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_phone(to_phone, self._country_code),
            "type": "template",
            "template": {
                "name": self._name,
                "language": {"code": self._lang},
                "components": [{"type": "body", "parameters": self.parameters(invoice)}],
            },
        }

    def parameters(self, invoice: Invoice) -> list[dict]:
        return [
            self._text(invoice.customer_name),
            self._text(invoice.number),
            self._text(self._format_date(invoice.issue_date)),
            self._text(self._money(invoice.total)),
        ]

    def render_text(self, invoice: Invoice) -> str:
        values = [p["text"] for p in self.parameters(invoice)]
        body = self.TEMPLATE_BODY
        for i, value in enumerate(values, start=1):
            body = body.replace(f"{{{{{i}}}}}", value)
        return body

    def build_text(self, invoice: Invoice, to_phone: str) -> dict:
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_phone(to_phone, self._country_code),
            "type": "text",
            "text": {"body": self.render_text(invoice)},
        }

    def _text(self, value, max_length: int = 512) -> dict:
        return {"type": "text", "text": self._sanitize(value, max_length)}

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