"""Offline pins for the polling half of the audit.

What is pinned here, all against the current behaviour:

- **First-run seeding takes a single listing snapshot** (``poller._poll_app``
  reads one candidate list, marks exactly those ids seen) — so an invoice
  created after the snapshot is *not* swallowed, and an invoice created *during*
  the send phase is not marked seen unsent.
- **The state is persisted once per app per cycle**, at batch exit — so a crash
  between the last send and that single atomic write re-sends everything sent in
  that cycle. At-least-once, by design, with a real duplicate-send window.
- **Reach is limit x max_pages.** A first run against a backlog larger than the
  reach seeds only the newest ``limit * max_pages`` rows, and the older ones are
  never re-reached: paging stops as soon as a page's last row is already seen.
  The saturation warning is not cosmetic, it is a silent permanent loss.
- **Listing order is load-bearing and unprotected.** The live Daftra list is
  newest-first (read-only observation), which is the only order in which this
  design works at all; an oldest-first listing would make new invoices
  invisible. The customers pipeline is audited separately in
  ``test_audit_polling_customers.py``, where this property is *live* rather than
  historical: ``/clients.json`` does not order by creation on its own.
- **The 0-byte ``<state>.lock`` file is an flock anchor, not a stale lock.**

Fully offline and deterministic: fake source, fake sender, fake clock, real
:class:`JsonPollStateStore` on a ``tmp_path``. No message is ever sent.
"""

import json

from fakes import CapturingSender

from sender.application.poller import InvoicePoller, PollApp
from sender.domain.templates import LegacyInvoiceTemplateBuilder
from sender.infrastructure.state import JsonPollStateStore, PollStateLock
from sender.presentation.stubs import StubInvoiceSource, make_stub_invoice

PHONE = "01027693262"


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class OldestFirstSource:
    """A source whose listing is OLDEST-first — the opposite of real Daftra.

    Records every id it hands out so "never fetched" can be distinguished from
    "fetched but not sent".
    """

    def __init__(self, invoices) -> None:
        self._invoices = list(invoices)
        self.fetched_ids: list[str] = []
        self.pages_served = 0

    def add(self, invoice) -> None:
        self._invoices.append(invoice)

    def list_invoices(self, limit: int = 10, page: int = 1):
        self.pages_served += 1
        start = (page - 1) * limit
        rows = self._invoices[start : start + limit]
        self.fetched_ids.extend(str(invoice.id) for invoice in rows)
        return list(rows)

    def get_invoice(self, invoice_id):
        for invoice in self._invoices:
            if str(invoice.id) == str(invoice_id):
                return invoice
        raise ValueError(f"no invoice {invoice_id!r}")

    def get_raw_invoice(self, invoice_id) -> dict:
        return {}


class SourceMutatingSender(CapturingSender):
    """Records payloads, and creates a new invoice on the source mid-run.

    The invoice is added *while* a send is in flight, i.e. strictly after the
    cycle's listing snapshot was taken.
    """

    def __init__(self, source, trigger_number: str, new_invoice) -> None:
        super().__init__()
        self._source = source
        self._trigger = trigger_number
        self._invoice = new_invoice
        self.mutated = False

    def send(self, payload: dict) -> dict:
        response = super().send(payload)
        if not self.mutated and _number_of(payload) == self._trigger:
            self.mutated = True
            self._source.seed(self._invoice)
        return response


class ExplodingSaveStore(JsonPollStateStore):
    """A store whose atomic write always fails, i.e. a crash after the sends
    but before the single batch-exit persist."""

    def _save(self) -> None:
        raise RuntimeError("crash before the batch-exit state write")


def _number_of(payload: dict) -> str:
    body = next(c for c in payload["template"]["components"] if c["type"] == "body")
    return body["parameters"][1]["text"].strip("\u2066\u2068\u2069")


def _sent_numbers(sender) -> list[str]:
    return [_number_of(payload) for payload in sender.payloads]


