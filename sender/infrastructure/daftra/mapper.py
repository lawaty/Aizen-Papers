from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sender.domain.models import Invoice, InvoiceItem
from sender.domain.phones import normalize_phone

log = logging.getLogger(__name__)

STATUS_LABELS = {
    -1: "Draft",
    0: "Unpaid",
    1: "Partially Paid",
    2: "Paid",
    3: "Refunded",
    4: "Overpaid",
}

NUMBER_KEYS = ("no", "number", "code")
TOTAL_KEYS = ("summary_total", "total", "total_amount")
SUBTOTAL_KEYS = ("summary_subtotal", "subtotal", "sub_total")
PAID_KEYS = ("summary_paid", "total_paid", "paid_amount")
BALANCE_KEYS = ("summary_unpaid", "balance_due", "remaining", "due_amount")
ISSUE_KEYS = ("date", "issue_date", "invoice_date")
CURRENCY_KEYS = ("currency_code", "currency", "default_currency_code")
#: The human-facing page the customer can open. Deliberately *not* the PDF: the
#: template's body already links to this, and it is what the mapper has always
#: exposed, so keeping the two apart is a behaviour change in name only.
PUBLIC_URL_KEYS = ("invoice_html_url", "public_url", "permalink")
#: The actual PDF. Daftra only ever exposes this as ``invoice_pdf_url``, and it
#: is session-gated (it redirects to the login page without a browser session),
#: which is why the sender renders its own copy instead of linking to it.
PDF_URL_KEYS = ("invoice_pdf_url",)

#: Arabic/Persian localizations of the ASCII number glyphs the rest of ``_money``
#: assumes. Without this fold, an amount a proxy localized for the customer's
#: locale would have its separators stripped by the regex and silently change
#: magnitude (``١٢٥٠٫٠٠`` would become 125000), or its Unicode minus (``−``)
#: would be dropped and a credit read as a positive charge. ``str.translate``
#: maps each source code point to one target code point.
_MONEY_TRANSLATIONS = str.maketrans(
    {
        "٫": ".",  # Arabic decimal separator
        "٬": ",",  # Arabic thousands separator
        "−": "-",  # Unicode minus
        "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
        "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",  # Arabic-Indic digits
        "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
        "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",  # Extended Arabic-Indic
    }
)


def _raise_money(value, cause: Exception | None = None) -> Decimal:
    """Raise the mapper's canonical "this amount cannot be parsed" error.

    Returning a helper keeps the message and the exception type in one place so
    every caller (the integer path, the string path, the ``Decimal`` fallback)
    fails identically.
    """
    error = ValueError(f"Unparseable money value: {value!r}")
    if cause is not None:
        raise error from cause
    raise error


def _row_label(row: dict) -> str:
    """A human-readable identity for an unnormalizable listing row.

    Daftra nests the Invoice object inside the row (or the row *is* the invoice
    in the flat shape), so the number/id is pulled from whichever shape the row
    actually carries. Falls back to a generic label so the warning is never
    empty.
    """
    node = row.get("Invoice") if isinstance(row.get("Invoice"), dict) else row
    ident = node.get("no") if isinstance(node, dict) else None
    if not ident:
        ident = node.get("id") if isinstance(node, dict) else None
    return f"invoice {ident}" if ident else "an invoice"


