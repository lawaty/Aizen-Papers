from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sender.domain.models import Customer, Invoice, InvoiceItem, Payment
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
#: The free text an invoice carries *about itself* (Daftra calls it notes). Read
#: before ``description``: ``notes`` is what the documented API writes and what the
#: live account sends, while ``description`` is the spelling a few Daftra shapes use
#: for the same field, so both are accepted rather than betting on one.
NOTES_KEYS = ("notes", "description")
#: The free text a single line carries about itself, under its product name.
ITEM_DESCRIPTION_KEYS = ("description",)
#: The phone fields Daftra keeps on a client, in preference order. ``phone1``
#: and ``phone2`` are the two the ERP actually exposes; ``mobile``/``phone`` are
#: older shapes seen in the wild. Every key is read as a *source of another
#: recipient*, not as an ordered preference to stop at, so an account with two
#: different numbers filled in is reachable on both. The invoice-level
#: ``client_<key>`` spelling is the fallback for payloads that flatten the
#: client onto the invoice.
_PHONE_KEYS = ("phone2", "phone1", "mobile", "phone")

#: Money Daftra can charge that this sender deliberately does not model — see
#: :meth:`DaftraInvoiceMapper._warn_unmapped_money`. Listed so the gap is a
#: recorded decision rather than an oversight, and so the tripwire has a key list
#: to watch. Only truthiness is checked; these are never parsed into the model.
UNMAPPED_MONEY_KEYS = ("summary_tax1", "summary_tax2", "summary_tax3", "summary_discount", "deposit")
UNMAPPED_ITEM_MONEY_KEYS = ("tax1", "tax2", "discount")

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


def _is_money_present(value) -> bool:
    """Whether *value* is an amount that is actually non-zero.

    Truthiness is not enough, and getting this wrong cries wolf on every single
    invoice: Daftra sends ``deposit: "0"`` (a string zero, which is truthy) and
    ``summary_discount: 0`` on invoices that carry neither, so a bare
    ``if value:`` test reports unmapped money on essentially the whole ledger and
    trains the operator to ignore the warning. Measuring through :meth:`_money`
    means every spelling of zero (``0``, ``"0"``, ``"0.00"``, ``"EGP 0.00"``)
    reads as zero.

    A value that is present but unparseable counts as present: it is exactly the
    case a human should look at, and this must never raise, because a raise here
    would abandon a real invoice over a field that never reaches the document.
    """
    if value is None or value == "":
        return False
    try:
        return DaftraInvoiceMapper._money(value) != 0
    except ValueError:
        return True


