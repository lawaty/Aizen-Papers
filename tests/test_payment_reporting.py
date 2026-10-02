"""The report's JSON round-trip and its per-page rendering.

``test_reporting.py`` covers only ``obfuscate_phone``. This file covers the parts
the payments pipeline actually exercises — that a payment row survives a
round-trip, that rows written *before* that pipeline existed still render as
invoices, and that a page holding both kinds says which is which.
"""

from datetime import date
from decimal import Decimal

from sender.domain.models import ABANDONED, KIND_INVOICE, KIND_PAYMENT, SENT, SendOutcome
from sender.domain.reporting import (
    outcome_from_json,
    outcome_to_json,
    render_date_page,
    render_index,
)


def _payment_outcome(**over) -> SendOutcome:
    values = dict(
        app="aizenpaper",
        invoice_id="116",
        invoice_number="000116",
        customer_name="Ahmed Hassan",
        attempted_at=1_700_000_000.0,
        customer_phone="201027693262",
        currency="EGP",
        total=Decimal("1500.00"),
        issue_date=date(2026, 9, 1),
        status=SENT,
        kind=KIND_PAYMENT,
    )
    values.update(over)
    return SendOutcome(**values)


def _invoice_outcome(**over) -> SendOutcome:
    values = dict(
        app="aizenpaper",
        invoice_id="2",
        invoice_number="000002",
        customer_name="Print Home",
        attempted_at=1_700_000_100.0,
        customer_phone="201027693262",
        currency="EGP",
        total=Decimal("1500.00"),
        issue_date=date(2026, 9, 2),
        status=SENT,
    )
    values.update(over)
    return SendOutcome(**values)


def test_a_payment_outcome_round_trips_through_json():
    restored = outcome_from_json(outcome_to_json(_payment_outcome()))
    assert restored.kind == KIND_PAYMENT
    assert restored.invoice_number == "000116"
    assert restored.invoice_id == "116"
    assert restored.total == Decimal("1500.00")
    assert restored.issue_date == date(2026, 9, 1)
    assert restored.customer_name == "Ahmed Hassan"


def test_a_row_written_before_the_payments_pipeline_still_reads_as_an_invoice():
    """Every JSONL file already on disk predates this feature. A missing ``kind``
    must not orphan them or render them as an unlabelled third thing."""
    legacy = {
        "app": "aizenpaper",
        "invoice_id": "2",
        "invoice_number": "000002",
        "customer_name": "Print Home",
        "customer_phone": "201027693262",
        "currency": "EGP",
        "total": "1500.00",
        "issue_date": "2026-09-02",
        "status": SENT,
        "attempted_at": 1_700_000_100.0,
        "error": None,
        "fallback": False,
        "wamid": "wamid.OLD",
    }
    restored = outcome_from_json(legacy)
    assert restored.kind == KIND_INVOICE
    assert "Invoice" in render_date_page(date(2026, 9, 2), [restored])


def test_a_page_of_payments_only_says_so():
    page = render_date_page(date(2026, 9, 1), [_payment_outcome()])
    assert "Payment sends" in page
    assert "000116" in page
    assert "1,500.00 EGP" in page


def test_a_mixed_page_names_the_kind_of_every_row():
    page = render_date_page(
        date(2026, 9, 1), [_payment_outcome(), _invoice_outcome()]
    )
    assert "Message sends" in page
    assert ">Payment<" in page and ">Invoice<" in page
    # Both documents are on the page, each under its own row.
    assert "000116" in page and "000002" in page


def test_a_page_of_invoices_keeps_its_original_title():
    page = render_date_page(date(2026, 9, 1), [_invoice_outcome()])
    assert "Invoice sends" in page


def test_an_unknown_kind_does_not_break_the_page():
    """A kind a future version invents must degrade, not crash a page an operator
    is reading."""
    page = render_date_page(
        date(2026, 9, 1), [_payment_outcome(kind="something-new")]
    )
    assert "000116" in page


def test_the_abandoned_status_still_renders_for_a_payment():
    page = render_date_page(
        date(2026, 9, 1), [_payment_outcome(status=ABANDONED, error="no phone")]
    )
    assert "Abandoned" in page
    assert "no phone" in page


def test_the_index_is_titled_neutrally():
    """One audit trail covers both pipelines, so it may no longer be called the
    invoice report."""
    page = render_index([date(2026, 9, 1)])
    assert "Send reports" in page
    assert "Invoice send reports" not in page


def test_a_broken_row_costs_only_that_row():
    """Tolerant on purpose: one bad historical line must not cost a whole day."""
    broken = {"app": "a", "invoice_id": "1", "total": "not-a-number", "attempted_at": "x"}
    restored = outcome_from_json(broken)
    assert restored.total == Decimal("0")
    assert restored.attempted_at == 0.0
    assert restored.kind == KIND_INVOICE