def _invoices(count: int, start: int = 1):
    return [
        make_stub_invoice(id=str(i), number=f"INV-{i:03d}", customer_phones=(PHONE,))
        for i in range(start, start + count)
    ]


def _builder() -> LegacyInvoiceTemplateBuilder:
    return LegacyInvoiceTemplateBuilder(template_name="aizen_invoice", language="en", country_code="20")


def _poller(source, sender, state, **kwargs) -> InvoicePoller:
    return InvoicePoller(
        apps=[PollApp(name="app1", source=source)],
        sender=sender,
        builder=_builder(),
        state=state,
        clock=kwargs.pop("clock", None) or FakeClock(),
        **kwargs,
    )


def _run(state_path, source, sender, store_cls=JsonPollStateStore, **kwargs) -> dict:
    """One cycle with a *fresh* poller and a *fresh* store over ``state_path``.

    A new store per cycle is what a cron/systemd ``--once`` run actually does:
    the only thing carried between cycles is the file on disk.
    """
    poller = _poller(source, sender, store_cls(str(state_path)), **kwargs)
    return poller.run_once()


# --- 1. first-run seeding is a single snapshot ----------------------------------


def test_first_run_seeding_uses_a_single_snapshot__invoice_created_after_seeding_is_sent_next_run(tmp_path):
    """Seeding marks exactly the ids of ONE listing, and only those. An invoice
    created after that snapshot is still unknown next cycle, so it is still sent
    next cycle — the seed is a snapshot, not a high-water mark that would swallow
    everything up to the newest id it happened to observe."""
    path = tmp_path / "poll_state.json"
    source = StubInvoiceSource(_invoices(2))  # A (id 1), B (id 2)
    run1_sender = CapturingSender()

    first = _run(path, source, run1_sender)["apps"][0]
    assert first["first_run"] is True
    assert first["listed"] == 2
    assert first["seeded"] == 2
    assert first["sent"] == 0
    assert run1_sender.payloads == []

    source.add_new_invoice(customer_phones=(PHONE,))  # C (id 3), created after the snapshot
    run2_sender = CapturingSender()
    second = _run(path, source, run2_sender, send_existing=True)["apps"][0]

    assert second["sent"] == 1
    assert _sent_numbers(run2_sender) == ["INV-003"]
    # A and B were seeded, so they are never sent — by any run.
    assert _sent_numbers(run1_sender) + _sent_numbers(run2_sender) == ["INV-003"]
    state = JsonPollStateStore(str(path))
    assert state.seen("app1", "1") is True
    assert state.seen("app1", "2") is True
    assert state.seen("app1", "3") is True


# --- 2. an invoice created mid-cycle is not marked seen unsent ------------------


def test_invoice_created_during_the_send_phase_is_not_swallowed(tmp_path):
    """State only ever contains ids from the cycle's listing snapshot.

    D is created between run 1 and run 2, and E is created *while run 2 is
    sending D* — after the snapshot was taken. E therefore cannot be in the
    candidate list, so it cannot be marked seen, so run 3 sends it. If the state
    were written from "everything currently in the source" instead of from the
    snapshot, E would be silently retired without ever being sent.
    """
    path = tmp_path / "poll_state.json"
    source = StubInvoiceSource(_invoices(2))  # A (id 1), B (id 2)

    run1_sender = CapturingSender()
    assert _run(path, source, run1_sender)["apps"][0]["seeded"] == 2

    source.add_new_invoice(customer_phones=(PHONE,))  # D (id 3)
    e = make_stub_invoice(id="4", number="INV-004", customer_phones=(PHONE,))
    run2_sender = SourceMutatingSender(source, trigger_number="INV-003", new_invoice=e)

    second = _run(path, source, run2_sender, send_existing=True)["apps"][0]

    assert run2_sender.mutated is True  # E really did appear mid-send
    assert second["listed"] == 3  # the snapshot held A, B, D — not E
    assert second["sent"] == 1
    assert _sent_numbers(run2_sender) == ["INV-003"]

    state_after_run2 = JsonPollStateStore(str(path))
    assert state_after_run2.seen("app1", "4") is False  # E was NOT marked seen

    run3_sender = CapturingSender()
    third = _run(path, source, run3_sender, send_existing=True)["apps"][0]
    assert third["sent"] == 1
    assert _sent_numbers(run3_sender) == ["INV-004"]