def _row_label(row: dict) -> str:
    """A human-readable identity for an unnormalizable listing row.

    Daftra nests the record inside the row (or the row *is* the record in the
    flat shape), and the wrapper key says which kind it is — ``Invoice`` or
    ``InvoicePayment``. The kind is named in the label so an operator reading a
    warning knows which pipeline dropped the row and which state file it will be
    re-attempted against. Falls back to a generic label so the warning is never
    empty.
    """
    node = None
    kind = None
    for key, kind in (("Invoice", "invoice"), ("InvoicePayment", "payment"), ("Client", "customer")):
        if isinstance(row.get(key), dict):
            node = row[key]
            break
    if node is None:
        node = row
    # Payments are identified by their reference code, which is what an operator
    # sees on the receipt; everything else falls back to a number then the id.
    for field in ("no", "code", "id"):
        ident = node.get(field) if isinstance(node, dict) else None
        if ident:
            return f"{kind or 'record'} {ident}"
    if not kind:
        return "an unidentifiable record"
    # Both kinds start with a vowel, but this must not depend on that staying true.
    article = "an" if kind[0] in "aeiou" else "a"
    return f"{article} {kind}"


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
        self._warn_unmapped_money(invoice, items_raw)

        total = self._money(self._first(invoice, TOTAL_KEYS))
        paid = self._money(self._first(invoice, PAID_KEYS))
        balance = self._first(invoice, BALANCE_KEYS)
        if balance is None:
            balance = total - paid

        return Invoice(
            id=str(invoice.get("id", "")),
            number=self._number(invoice),
            customer_name=self._customer_name(invoice, client),
            customer_phones=self._customer_phones(invoice, client),
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
            description=self._text(self._first(invoice, NOTES_KEYS)),
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
        # The defaults below are all *plausible*, which is exactly why an absent
        # field has to be reported: a row that says nothing about its quantity
        # would otherwise reach the customer as a confident "1", and one that says
        # nothing about its price as a confident "0.00". A name-ish field gets a
        # visible placeholder and asserts nothing false, so it is not reported.
        #
        # Absent warns rather than raises on purpose: Daftra uses ``null`` for
        # "not applicable" money elsewhere (``tax1``/``tax2`` are null on real
        # items), and a raise here is a bare ``ValueError``, which the poller
        # classifies *permanent* — retiring a real invoice on the first sight of a
        # shape it has never seen. Present-but-garbage still raises, via
        # :meth:`_money`; that asymmetry is the whole decision.
        name = self._first(item, ("item", "name", "description"), default="Item")
        unit_price = self._first(item, ("unit_price", "price"))
        total = self._first(item, ("subtotal", "total", "line_total"))
        absent = [
            field
            for field, value in (
                ("quantity", self._first(item, ("quantity", "qty"))),
                ("unit_price", unit_price),
                ("total", total),
            )
            if value is None
        ]
        if absent:
            log.warning(
                "invoice item %r carries no %s; the PDF will show the defaults "
                "rather than fail the invoice",
                str(name),
                "/".join(absent),
            )
        return InvoiceItem(
            name=str(name),
            quantity=self._money(self._first(item, ("quantity", "qty"), default=1)),
            unit_price=self._money(unit_price),
            total=self._money(total),
            description=self._text(self._first(item, ITEM_DESCRIPTION_KEYS)),
        )

    def _warn_unmapped_money(self, invoice: dict, items_raw: object) -> None:
        """Report money Daftra charged that the invoice model does not carry.

        Tax, discount and deposit are deliberately not mapped: ``Invoice`` has no
        field for them and the PDF totals block has no row, so mapping them now
        would be a feature built against a schema no account currently sends. What
        is *not* acceptable is that same state being silent — money in the raw
        payload, absent from the customer's document, nobody told. This is the
        mapper's tripwire, and it fires the day a tenant enables VAT.

        The values are only *tested for truthiness*, never parsed: a ``_money()``
        call here could raise on a shape we chose not to model, and that would
        abandon a real invoice over a field that never reaches the document.
        """
        found = [key for key in UNMAPPED_MONEY_KEYS if _is_money_present(self._first(invoice, (key,)))]
        if isinstance(items_raw, list):
            for item in items_raw:
                if not isinstance(item, dict):
                    continue
                found += [
                    f"InvoiceItem.{key}"
                    for key in UNMAPPED_ITEM_MONEY_KEYS
                    if _is_money_present(self._first(item, (key,)))
                ]
        if found:
            log.warning(
                "invoice %s: Daftra reports money this sender does not model (%s); "
                "it will not appear on the PDF",
                self._number(invoice),
                ", ".join(sorted(set(found))),
            )

    def _number(self, invoice: dict) -> str:
        number = self._first(invoice, NUMBER_KEYS)
        if number is not None:
            return str(number)
        return str(invoice.get("id") or "")

    @staticmethod
    def _text(value) -> str:
        """A free-text field, as the plain string the PDF will draw.

        Deliberately has no default, unlike every other reader in this class: a
        name-ish field can fall back to a neighbour or to a visible placeholder
        because the document still needs *a* name there, but a note that Daftra
        did not send has no honest substitute. ``""`` is the whole answer — the PDF
        draws nothing for it, which is what "this invoice has no notes" looks like
        on paper.
        """
        return "" if value is None else str(value)

    def _customer_name(self, invoice: dict, client: dict) -> str:
        for key in ("business_name", "client_business_name"):
            value = self._first(client, (key,)) or self._first(invoice, (key,))
            if value:
                return str(value)
        first = self._first(invoice, ("client_first_name",)) or self._first(client, ("first_name",))
        last = self._first(invoice, ("client_last_name",)) or self._first(client, ("last_name",))
        joined = " ".join(str(part) for part in (first, last) if part).strip()
        return joined or "Customer"

    def _customer_phones(self, invoice: dict, client: dict) -> tuple[str, ...]:
        """Every distinct usable phone on the client, most-preferred first.

        Daftra keeps two phone fields on a client and either may be filled, so
        this collects **all** of them instead of picking one. ``phone2`` stays
        first because that is the order the single-recipient mapper always used,
        which keeps the primary recipient of an account whose two fields held the
        same number unchanged.

        Deduplication is on the *normalized* value, the only comparison that can
        tell two entries for the same subscriber apart: an account whose
        ``phone1`` is ``01027693262`` and whose ``phone2`` is ``+201027693262`` is
        one person, and sending them the same invoice twice is worse than sending
        it once. Two genuinely different numbers are both returned.

        Empty fields are skipped silently — "the second phone is blank" is the
        ordinary case, not a problem. A field that is *filled but unusable* warns
        and names itself: with two fields to read, a silently dropped number is
        indistinguishable from an invoice that never had a second phone, which is
        exactly the ambiguity this method exists to remove.
        """
        phones: list[str] = []
        seen: set[str] = set()
        for key in _PHONE_KEYS:
            raw = self._first(client, (key,))
            if raw is None or raw == "":
                raw = self._first(invoice, (f"client_{key}",))
            if raw is None or raw == "":
                continue
            try:
                phone = normalize_phone(raw, self._country_code)
            except ValueError:
                log.warning(
                    "dropping the unusable phone %r in %r; it cannot be normalized "
                    "and Meta would reject the send per recipient",
                    raw, key,
                )
                continue
            if phone in seen:
                continue
            seen.add(phone)
            phones.append(phone)
        return tuple(phones)

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
        customer's locale cannot silently change magnitude. Exponent notation is
        rejected outright rather than sanitized, for the same reason.
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
        if re.search(r"\d\s*[eE]\s*[+-]?\d", text):
            # Exponent form must be rejected *before* the sanitizer, which strips
            # every non-digit and would turn "1e3" into "13" and "1e999" into
            # "1999" — a silent change of magnitude, the one failure this function
            # exists to prevent. Letters in general cannot be rejected: the
            # currency words in "EGP 1,250.00" must keep stripping, so the test is
            # for the digit-exponent-digit *form*, which those never match.
            _raise_money(value)
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
        # Present but unparseable is not the same as absent, and the difference is
        # invisible downstream: both land as ``None``, and the PDF renders that as
        # "N/A". Say so, or nobody learns the date was dropped.
        log.warning("unparseable invoice date %r; the PDF will show it as N/A", value)
        return None


class DaftraPaymentMapper(DaftraInvoiceMapper):
    """Translate ``/invoice_payments`` rows into :class:`Payment`.

    A subclass rather than a sibling so it reuses the parts of the invoice
    mapping that are genuinely the same rules and not invoice-specific: parsing a
    money string that may arrive localized or with a tax split, parsing Daftra's
    several date spellings, picking the client name out of an ``Invoice`` node
    with ``Client`` nested inside it, and normalizing a phone with the
    deployment's country code. Duplicating any of those would be a second place
    to get a customer's amount or number wrong.
    """

    def to_payment(self, raw: dict, invoice_raw: dict | None = None) -> Payment:
        """Normalize one payment.

        *invoice_raw* is the **linked invoice's** raw payload, when the caller has
        one. It is not decoration: a payment record carries no payer — ``client_id``
        is usually null and the contact fields are empty — so the invoice it settles
        is the only place the customer exists. Without it the payment comes back
        with no name and no phone and the poller skips it, which is the correct
        outcome for a payment nobody can be reached about.
        """
        node = self._payment_node(raw)
        if not isinstance(node, dict):
            raise ValueError(f"Unexpected Daftra payment payload shape: {type(raw).__name__}")
        if not node.get("id"):
            raise ValueError(f"Malformed Daftra response: missing payment data in {list(node)[:6]}")
        invoice = self._invoice_node(invoice_raw)
        client = invoice.get("Client") if isinstance(invoice.get("Client"), dict) else {}
        return Payment(
            id=str(node.get("id")),
            # ``code`` is the receipt reference the customer is quoted — the
            # "operation number" the template prints. The id is the fallback for
            # an account whose payments carry no code, so a payment is never sent
            # with an empty identifier.
            number=str(self._first(node, ("code",)) or node.get("id")),
            customer_name=self._customer_name(invoice, client),
            customer_phones=self._customer_phones(invoice, client),
            status=str(self._first(node, ("status",), default="Unknown") or "Unknown"),
            currency=str(self._first(node, CURRENCY_KEYS) or ""),
            amount=self._money(self._first(node, ("amount",))),
            payment_date=self._date(self._first(node, ("date", "created"))),
            invoice_id=self._invoice_id(node),
            payment_method=str(self._first(node, ("payment_method",), default="") or ""),
        )

    def to_payments(self, raw: dict) -> list[Payment]:
        data = raw.get("data") if isinstance(raw, dict) else None
        rows = data if isinstance(data, list) else []
        payments: list[Payment] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                payments.append(self.to_payment(row))
            except ValueError as exc:
                # One payment that cannot be normalized must not take the whole
                # tenant's cycle down: skip it so the rest of the listing flows,
                # and warn with the payment's own identity so the operator can find
                # it. It is never marked seen, so it is re-attempted on the next
                # cycle and stays visible until the source data is fixed.
                log.warning(
                    "skipping %s, which could not be normalized (%s); it will "
                    "be re-attempted on the next cycle",
                    _row_label(row), exc,
                )
        return payments

    @staticmethod
    def _payment_node(raw: dict) -> dict:
        """Unwrap the ``InvoicePayment`` node from a detail or listing payload.

        Accepts both shapes Daftra uses: ``{"data": {"InvoicePayment": {...}}}``
        for a single record and ``{"data": [{"InvoicePayment": {...}}]}`` for a
        page. A payload that is already the flat record is passed through, which
        is what the offline stub and hand-written test rows use.
        """
        if not isinstance(raw, dict):
            return raw
        data = raw.get("data")
        if isinstance(data, dict):
            candidate = data.get("InvoicePayment")
            return candidate if isinstance(candidate, dict) else data
        row = raw.get("InvoicePayment")
        return row if isinstance(row, dict) else raw

    @staticmethod
    def _invoice_node(raw: dict | None) -> dict:
        """Unwrap the ``Invoice`` node from a linked invoice's payload, or ``{}``.

        The same two shapes as :meth:`_payment_node`, and for the same reason: the
        two-hop fetch in the client may have been answered with either. An absent
        or unreadable invoice yields ``{}``, which makes ``_customer_name`` fall
        back to its generic "Customer" and the phone ``None`` — i.e. a payment with
        no reachable customer, which the poller skips.
        """
        if not isinstance(raw, dict):
            return {}
        data = raw.get("data")
        if isinstance(data, dict):
            candidate = data.get("Invoice")
            return candidate if isinstance(candidate, dict) else data
        row = raw.get("Invoice")
        return row if isinstance(row, dict) else raw

    @staticmethod
    def _invoice_id(payment_node: dict) -> str | None:
        """The linked invoice id, or ``None`` when the payment has no invoice.

        Payments that do not settle an invoice exist (``client_credit``,
        opening-balance rows); Daftra excludes them from this endpoint by default,
        but a null here must stay a real ``None`` rather than the string ``"None"``,
        because the client uses it to decide whether the second hop is worth
        making at all.
        """
        value = payment_node.get("invoice_id")
        if value is None or value == "":
            return None
        return str(value)


class DaftraCustomerMapper(DaftraInvoiceMapper):
    """Translate ``/clients.json`` rows into :class:`Customer`.

    A subclass, for the same reason :class:`DaftraPaymentMapper` is one: the name
    and phone policies are *the same rules* as on the invoice path, so they are
    reused rather than reimplemented — a third copy of "prefer the business name,
    else first + last" is a third place to get a customer's name wrong.

    The reuse is exact because the client row **is** the node those rules read:
    ``_customer_name(node, node)`` looks for ``client_business_name`` on the
    "invoice" side first, misses (a client row has no such key), and lands on
    ``business_name``/``first_name``/``last_name`` on the client side. Same for
    ``_customer_phones``. Nothing about the policy differs per pipeline; only the
    shape the data arrives in does.
    """

    def to_customer(self, raw: dict) -> Customer:
        node = self._customer_node(raw)
        if not isinstance(node, dict):
            raise ValueError(f"Unexpected Daftra client payload shape: {type(raw).__name__}")
        if not node.get("id"):
            raise ValueError(f"Malformed Daftra response: missing client data in {list(node)[:6]}")
        return Customer(
            id=str(node.get("id")),
            # The client number is what an operator reads in the ERP and what the
            # poller labels a row with; the id is the fallback so a blank
            # ``client_number`` never yields an empty identifier.
            number=str(self._first(node, ("client_number", "code", "no")) or node.get("id")),
            customer_name=self._customer_name(node, node),
            customer_phones=self._customer_phones(node, node),
            created=self._date(self._first(node, ("created",))),
            email=str(self._first(node, ("email",), default="") or ""),
            # Carried, never acted on. See Customer.type for why.
            type=str(self._first(node, ("type",), default="") or ""),
            suspend=str(self._first(node, ("suspend",), default="") or ""),
            is_offline=str(self._first(node, ("is_offline",), default="") or ""),
        )

    def to_customers(self, raw: dict) -> list[Customer]:
        data = raw.get("data") if isinstance(raw, dict) else None
        rows = data if isinstance(data, list) else []
        customers: list[Customer] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                customers.append(self.to_customer(row))
            except ValueError as exc:
                # Same contract as the payment listing: one unnormalizable row
                # must not take the cycle down. It is skipped, warned about by its
                # own identity, and left unseen so the next cycle retries it.
                log.warning(
                    "skipping %s, which could not be normalized (%s); it will "
                    "be re-attempted on the next cycle",
                    _row_label(row), exc,
                )
        return customers

    @staticmethod
    def _customer_node(raw: dict) -> dict:
        """Unwrap the ``Client`` node from a detail or listing payload.

        ``/clients.json`` is the third envelope Daftra uses, and it differs from
        the other two in a way that matters: each element of ``data`` is wrapped in
        its own capitalized ``Client`` key — ``{"data": [{"Client": {...}}]}`` —
        where invoices wrap as ``{"data": [{"Invoice": {...}}]}`` and a *single*
        record wraps as ``{"data": {"InvoicePayment": {...}}}``.

        All three shapes are accepted, plus a flat record, because the offline
        stub and hand-written test rows use the flat one and a refactor must not be
        able to break the mapper by changing a fixture's nesting.
        """
        if not isinstance(raw, dict):
            return raw
        data = raw.get("data")
        if isinstance(data, dict):
            candidate = data.get("Client")
            return candidate if isinstance(candidate, dict) else data
        row = raw.get("Client")
        return row if isinstance(row, dict) else raw