class DaftraInvoiceMapper:
    def __init__(self, country_code: str = "20") -> None:
        # The phone is normalized here with the *configured* country code rather
        # than a hardcoded 20: the poller re-normalizes with the same setting, so
        # two passes that disagree (a Saudi local number read as Egyptian, a value
        # dropped as "unusable" and fetched again) silently corrupted the number.
        # Kept as "20" by default so the historic Egypt-only behaviour is the
        # default of a bare DaftraInvoiceMapper().
        self._country_code = country_code

    def to_invoice(self, raw: dict) -> Invoice:
        node = self._data_node(raw)
        if not isinstance(node, dict):
            raise ValueError(f"Unexpected Daftra payload shape: {type(raw).__name__}")
        invoice = node.get("Invoice") if isinstance(node.get("Invoice"), dict) else node
        if "Invoice" in node and not isinstance(node.get("Invoice"), dict):
            raise ValueError(
                f"Malformed Daftra response: 'Invoice' is not an object: {list(node)[:6]}"
            )
        if not invoice.get("id") and not invoice.get("no"):
            raise ValueError(f"Malformed Daftra response: missing invoice data in {list(node)[:6]}")
        client_raw = self._embedded(invoice, node, "Client")
        client = client_raw if isinstance(client_raw, dict) else {}
        items_raw = self._embedded(invoice, node, "InvoiceItem") or []
        if isinstance(items_raw, dict):
            items_raw = [items_raw]
        if not isinstance(items_raw, list):
            items_raw = []
        items = tuple(self._to_item(item) for item in items_raw if isinstance(item, dict))

        total = self._money(self._first(invoice, TOTAL_KEYS))
        paid = self._money(self._first(invoice, PAID_KEYS))
        balance = self._first(invoice, BALANCE_KEYS)
        if balance is None:
            balance = total - paid

        return Invoice(
            id=str(invoice.get("id", "")),
            number=self._number(invoice),
            customer_name=self._customer_name(invoice, client),
            customer_phone=self._phone(self._customer_phone(invoice, client)),
            status=self._status(invoice),
            currency=str(self._first(invoice, CURRENCY_KEYS) or ""),
            subtotal=self._money(self._first(invoice, SUBTOTAL_KEYS)),
            total=total,
            total_paid=paid,
            balance_due=self._money(balance),
            issue_date=self._date(self._first(invoice, ISSUE_KEYS)),
            items=items,
            public_url=self._first(invoice, PUBLIC_URL_KEYS),
            pdf_url=self._first(invoice, PDF_URL_KEYS),
        )

    def to_invoices(self, raw: dict) -> list[Invoice]:
        data = raw.get("data") if isinstance(raw, dict) else None
        rows = data if isinstance(data, list) else []
        invoices: list[Invoice] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                invoices.append(self.to_invoice(row))
            except ValueError as exc:
                # One invoice that cannot be normalized (e.g. a genuinely
                # unparseable amount) must not take the whole tenant's cycle
                # down: skip it so the rest of the listing flows, and warn with
                # the invoice's own identity so the operator can find it. It is
                # never marked seen, so it is re-attempted on the next cycle and
                # stays visible until the source data is fixed.
                log.warning(
                    "skipping %s, which could not be normalized (%s); it will "
                    "be re-attempted on the next cycle",
                    _row_label(row), exc,
                )
        return invoices

    def _data_node(self, raw: dict) -> dict:
        if not isinstance(raw, dict):
            return raw
        data = raw.get("data")
        return data if isinstance(data, dict) else raw

    @staticmethod
    def _embedded(invoice: dict, node: dict, key: str):
        """Daftra nests Client/InvoiceItem inside the Invoice object; older
        payloads and the offline stub place them beside it. Prefer the nested
        value whenever the key is present on the invoice itself."""
        if isinstance(invoice, dict) and key in invoice:
            return invoice[key]
        return node.get(key)

    def _to_item(self, item: dict) -> InvoiceItem:
        return InvoiceItem(
            name=str(self._first(item, ("item", "name", "description"), default="Item")),
            quantity=self._money(self._first(item, ("quantity", "qty"), default=1)),
            unit_price=self._money(self._first(item, ("unit_price", "price"))),
            total=self._money(self._first(item, ("subtotal", "total", "line_total"))),
        )

    def _number(self, invoice: dict) -> str:
        number = self._first(invoice, NUMBER_KEYS)
        if number is not None:
            return str(number)
        return str(invoice.get("id") or "")

    def _customer_name(self, invoice: dict, client: dict) -> str:
        for key in ("business_name", "client_business_name"):
            value = self._first(client, (key,)) or self._first(invoice, (key,))
            if value:
                return str(value)
        first = self._first(invoice, ("client_first_name",)) or self._first(client, ("first_name",))
        last = self._first(invoice, ("client_last_name",)) or self._first(client, ("last_name",))
        joined = " ".join(str(part) for part in (first, last) if part).strip()
        return joined or "Customer"

    def _customer_phone(self, invoice: dict, client: dict) -> str | None:
        for key in ("phone2", "phone1", "mobile", "phone"):
            value = self._first(client, (key,)) or self._first(invoice, (f"client_{key}",))
            if value:
                return str(value)
        return None

    def _status(self, invoice: dict) -> str:
        draft = self._first(invoice, ("draft",), default=False)
        if draft in (1, "1", True, "true", "True"):
            return "Draft"
        payment_status = self._first(invoice, ("payment_status", "status"))
        if isinstance(payment_status, bool):
            payment_status = int(payment_status)
        if isinstance(payment_status, int) and payment_status in STATUS_LABELS:
            return STATUS_LABELS[payment_status]
        if isinstance(payment_status, str) and payment_status.isdigit():
            return STATUS_LABELS.get(int(payment_status), "Unknown")
        text = str(payment_status or "").replace("_", " ").title()
        return text or "Unknown"

    def _first(self, node: dict, keys: tuple[str, ...], default=None):
        for key in keys:
            value = self._pick(node, key)
            if value is None or value == "" or value == []:
                continue
            return value
        return default

    def _pick(self, node: dict, key: str):
        current = node
        for part in key.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    @staticmethod
    def _money(value) -> Decimal:
        """Parse a money value into a Decimal, failing loudly on a *present but
        unparseable* value instead of silently sending a wrong amount.

        Daftra returns JSON numbers today, but a formatted string from the API or
        an intermediary (``"EGP 1,250.00"``, ``"12,345,678"``, ``"1 250,00"``)
        must parse to the right amount — a silent ``0.00`` on a customer-facing
        document is the worst failure this mapper can have. The last of ``,``/``.``
        wins as the decimal separator when both are present; a lone comma with 1-2
        digits after it is a decimal point (``1,99``), otherwise a thousands
        separator; a lone dot is a decimal point. Thousand groups are validated so
        ``1.234.567`` parses but ``12..34`` raises. Arabic/Persian localizations
        are folded to the ASCII forms first (``٫``→``.``, ``٬``→``,``, ``−``→``-``,
        and Arabic-Indic digits to 0-9), so a value a proxy localized for the
        customer's locale cannot silently change magnitude.
        """
        if isinstance(value, bool):
            return Decimal("1" if value else "0")
        if value is None or value == "":
            return Decimal("0")
        if isinstance(value, (int, float, Decimal)):
            result = Decimal(str(value))
            return result if result.is_finite() else _raise_money(value)
        if isinstance(value, (list, tuple, dict, set)):
            _raise_money(value)
        text = str(value).translate(_MONEY_TRANSLATIONS)
        cleaned = re.sub(r"[^\d.,\s-]", "", text).strip().replace(" ", "")
        if not cleaned:
            # Whitespace-only after the strip means "no amount was present", the
            # same as an empty string; anything else that reduces to nothing
            # (``"n/a"``) is a present-but-unparseable value and fails loudly.
            if not str(value).strip():
                return Decimal("0")
            _raise_money(value)
        if cleaned in ("-", ".", ","):
            _raise_money(value)
        if "," in cleaned and "." in cleaned:
            dec, th = (",", ".") if cleaned.rindex(",") > cleaned.rindex(".") else (".", ",")
            whole, _, frac = cleaned.rpartition(dec)
            if not (frac.isdigit() and len(frac) <= 2) or not DaftraInvoiceMapper._valid_thousands(whole, th):
                _raise_money(value)
            cleaned = whole.replace(th, "") + "." + frac
        elif "," in cleaned:
            whole, _, frac = cleaned.partition(",")
            if frac.isdigit() and len(frac) <= 2:
                cleaned = whole + "." + frac
            else:
                if not DaftraInvoiceMapper._valid_thousands(cleaned, ","):
                    _raise_money(value)
                cleaned = cleaned.replace(",", "")
        elif cleaned.count(".") > 1:
            if not DaftraInvoiceMapper._valid_thousands(cleaned, "."):
                _raise_money(value)
            cleaned = cleaned.replace(".", "")
        try:
            result = Decimal(cleaned)
        except (InvalidOperation, ValueError) as exc:
            _raise_money(value, exc)
        return result if result.is_finite() else _raise_money(value)

    @staticmethod
    def _valid_thousands(whole: str, sep: str) -> bool:
        """Whether *whole* is digits grouped by *sep* into 1-3 digit groups.

        The first group may carry a leading minus (``-1,250``). An empty group
        (``12..34``) or an over-long one (``1234,567``) is malformed, so the
        caller raises instead of guessing.
        """
        for index, group in enumerate(whole.split(sep)):
            digits = group[1:] if index == 0 and group.startswith("-") else group
            if not (digits.isdigit() and 1 <= len(digits) <= 3):
                return False
        return True

    @staticmethod
    def _date(value) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if value is None or value == "":
            return None
        text = str(value).strip()[:10]
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    def _phone(self, value) -> str | None:
        if value is None or value == "":
            return None
        try:
            return normalize_phone(value, self._country_code)
        except ValueError:
            return None