# --- 3. one atomic write per cycle => at-least-once duplicates ------------------


def test_crash_after_send_before_state_write_resends_on_next_run__duplicate_send_risk(tmp_path):
    """At-least-once, with a real duplicate-send window.

    The sends happen one by one and the seen marks are only persisted ONCE, at
    the batch exit, so a crash (or a failed write) between the last send and
    that single atomic write loses *every* mark made in the cycle. The next run
    reads a state file that never learned about them and sends the same customer
    a second invoice notification. The per-app isolation handler keeps the cycle
    from taking the other tenants down, which is why this is silent: the app is
    merely reported not-ok.

    The summary of the failed cycle keeps the counts the cycle had already
    accumulated (``InvoicePoller._poll_app`` catches the failure itself and only
    flips ``ok``/``error``), so an operator reading the cycle output still sees
    that the customer was messaged.
    """
    path = tmp_path / "poll_state.json"
    source = StubInvoiceSource(_invoices(2))  # A (id 1), B (id 2)

    run1_sender = CapturingSender()
    assert _run(path, source, run1_sender)["apps"][0]["seeded"] == 2

    source.add_new_invoice(customer_phones=(PHONE,))  # D (id 3)
    run2_sender = CapturingSender()
    summary = _run(path, source, run2_sender, store_cls=ExplodingSaveStore, send_existing=True)
    app2 = summary["apps"][0]

    # The send happened...
    assert _sent_numbers(run2_sender) == ["INV-003"]
    # ...the persist failed, and the cycle is reported as a failed app.
    assert app2["ok"] is False
    assert "crash before the batch-exit state write" in (app2["error"] or "")
    # The reported counts are the cycle's real ones, not a zeroed skeleton:
    # the operator can still tell a customer was messaged.
    assert app2["sent"] == 1
    assert app2["listed"] == 3
    assert [outcome["status"] for outcome in app2["invoices"]] == ["sent"]
    # Nothing reached the disk, so D is still unknown to the next process.
    on_disk = json.loads(path.read_text())
    assert on_disk["apps"]["app1"]["seen"] == ["2", "1"]

    run3_sender = CapturingSender()
    third = _run(path, source, run3_sender, send_existing=True)["apps"][0]

    assert third["sent"] == 1
    assert _sent_numbers(run3_sender) == ["INV-003"]  # D AGAIN
    assert _sent_numbers(run2_sender) + _sent_numbers(run3_sender) == ["INV-003", "INV-003"]


# --- 4. saturation is permanent loss, not a warning ------------------------------


