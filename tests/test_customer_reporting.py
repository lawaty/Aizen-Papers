"""The report's JSON round-trip and per-page rendering, for the customers pipeline.

``test_reporting.py`` covers only ``obfuscate_phone``;
``test_payment_reporting.py`` covers the second pipeline. This file covers the
third, and — more importantly — the parts that are now genuinely shared by all
three: one audit trail, one page per day, a ``kind`` column that says which
template each row went out under, and page titles that must not mislabel a
customer row as an invoice.
"""

from datetime import date

from sender.domain.models import (
    ABANDONED,
    KIND_CUSTOMER,
    KIND_INVOICE,
    KIND_PAYMENT,
    SENT,
    SendOutcome,
)
from sender.domain.reporting import (
    outcome_from_json,
    outcome_to_json,
    render_date_page,
    render_index,
)


def _customer_outcome(**over) -> SendOutcome:
    values = dict(
        app="aizenpaper",
        # The report's columns are named for the first pipeline; a customer row
        # reuses them for the client id and client number, which is exactly the
        # kind of overloading payments already does.
        invoice_id="1",
        invoice_number="000001",
        customer_name="Print Home",
        customer_phone="201022322634",
        issue_date=date(2026, 9, 24),
        attempted_at=1_700_000_200.0,
        status=SENT,
        kind=KIND_CUSTOMER,
    )
    values.update(over)
    return SendOutcome(**values)


def _payment_outcome(**over) -> SendOutcome:
    values = dict(
        app="aizenpaper",
        invoice_id="116",
        invoice_number="000116",
        customer_name="Ahmed Hassan",
        attempted_at=1_700_000_000.0,
        customer_phone="201027693262",
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
        issue_date=date(2026, 9, 2),
        status=SENT,
    )
    values.update(over)
    return SendOutcome(**values)


def test_a_customer_outcome_round_trips_through_json():
    restored = outcome_from_json(outcome_to_json(_customer_outcome()))
    assert restored.kind == KIND_CUSTOMER
    assert restored.invoice_id == "1"
    assert restored.invoice_number == "000001"
    assert restored.customer_name == "Print Home"
    assert restored.issue_date == date(2026, 9, 24)
    assert restored.status == SENT


def test_a_customer_page_says_customer():
    """The title must not read "Invoice sends" for a customer row — that is the
    mislabelling the ``kind`` column exists to prevent."""
    page = render_date_page(date(2026, 9, 24), [_customer_outcome()])
    assert "Customer sends" in page
    assert ">Customer<" in page
    assert "000001" in page
    assert "Print Home" in page


def test_the_creation_date_is_the_date_the_page_is_filed_under():
    """A welcome has no invoice date, so the account creation date stands in —
    which is also the date that makes the "new customer" claim checkable."""
    page = render_date_page(date(2026, 9, 24), [_customer_outcome()])
    assert "24/09/2026" in page or "2026-09-24" in page


def test_a_mixed_page_names_the_kind_of_every_row():
    page = render_date_page(
        date(2026, 9, 24),
        [_customer_outcome(), _payment_outcome(), _invoice_outcome()],
    )
    assert "Message sends" in page, "three kinds on one day gets the neutral title"
    assert ">Customer<" in page
    assert ">Payment<" in page
    assert ">Invoice<" in page


def test_a_customer_and_a_payment_page_gets_the_neutral_title():
    page = render_date_page(
        date(2026, 9, 24), [_customer_outcome(), _payment_outcome()]
    )
    assert "Message sends" in page


def test_an_invoice_only_page_keeps_its_original_title():
    """So the existing report pages an operator already has do not change."""
    page = render_date_page(date(2026, 9, 2), [_invoice_outcome()])
    assert "Invoice sends" in page


def test_a_payment_only_page_keeps_its_title():
    page = render_date_page(date(2026, 9, 1), [_payment_outcome()])
    assert "Payment sends" in page


def test_an_unknown_kind_does_not_break_the_page():
    """A kind a future version invents must degrade, not crash a page."""
    page = render_date_page(date(2026, 9, 24), [_customer_outcome(kind="something-new")])
    assert "000001" in page


def test_the_abandoned_status_renders_for_a_customer():
    """A customer whose number was rejected permanently shows up here, which is the
    only place an operator would learn about it."""
    page = render_date_page(
        date(2026, 9, 24),
        [_customer_outcome(status=ABANDONED, error="not on WhatsApp")],
    )
    assert "Abandoned" in page
    assert "not on WhatsApp" in page


def test_the_index_is_titled_neutrally():
    """One audit trail covers all three pipelines, so the page may no longer be
    called the invoice report.

    Scoped to the title and heading: the body legitimately names the Invoice
    category, which is the point of showing all three.
    """
    html = render_index([date(2026, 9, 24)], {date(2026, 9, 24): (1, 0)}, {})
    assert "invoice" not in html.split("</head>")[0].lower()
    assert "invoice" not in html.split("<h1>")[1].split("</h1>")[0].lower()
    assert "2026-09-24" in html


class TestIndexAlwaysShowsEveryCategory:
    """The index must advertise all three pipelines, not only the busy one.

    An index assembled purely from the rows on disk cannot distinguish "this
    pipeline is deployed but idle today" from "this pipeline does not exist",
    so a client would conclude the payments and welcomes were not audited.
    """

    def test_all_three_kinds_listed_when_only_invoices_have_rows(self) -> None:
        page = render_index(
            [date(2026, 9, 24)],
            {date(2026, 9, 24): (1, 0)},
            {KIND_INVOICE: (1, 0)},
        )
        for label in ("Invoice", "Payment", "Customer"):
            assert label in page

    def test_empty_tallies_render_as_zero_not_a_dash(self) -> None:
        page = render_index([], {}, {KIND_INVOICE: (2, 1)})
        assert "0" in page
        assert "2" in page and "1" in page

    def test_totals_sum_every_kind(self) -> None:
        page = render_index(
            [],
            {},
            {KIND_INVOICE: (2, 1), KIND_PAYMENT: (3, 0), KIND_CUSTOMER: (1, 2)},
        )
        assert "<strong>6</strong>" in page
        assert "<strong>3</strong>" in page

    def test_subtitle_names_all_three_categories(self) -> None:
        page = render_index([], {}, {})
        sub = page.split('<p class="sub">')[1].split("</p>")[0]
        assert "invoices" in sub
        assert "payment confirmations" in sub
        assert "welcomes" in sub

    def test_categories_appear_even_with_no_history_at_all(self) -> None:
        page = render_index([], {}, {})
        for label in ("Invoice", "Payment", "Customer"):
            assert label in page
        assert "No sends have been recorded yet." in page

    def test_unknown_kind_is_still_listed(self) -> None:
        page = render_index([], {}, {"something-new": (1, 0)})
        assert "something-new" in page

    def test_date_table_is_still_present(self) -> None:
        page = render_index([date(2026, 9, 24)], {date(2026, 9, 24): (1, 0)}, {})
        assert "By date" in page
        assert "2026-09-24.html" in page
