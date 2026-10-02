"""Pure rendering and serialization for the HTML send report.

Deliberately free of I/O, like :mod:`sender.domain.templates`: given an outcome
it returns HTML text, and given a dict it returns an
:class:`~sender.domain.models.SendOutcome`. The filesystem lives in
:mod:`sender.infrastructure.reporting`.

Every value interpolated into HTML goes through :func:`html.escape` with
``quote=True``. The input is not trustworthy: customer names come from the ERP
and error strings are Meta's own text, so an unescaped ``<`` or ``&`` would
both corrupt the page and let stored content inject markup into a page served
from the company's own domain.
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    ABANDONED,
    FAILED,
    KIND_CUSTOMER,
    KIND_INVOICE,
    KIND_PAYMENT,
    SENT,
    SendOutcome,
)

__all__ = [
    "obfuscate_phone",
    "outcome_from_json",
    "outcome_to_json",
    "render_date_page",
    "render_index",
]


def obfuscate_phone(phone: str | None) -> str:
    """Mask the middle of a phone number, keeping country code and last digits.

    The reports sit on a public web root, and a full recipient number is
    personal data that lets a reader text the customer. Keeping the country
    code and the last four digits keeps the column useful for cross-checking
    against an invoice without handing out a dialable number.
    """
    if not phone:
        return ""
    text = str(phone).strip()
    # A number too short to mask with a prefix and a suffix must be hidden
    # completely: keeping 4 of 5 characters would leave a dialable number
    # behind, which is the one thing masking exists to prevent. Needs >= 7
    # characters for 2 + at least 1 masked + 4 trailing digits to line up.
    if len(text) <= 6:
        return "•" * len(text)
    return f"{text[:2]}{'•' * (len(text) - 6)}{text[-4:]}"


def _format_money(total: Decimal, currency: str) -> str:
    try:
        amount = f"{total:,.2f}"
    except (ValueError, TypeError, InvalidOperation):
        amount = str(total)
    return f"{amount} {currency}".strip()


def _format_timestamp(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "-"


def _format_date(value: date | None) -> str:
    return value.isoformat() if value else ""


def _local_day(epoch: float) -> date:
    """The host-local date an epoch falls on.

    Local time is deliberate and matches how the poll summary prints timestamps:
    the operator reads "what happened today" against the host's clock, so the
    day boundary is the server's, not UTC's.
    """
    try:
        return datetime.fromtimestamp(epoch).date()
    except (OverflowError, OSError, ValueError):
        return date.today()


def outcome_to_json(outcome: SendOutcome) -> dict[str, Any]:
    """Serialize to JSON-safe primitives (``Decimal``/``date`` as strings)."""
    return {
        "app": outcome.app,
        "invoice_id": outcome.invoice_id,
        "invoice_number": outcome.invoice_number,
        "customer_name": outcome.customer_name,
        "customer_phone": outcome.customer_phone,
        "currency": outcome.currency,
        "total": str(outcome.total),
        "issue_date": _format_date(outcome.issue_date),
        "status": outcome.status,
        "attempted_at": outcome.attempted_at,
        "error": outcome.error,
        "fallback": outcome.fallback,
        "wamid": outcome.wamid,
        "kind": outcome.kind,
    }


def outcome_from_json(data: Mapping[str, Any]) -> SendOutcome:
    """Rebuild an outcome from :func:`outcome_to_json` output.

    Tolerant on purpose: a single bad field in one historical line should cost
    that row, not the whole report.
    """
    raw_total = data.get("total") or "0"
    try:
        total = Decimal(str(raw_total))
    except (InvalidOperation, TypeError, ValueError):
        total = Decimal("0")
    raw_issue = data.get("issue_date") or ""
    try:
        issue_date = date.fromisoformat(raw_issue) if raw_issue else None
    except (TypeError, ValueError):
        issue_date = None
    # A row written before the payments pipeline existed has no ``kind``. Defaulting
    # it to an invoice is what keeps the whole existing history renderable — and
    # correctly labelled — instead of showing up as a nameless third thing.
    kind = str(data.get("kind") or KIND_INVOICE)
    try:
        attempted_at = float(data.get("attempted_at") or 0.0)
    except (TypeError, ValueError):
        attempted_at = 0.0
    return SendOutcome(
        app=str(data.get("app") or ""),
        invoice_id=str(data.get("invoice_id") or ""),
        invoice_number=str(data.get("invoice_number") or ""),
        customer_name=str(data.get("customer_name") or ""),
        customer_phone=data.get("customer_phone"),
        currency=str(data.get("currency") or ""),
        total=total,
        issue_date=issue_date,
        status=str(data.get("status") or SENT),
        attempted_at=attempted_at,
        error=data.get("error"),
        fallback=bool(data.get("fallback")),
        wamid=data.get("wamid"),
        kind=kind,
    )


_STATUS_META = {
    SENT: ("Sent", "ok"),
    FAILED: ("Failed", "bad"),
    ABANDONED: ("Abandoned", "warn"),
}

#: Both pipelines record into this one audit trail, so a page can hold invoices
#: and payments side by side. The column headers therefore stay neutral
#: ("Number", "Date", "Amount") and each row says which kind it is, rather than a
#: page header claiming everything is an invoice.
_KIND_TITLES = {KIND_INVOICE: "Invoice", KIND_PAYMENT: "Payment", KIND_CUSTOMER: "Customer"}


def _kind_badge(kind: str) -> str:
    label = _KIND_TITLES.get(kind, kind or "Unknown")
    return f'<span class="badge kind">{html.escape(label)}</span>'


def _status_badge(status: str) -> str:
    label, kind = _STATUS_META.get(status, (status or "Unknown", "warn"))
    return f'<span class="badge {kind}">{html.escape(label)}</span>'


def _cell(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _style() -> str:
    return (
        "body{font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
        "margin:0;padding:2rem;background:#f6f7f9;color:#1c2024}"
        ".wrap{max-width:1100px;margin:0 auto}"
        "h1{font-size:1.5rem;margin:0 0 .25rem}h2{font-size:1.1rem;margin:2rem 0 .5rem}"
        ".sub{color:#656d76;margin:0 0 1.5rem;font-size:.9rem}"
        "table{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;"
        "overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08)}"
        "th,td{padding:.55rem .7rem;text-align:left;border-bottom:1px solid #e5e7eb;"
        "vertical-align:top;font-size:.9rem}"
        "th{background:#eef1f4;font-weight:600;font-size:.8rem;text-transform:uppercase;"
        "letter-spacing:.03em;color:#4a5157}"
        "tr:last-child td{border-bottom:none}"
        ".num{text-align:right;font-variant-numeric:tabular-nums}"
        ".badge{display:inline-block;padding:.1rem .5rem;border-radius:999px;"
        "font-size:.75rem;font-weight:600}"
        ".badge.ok{background:#dcfce7;color:#166534}.badge.bad{background:#fee2e2;color:#991b1b}"
        ".badge.warn{background:#fef3c7;color:#92400e}.badge.kind{background:#e0e7ff;color:#3730a3}"
        ".none{background:#fff;padding:1.5rem;border-radius:8px;color:#656d76;"
        "box-shadow:0 1px 3px rgba(0,0,0,.08)}"
        "a{color:#0b62d6}.err{color:#991b1b;font-size:.82rem;max-width:32ch;"
        "display:inline-block;word-break:break-word}"
        "footer{margin-top:2rem;color:#8b949e;font-size:.8rem}"
    )


def render_date_page(
    day: date,
    entries: Sequence[SendOutcome],
    *,
    obfuscate: bool = True,
) -> str:
    """Render one day's sends as a standalone, self-contained HTML page."""
    ordered = sorted(entries, key=lambda item: item.attempted_at)
    sent = sum(1 for item in ordered if item.ok)
    failed = len(ordered) - sent
    kinds = {item.kind for item in ordered}
    # One day can hold any of the three kinds, so the title names what is actually
    # on the page: "invoices" alone would mislabel every payment or customer row
    # on a mixed day.
    if len(kinds) == 1:
        only = next(iter(kinds))
        title = f"{_KIND_TITLES.get(only, only or 'Message')} sends — {day.isoformat()}"
    else:
        title = f"Message sends — {day.isoformat()}"

    if ordered:
        rows = []
        for item in ordered:
            phone = obfuscate_phone(item.customer_phone) if obfuscate else (item.customer_phone or "")
            note = _cell(item.error) if item.error else ""
            if item.fallback:
                fallback_note = (
                    '<span class="badge warn">free-form fallback</span>'
                    if not note
                    else '<span class="badge warn">free-form fallback</span><br>' + note
                )
                note = fallback_note
            rows.append(
                "<tr>"
                f"<td>{_cell(_format_timestamp(item.attempted_at))}</td>"
                f"<td>{_status_badge(item.status)}</td>"
                f"<td>{_cell(item.customer_name)}</td>"
                f"<td>{_cell(phone)}</td>"
                f"<td>{_cell(item.invoice_number)}</td>"
                f"<td>{_cell(item.invoice_id)}</td>"
                f"<td>{_cell(_format_date(item.issue_date))}</td>"
                f'<td class="num">{_cell(_format_money(item.total, item.currency))}</td>'
                f"<td>{_kind_badge(item.kind)}</td>"
                f"<td>{_cell(item.app)}</td>"
                f'<td class="err">{note}</td>'
                "</tr>"
            )
        body = (
            "<table><thead><tr>"
            "<th>Time</th><th>Status</th><th>Customer</th><th>Phone</th>"
            "<th>Number</th><th>ID</th><th>Date</th><th>Amount</th>"
            "<th>Kind</th><th>App</th><th>Notes</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        )
    else:
        body = '<p class="none">No messages were sent on this date.</p>'

    summary = (
        f'<p class="sub">{len(ordered)} send attempt(s) &middot; '
        f"{sent} sent &middot; {failed} failed or abandoned</p>"
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{html.escape(title)}</title>"
        f"<style>{_style()}</style></head><body><div class='wrap'>"
        f'<h1>{html.escape(title)}</h1>'
        f'<p class="sub"><a href="index.html">&larr; All dates</a></p>'
        f"{summary}{body}"
        f"<footer>Generated by the Aizen sender.</footer>"
        "</div></body></html>\n"
    )


_SEARCH_CSS = (
    ".bar{display:flex;gap:.5rem;flex-wrap:wrap;margin:0 0 1rem}"
    ".bar input{flex:1 1 16rem;padding:.5rem .6rem;border:1px solid #cdd3d9;"
    "border-radius:6px;font:inherit;background:#fff}"
    ".bar select{padding:.5rem .6rem;border:1px solid #cdd3d9;border-radius:6px;"
    "font:inherit;background:#fff}"
    "#hits{overflow-x:auto}"
    ".mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;"
    "font-size:.85rem;white-space:nowrap}"
)

_SEARCH_FORM = (
    '<div class="bar">'
    '<input id="q" type="search" placeholder="Search phone, name, invoice, app..." '
    'autocomplete="off" autofocus>'
    '<select id="st"><option value="">All results</option>'
    '<option value="sent">Sent only</option>'
    '<option value="failed">Failed / abandoned only</option></select>'
    "</div>"
)

_SEARCH_JS = """<script>
const DATA = __DATA__;
const q = document.getElementById("q");
const st = document.getElementById("st");
const hits = document.getElementById("hits");
const esc = (v) => (v === null || v === undefined) ? "" : String(v)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
  .replace(/"/g,"&quot;");

function badge(status) {
  const ok = status === "sent";
  const cls = ok ? "ok" : (status === "failed" ? "bad" : "warn");
  const label = ok ? "Sent" : (status === "failed" ? "Failed" : "Abandoned");
  return '<span class="badge ' + cls + '">' + label + "</span>";
}

function draw() {
  const needle = q.value.trim().toLowerCase();
  const want = st.value;
  const rows = DATA.filter((r) => {
    if (want === "sent" && r.s !== "sent") return false;
    if (want === "failed" && r.s === "sent") return false;
    if (!needle) return true;
    // Digits are matched loosely so a national-format tail finds the stored
    // international number despite a leading country code.
    const digits = needle.replace(/\\D/g, "");
    const hay = (r.p + " " + r.n + " " + r.i + " " + r.g + " " + r.a).toLowerCase();
    if (hay.includes(needle)) return true;
    return digits.length >= 4 && (r.p || "").replace(/\\D/g, "").includes(digits);
  });
  if (!rows.length) {
    hits.innerHTML = '<p class="none">Nothing matches that filter.</p>';
    return;
  }
  const body = rows.map((r) => "<tr>"
    + '<td><a href="' + esc(r.u) + '">' + esc(r.d) + "</a></td>"
    + "<td>" + esc(r.t) + "</td>"
    + "<td>" + badge(r.s) + "</td>"
    + "<td>" + esc(r.n) + "</td>"
    + '<td class="mono">' + esc(r.p) + "</td>"
    + "<td>" + esc(r.i) + "</td>"
    + "<td>" + esc(r.a) + "</td>"
    + '<td class="err">' + esc(r.x) + "</td>"
    + "</tr>").join("");
  hits.innerHTML = rows.length + " row(s)<table><thead><tr>"
    + "<th>Date</th><th>Time</th><th>Status</th><th>Customer</th><th>Phone</th>"
    + "<th>Invoice #</th><th>App</th><th>Notes</th>"
    + "</tr></thead><tbody>" + body + "</tbody></table>";
}
q.addEventListener("input", draw);
st.addEventListener("change", draw);
draw();
</script>
"""


def _json_for_script(payload: Any) -> str:
    """Serialize ``payload`` for embedding inside a ``<script>`` element.

    ``json.dumps`` alone is not safe here: HTML parsing ends a script block at
    the first ``</script``, so a customer name from the ERP containing that
    sequence (or ``<!--``) would break out of the tag and inject markup. The
    three HTML-significant characters are escaped to their ``\\uXXXX`` JSON
    forms, which parse back to the same strings but cannot terminate the block.
    """
    return (
        json.dumps(payload, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_recipients(
    entries: Sequence[SendOutcome],
    *,
    obfuscate: bool = True,
) -> str:
    """Render a searchable, sortable list of every recipient and what they got.

    The date pages answer "what happened on the 12th". This answers the other
    half of the question an operator actually has: "what did we send this
    person, and did any of it fail". It is a single self-contained page whose
    filter is plain client-side JavaScript, because the reports are static files
    behind basic auth with no server-side code to lean on.
    """
    if not entries:
        return _page(
            "Recipients",
            '<p class="sub"><a href="index.html">&larr; All dates</a></p>',
            '<p class="none">No messages have been sent yet.</p>',
        )

    data = _json_for_script(
        [
            {
                "d": _format_date(_local_day(entry.attempted_at)),
                "t": _format_timestamp(entry.attempted_at),
                "n": entry.customer_name or "",
                "p": obfuscate_phone(entry.customer_phone)
                if obfuscate
                else (entry.customer_phone or ""),
                "i": entry.invoice_number or "",
                "g": entry.invoice_id or "",
                "a": entry.app or "",
                "s": entry.status or "",
                "x": entry.error or "",
                "u": f"{_local_day(entry.attempted_at).isoformat()}.html",
            }
            for entry in entries
        ]
    )
    return _page(
        "Recipients",
        '<p class="sub"><a href="index.html">&larr; All dates</a></p>'
        '<p class="sub">Filter by phone, name, invoice or app. '
        f"{len(entries)} send attempt(s).</p>",
        _SEARCH_FORM + '<div id="hits"></div>',
        script=_SEARCH_JS.replace("__DATA__", data),
    )


def _page(title: str, sub: str, body: str, *, script: str = "") -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{html.escape(title)}</title>"
        f"<style>{_style()}{_SEARCH_CSS}</style></head><body><div class='wrap'>"
        f"<h1>{html.escape(title)}</h1>"
        f"{sub}{body}"
        "<footer>Generated by the Aizen invoice sender.</footer>"
        "</div>"
        f"{script}"
        "</body></html>\n"
    )


def render_index(
    days: Iterable[date],
    counts: Mapping[date, tuple[int, int]] | None = None,
) -> str:
    """Render the date index, newest first.

    ``counts`` maps a date to ``(sent, failed)``; dates without an entry show a
    dash rather than a misleading zero.
    """
    counts = counts or {}
    ordered = sorted(set(days), reverse=True)
    if ordered:
        rows = []
        for day in ordered:
            tally = counts.get(day)
            detail = (
                f"{tally[0]} sent &middot; {tally[1]} failed"
                if tally
                else "&mdash;"
            )
            rows.append(
                "<tr>"
                f'<td><a href="{_cell(day.isoformat())}.html">{_cell(day.isoformat())}</a></td>'
                f'<td class="num">{detail}</td>'
                "</tr>"
            )
        body = (
            "<table><thead><tr><th>Date</th><th>Result</th></tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody></table>"
        )
    else:
        body = '<p class="none">No sends have been recorded yet.</p>'

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        "<title>Send reports</title>"
        f"<style>{_style()}</style></head><body><div class='wrap'>"
"<h1>Invoice send reports</h1>"
        '<p class="sub">WhatsApp messages sent to customers, by date. '
        '<a href="recipients.html">Browse by recipient &rarr;</a></p>'
        f"{body}"
        "<footer>Generated by the Aizen sender.</footer>"
        "</div></body></html>\n"
    )