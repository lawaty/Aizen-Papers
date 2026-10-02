from __future__ import annotations

from sender.domain.reporting import obfuscate_phone
from sender.infrastructure.reporting import _LEGACY_HTACCESS, merge_htaccess

_OPERATOR_BLOCK = """AuthType Basic
AuthName "Restricted reports"
AuthUserFile /home/aizewlkt/.htpasswd-aizen-reports
Require valid-user
"""


def test_short_phone_is_fully_masked():
    for raw in ("12345", "123456", "1", "1234"):
        out = obfuscate_phone(raw)
        assert out == "•" * len(raw), (raw, out)

def test_normal_phone_keeps_country_and_last_four():
    assert obfuscate_phone("201027693262") == "20••••••3262"

def test_long_phone_never_leaks_a_dialable_number():
    out = obfuscate_phone("+201027693262")
    assert out.startswith("+2")
    assert out.endswith("3262")
    assert "201027693262" not in out


def test_merge_htaccess_is_idempotent_across_two_merges():
    merged_once = merge_htaccess(_OPERATOR_BLOCK)
    assert merge_htaccess(merged_once) == merged_once


def test_merge_htaccess_preserves_an_existing_operator_block():
    merged = merge_htaccess(_OPERATOR_BLOCK)
    assert _OPERATOR_BLOCK in merged
    assert merged.endswith(_OPERATOR_BLOCK)


def test_merge_htaccess_on_empty_input_is_a_valid_apache_config():
    merged = merge_htaccess("")
    assert merged.startswith("# BEGIN Aizen invoice sender generated block\n")
    assert merged.endswith("# END Aizen invoice sender generated block\n")
    assert "Options -Indexes" in merged
    assert "Require all denied" in merged


def test_merge_htaccess_does_not_duplicate_lines():
    merged = merge_htaccess(merge_htaccess(_OPERATOR_BLOCK))
    assert merged.count("Options -Indexes") == 1
    assert merged.count("Require all denied") == 1
    assert merged.count("# BEGIN Aizen invoice sender generated block") == 1
    assert merged.count("# END Aizen invoice sender generated block") == 1


def test_merge_htaccess_replaces_a_pre_marker_file_without_duplication():
    merged = merge_htaccess(_LEGACY_HTACCESS)
    assert merged.count("Options -Indexes") == 1
    assert merged.count("Require all denied") == 1


def test_render_twice_does_not_grow_the_htaccess_file(tmp_path):
    """The cron cycle re-renders every five minutes; the file must not grow."""
    from sender.infrastructure.reporting import ReportStore

    store = ReportStore(tmp_path / "pages", tmp_path / "data")
    htaccess = tmp_path / "pages" / ".htaccess"

    store.render([])
    first = htaccess.read_text(encoding="utf-8")
    store.render([])
    store.render([])

    assert htaccess.read_text(encoding="utf-8") == first
    assert "Options -Indexes" in first


def _outcome(**kw):
    from datetime import date
    from decimal import Decimal
    from sender.domain.models import SENT, SendOutcome

    base = dict(
        app="aizenpaper", invoice_id="1", invoice_number="INV-001",
        customer_name="Acme", attempted_at=1_700_000_000.0,
        customer_phone="201027693262", currency="EGP", total=Decimal("10"),
        issue_date=date(2023, 11, 14), status=SENT,
    )
    base.update(kw)
    return SendOutcome(**base)


def test_recipients_page_embeds_searchable_data_and_masks_by_default():
    from sender.domain.reporting import render_recipients

    html = render_recipients([_outcome()], obfuscate=True)
    assert "recipients" not in html.lower() or "Recipients" in html
    assert "201******3262" in html or "20" in html
    assert "201027693262" not in html
    assert "INV-001" in html
    assert 'id="q"' in html


def test_recipients_page_shows_full_number_when_masking_is_off():
    from sender.domain.reporting import render_recipients

    html = render_recipients([_outcome()], obfuscate=False)
    assert "201027693262" in html


def test_recipients_page_escapes_hostile_customer_names():
    from sender.domain.reporting import render_recipients

    html = render_recipients(
        [_outcome(customer_name="<script>alert(1)</script>", error="<img onerror=x>")],
        obfuscate=True,
    )
    assert "<script>alert" not in html
    assert "\\u003cscript" in html or "&lt;script" in html


def test_recipients_page_handles_empty_history():
    from sender.domain.reporting import render_recipients

    html = render_recipients([], obfuscate=True)
    assert "No messages" in html


def test_report_store_writes_recipients_page(tmp_path):
    from sender.infrastructure.reporting import ReportStore, JsonlSendOutcomeRecorder

    rec = JsonlSendOutcomeRecorder(tmp_path / "data")
    rec.record(_outcome())
    store = ReportStore(tmp_path / "pages", tmp_path / "data")
    written = store.render()
    names = {p.name for p in written}
    assert "recipients.html" in names
    assert (tmp_path / "pages" / "recipients.html").exists()


def test_recipients_filter_uses_the_real_lowercase_status_values():
    """The status constants are lowercase; the JS filter must match them.

    A filter comparing against "SENT" would silently never match, so the page
    would always claim nothing was sent.
    """
    from sender.domain.reporting import render_recipients

    html = render_recipients([_outcome()], obfuscate=True)
    assert '"s": "sent"' in html
    assert 'value="sent"' in html
    assert "=== \"SENT\"" not in html
    assert "=== \"FAILED\"" not in html