def test_burst_beyond_limit_times_max_pages_is_permanently_missed(tmp_path, caplog):
    """Reach is limit x max_pages (10 x 5 = 50 by default), and the rows below
    it are unreachable forever.

    Paging stops as soon as a page's LAST row is already seen, so once the
    50-newest of a burst are handled, page 1 is entirely seen territory and the
    cycle never looks further back. The 10 oldest invoices of the burst are
    therefore not "delayed" — they are never fetched again, so they are never
    sent, no matter how many cycles run. The saturation warning that fires is
    the only signal, and it says "may be missed".
    """
    path = tmp_path / "poll_state.json"
    source = StubInvoiceSource(_invoices(50))  # exactly the default reach
    run1_sender = CapturingSender()

    with caplog.at_level("WARNING"):
        first = _run(path, source, run1_sender, limit=10, max_pages=5)["apps"][0]
    assert first["seeded"] == 50
    assert first["listed"] == 50
    assert first["sent"] == 0
    assert any("came back full" in record.message for record in caplog.records)
    assert any("more than 5 pages of unseen invoices" in record.message for record in caplog.records)

    # A burst of 60 new invoices (ids 51..110) while the poller is down.
    for invoice in _invoices(60, start=51):
        source.seed(invoice)
    burst_oldest = [f"INV-{i:03d}" for i in range(51, 61)]
    burst_newest = [f"INV-{i:03d}" for i in range(61, 111)]

    caplog.clear()
    run2_sender = CapturingSender()
    with caplog.at_level("WARNING"):
        second = _run(path, source, run2_sender, limit=10, max_pages=5, send_existing=True, max_sends_per_run=0)["apps"][0]
    assert second["listed"] == 50  # only the 50 newest of the burst
    assert second["sent"] == 50
    assert set(_sent_numbers(run2_sender)) == set(burst_newest)
    assert any("came back full" in record.message for record in caplog.records)
    assert any("more than 5 pages of unseen invoices" in record.message for record in caplog.records)

    caplog.clear()
    run3_sender = CapturingSender()
    with caplog.at_level("WARNING"):
        third = _run(path, source, run3_sender, limit=10, max_pages=5, send_existing=True)["apps"][0]
    # Page 1 is entirely seen, so paging stops after one page and nothing moves.
    assert third["listed"] == 10
    assert third["new"] == 0
    assert third["sent"] == 0
    assert _sent_numbers(run3_sender) == []

    # Across every run: the 10 oldest invoices of the burst were never sent.
    everything = _sent_numbers(run1_sender) + _sent_numbers(run2_sender) + _sent_numbers(run3_sender)
    # Sent newest-first, in listing order.
    assert everything == sorted(burst_newest, reverse=True)
    assert set(burst_oldest).isdisjoint(everything)


# --- 5. the ordering assumption is load-bearing ---------------------------------


def test_oldest_first_listing_silently_misses_new_invoices__ordering_assumption_is_load_bearing(tmp_path):
    """Documentation, not a live bug.

    A read-only live observation of the real Daftra listing already confirmed it
    is NEWEST-FIRST (strictly descending ids, id breaking same-date ties), so
    this is what the code is written against. Nothing in the code, the state
    file, or the docs *enforces* it, though: the entire design rests on the
    assumption that new invoices appear at the head of page 1, and paging then
    walks backwards from there.

    With an oldest-first listing the assumption fails silently and totally — a
    first run seeds the OLDEST ``limit * max_pages`` rows, and from then on page
    1 is entirely seen territory, so the pager breaks immediately and a brand
    new invoice at the tail of the list is never fetched and never sent. There
    is no warning, no error, and no state that could recover it.
    """
    path = tmp_path / "poll_state.json"
    source = OldestFirstSource(_invoices(60))  # ids 1..60, oldest first
    run1_sender = CapturingSender()

    first = _run(path, source, run1_sender, limit=10, max_pages=5)["apps"][0]
    assert first["seeded"] == 50
    assert first["sent"] == 0
    seeded = JsonPollStateStore(str(path)).seen_ids("app1")
    assert sorted(int(i) for i in seeded) == list(range(1, 51))  # the 50 OLDEST

    new = make_stub_invoice(id="N", number="INV-NEW", customer_phones=(PHONE,))
    source.add(new)  # appended last, i.e. newest -> last row of the listing
    pages_before = source.pages_served
    run2_sender = CapturingSender()
    second = _run(path, source, run2_sender, limit=10, max_pages=5, send_existing=True)["apps"][0]

    # Exactly one page was fetched, and paging stopped on the seen last row.
    assert source.pages_served - pages_before == 1
    assert second["listed"] == 10
    assert second["new"] == 0
    assert second["sent"] == 0
    assert _sent_numbers(run2_sender) == []
    assert "N" not in source.fetched_ids
    assert JsonPollStateStore(str(path)).seen("app1", "N") is False


# --- 6. the 0-byte lock file is an anchor, not a stale lock ---------------------


def test_preexisting_zero_byte_lock_file_does_not_block_a_new_poller(tmp_path):
    """The repo ships a 0-byte ``poll_state.json.lock``; it is an flock anchor,
    not a stale lock. ``flock`` is advisory and owned by the open file
    description, so the file existing proves nothing about whether a poller holds
    it: the kernel releases it when the holder exits, so a crashed poller cannot
    wedge the next run. An operator (or a ``git clone``) that sees the file must
    not be tempted to delete it as a "stale lock"."""
    state_path = tmp_path / "poll_state.json"
    lock_file = tmp_path / "poll_state.json.lock"
    lock_file.write_bytes(b"")  # 0 bytes, exactly like the one in the repo root
    assert lock_file.exists() and lock_file.stat().st_size == 0

    lock = PollStateLock(str(state_path))
    lock.acquire()
    lock.release()

    again = PollStateLock(str(state_path))
    again.acquire()
    again.release()

    # And it still excludes a genuine concurrent holder.
    held = PollStateLock(str(state_path))
    held.acquire()
    try:
        try:
            PollStateLock(str(state_path)).acquire()
        except RuntimeError as exc:
            assert "another poller is already running" in str(exc)
        else:  # pragma: no cover - a lock that does not lock is a bigger finding
            raise AssertionError("the 0-byte anchor stopped excluding a second holder")
    finally:
        held.release()


# --- 7. the live 57-invoice app ---------------------------------------------------


def test_first_run_with_backlog_beyond_reach_seeds_only_the_newest__models_the_live_57_invoice_app(tmp_path, caplog):
    """Models the real ``mohamedsoph2006`` account: 57 invoices with the default
    ``--limit 10 --max-pages 5``.

    A first run therefore seeds only the 50 NEWEST and the 7 oldest are neither
    seeded nor sent — and because the seeded window is contiguous from the head
    of the listing, the 7 are never re-reached on any later cycle either. From
    the operator's point of view the first run looks perfect (50 seeded, 0 sent,
    "the backlog is taken care of"), which is precisely what makes the gap
    invisible. The 7 oldest customers are notified only if the state is reset
    with a larger reach.
    """
    path = tmp_path / "poll_state.json"
    source = StubInvoiceSource(_invoices(57))  # ids 1..57, newest first
    run1_sender = CapturingSender()

    with caplog.at_level("WARNING"):
        first = _run(path, source, run1_sender, limit=10, max_pages=5)["apps"][0]

    assert first["first_run"] is True
    assert first["seeded"] == 50
    assert first["listed"] == 50
    assert first["sent"] == 0
    assert run1_sender.payloads == []
    assert any("came back full" in record.message for record in caplog.records)
    assert any("more than 5 pages of unseen invoices" in record.message for record in caplog.records)

    seeded = JsonPollStateStore(str(path)).seen_ids("app1")
    assert sorted(int(i) for i in seeded) == list(range(8, 58))  # ids 8..57 = the 50 newest
    seven_oldest = [f"INV-{i:03d}" for i in range(1, 8)]
    assert not set(seven_oldest) & set(_sent_numbers(run1_sender))
    state = JsonPollStateStore(str(path))
    for invoice_id in range(1, 8):
        assert state.seen("app1", str(invoice_id)) is False

    # Nothing new happened: one page, all seen, nothing sent.
    caplog.clear()
    run2_sender = CapturingSender()
    second = _run(path, source, run2_sender, limit=10, max_pages=5, send_existing=True)["apps"][0]
    assert second["listed"] == 10
    assert second["new"] == 0
    assert second["sent"] == 0
    assert _sent_numbers(run2_sender) == []
    # The 7 oldest are still unknown and still unreachable.
    state2 = JsonPollStateStore(str(path))
    assert [i for i in range(1, 8) if state2.seen("app1", str(i))] == []
    assert set(seven_oldest).isdisjoint(_sent_numbers(run1_sender) + _sent_numbers(run2_sender))
