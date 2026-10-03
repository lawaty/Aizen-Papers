"""Polling use case: watch Daftra for new documents and notify customers.

Two pipelines share this module. :class:`InvoicePoller` announces new invoices
under ``aizen_invoice``; :class:`PaymentPoller` announces recorded payments under
``aizen_new_payment``. They differ in almost nothing that matters: both list a
paged, newest-first source per tenant, diff it against a per-app state store,
send what is new, and classify every failure the same way. All of that lives once
in :class:`DocumentPoller`, and each pipeline is a thin subclass supplying only
its source call, its template builder and its wording.

That split is deliberate and is not a refactor for its own sake. The rules below
are the reason a customer never silently misses a notification, and a second
copy of them would be a second chance to get one of them wrong. When the payments
pipeline was added, the invariants did not get re-implemented — they got a second
caller.

The poller is otherwise deliberately thin: it decides what is new and what to do
about it, and all I/O (Daftra, WhatsApp, the state file, sleeping) arrives through
ports so the whole loop is testable offline.

Failure policy (see docs/design.md §8):

- **Retryable** failures (network/timeout, HTTP 429, HTTP 5xx) are never given
  up. They are recorded in the state as ``pending`` with a bounded exponential
  backoff and retried on later cycles; a retryable failure can never become a
  silent permanent loss.
- **Permanent** failures (validation, missing ``public_url``, template
  rejection, self-send, any other non-transient error) are given up on the
  first attempt: the document is marked seen and recorded in the state as
  ``abandoned`` so an operator can see it and re-drive it.
- **Template contract failures** (the ``132000``-series) are the one exception
  to that split, because they are fixable by an operator rather than by waiting:
  a ``132001`` (no usable translation of the template in the requested language)
  retried unchanged would fail identically forever, so the poller falls back to a
  free-form message when ``freeform_fallback`` is on (see
  :meth:`DocumentPoller._send_freeform_fallback`) and records the document as
  ``pending`` with the *template* error when it is off or when the fallback send
  fails too. It is never ``abandoned`` and never marked seen on that path — a
  bridge must not consume documents.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Generic, Sequence, TypeVar

from sender.domain.errors import ApiError, WhatsAppApiError, WhatsAppSelfSendError, is_template_error
from sender.domain.models import (
    ABANDONED,
    FAILED,
    KIND_CUSTOMER,
    KIND_INVOICE,
    KIND_PAYMENT,
    SENT,
    Customer,
    Invoice,
    Payment,
    SendOutcome,
)
from sender.domain.phones import normalize_phone
from sender.domain.ports import (
    Clock,
    CustomerSource,
    InvoiceSource,
    MessageSender,
    PaymentSource,
    PollStateStore,
    SendOutcomeRecorder,
)
from sender.domain.templates import TemplateBuilder, build_fallback_document

log = logging.getLogger(__name__)

#: The document a pipeline announces (an :class:`Invoice` or a :class:`Payment`)
#: and the source it reads them from. Both pipeline classes are generic in these
#: so the engine below type-checks against either without casts, while running
#: exactly the same code for both.
TDoc = TypeVar("TDoc", Invoice, Payment)
TSource = TypeVar("TSource", InvoiceSource, PaymentSource)

_HANDLED_ERRORS = (ApiError, ValueError, RuntimeError)

# Meta error codes that are transient backpressure: retry with backoff. These
# arrive as HTTP 400, so the HTTP status alone would call them permanent.
_RETRYABLE_API_CODES = frozenset({4, 80007, 130429, 131056})

# Meta error codes that reflect number/quality state rather than a bad request.
# Retrying these actively worsens the metric Meta is enforcing, so they are
# treated as permanent and recorded as abandoned for an operator to look at.
_PERMANENT_API_CODES = frozenset({131047, 131048, 131049})


def _error_code(exc: Exception) -> Any:
    """Meta's error code from an API error's payload, or ``None``."""
    return ((getattr(exc, "payload", None) or {}).get("error") or {}).get("code")


def _header_document(payload: dict) -> dict | None:
    """The already-uploaded header document inside a built template payload.

    Only the ``{"id": ..., "filename": ...}`` shape counts. That id is an asset
    *we* uploaded, so reusing it in a free-form message costs nothing and cannot
    fail on a fetch; a ``link`` document is the opposite (Meta has to fetch it,
    and Daftra's own PDF url is session-gated), so the fallback falls back again
    to plain text instead of guessing. A document without a filename is skipped
    for the same reason — a free-form document message with no name is a worse
    message than the text one.
    """
    template = payload.get("template") or {}
    for component in template.get("components") or []:
        if component.get("type") != "header":
            continue
        for parameter in component.get("parameters") or []:
            if parameter.get("type") != "document":
                continue
            document = parameter.get("document") or {}
            if document.get("id") and document.get("filename"):
                return document
    return None


def _wamid(response: dict | None) -> str | None:
    """Meta's message id from a send response, or ``None``.

    Recorded for the report so an operator can trace a specific row back to
    Meta's own logs. Every layer is optional in the payload, so this stays
    defensive rather than indexing directly.
    """
    messages = (response or {}).get("messages") or []
    if not messages:
        return None
    identifier = (messages[0] or {}).get("id")
    return str(identifier) if identifier else None


def _is_retryable(exc: Exception) -> bool:
    """Classify a handled error as transient (retry) or permanent (give up).

    Network/timeout errors carry ``status=None``; HTTP 429 and 5xx are the
    transient classes. Everything else (4xx, validation, template rejection,
    self-send) can never succeed on retry.

    Meta's payload-level throttle codes are classified too, because they arrive
    as an HTTP 400 and the status alone would misread them as permanent. Which
    side of the line a code falls on matters a lot:

    - ``130429`` (throughput) and ``131056`` (too many messages to one
      recipient) are pure backpressure: they clear on their own, so retrying
      with backoff is exactly right.
    - ``131048`` (number restricted, "too many previous messages were blocked
      or flagged as spam"), ``131049`` (per-user marketing limit, adaptive and
      unpublished) and ``131047`` (outside the 24h customer service window) are
      *quality* signals. Retrying them feeds the very behaviour Meta is
      penalising and escalates to a block, so they stay permanent — for
      ``131047`` the correct fix is a template, which the template-fallback path
      above already handles.

    See docs/guide/rate-limits.md for the sources.
    """
    if isinstance(exc, WhatsAppSelfSendError):
        return False
    if isinstance(exc, ApiError):
        if _error_code(exc) in _RETRYABLE_API_CODES:
            return True
        if _error_code(exc) in _PERMANENT_API_CODES:
            return False
        status = exc.status
        if status is None:
            return True
        if status == 429 or (500 <= status < 600):
            return True
    return False


@dataclass(frozen=True)
class PollApp(Generic[TSource]):
    """One tenant in a poll cycle: its name (which keys its state) and its source."""

    name: str
    source: TSource


class DocumentPoller(Generic[TDoc, TSource]):
    """The shared engine: list, diff against state, send what is new, classify.

    Subclasses supply only what is genuinely per-document — which source call
    lists and which fetches a single record, whether a listing row is too thin to
    send from, and how a send maps onto a report row. Everything else, including
    every safety invariant, is inherited unchanged:

    1. the first run per app **seeds without sending**;
    2. a dry run **writes nothing** to the state;
    3. a retryable failure is **never** abandoned.

    A subclass that changed any of those would not be "a payment poller", it would
    be a second, less-tested copy of the notification guarantee.
    """

    #: Document kind, also the noun used in log lines and the ``kind`` key of the
    #: per-app summary. Overridden by every concrete pipeline.
    KIND = KIND_INVOICE
    #: The env var that raises the per-run send cap, named in the warning that
    #: fires when the cap bites. Each pipeline has its own cap, so each has to
    #: tell the operator which knob to turn.
    CAP_ENV_VAR = "POLL_MAX_SENDS_PER_RUN"

    def __init__(
        self,
        apps: Sequence[PollApp],
        sender: MessageSender | None,
        builder: TemplateBuilder,
        state: PollStateStore,
        clock: Clock,
        *,
        interval: float = 60.0,
        limit: int = 10,
        dry_run: bool = False,
        send_existing: bool = False,
        once: bool = False,
        max_cycles: int | None = None,
        timeout: float | None = None,
        max_backoff: float = 3600.0,
        max_pages: int = 5,
        country_code: str = "20",
        freeform_fallback: bool = True,
        max_sends_per_run: int | None = 10,
        recorder: SendOutcomeRecorder | None = None,
    ) -> None:
        self._apps = list(apps)
        self._sender = sender
        self._builder = builder
        self._state = state
        self._clock = clock
        self._interval = interval
        self._limit = limit
        self._dry_run = dry_run
        self._send_existing = send_existing
        self._once = once
        self._max_cycles = max_cycles
        self._timeout = timeout
        self._max_backoff = max_backoff
        self._max_pages = max_pages
        self._country_code = country_code
        self._freeform_fallback = freeform_fallback
        self._max_sends_per_run = max_sends_per_run
        self._recorder = recorder
        # Sends actually attempted during the current cycle. This is the cap
        # that bounds Meta's exposure, so it is incremented at the send site
        # (and on a send that then fails) rather than inferred from a count of
        # successes.
        self._sends_this_cycle = 0

    # -- per-document hooks -------------------------------------------------
    #
    # Everything below is what a subclass has to say to become a pipeline. Each
    # one is a thin adapter over a source call or a model field; none of them may
    # reimplement a policy decision.

    @property
    def _noun(self) -> str:
        """Singular document noun, for log lines ("invoice 12", "payment 000116")."""
        return self.KIND

    @property
    def _plural(self) -> str:
        return f"{self.KIND}s"

    def _list_documents(self, app: PollApp, *, limit: int, page: int) -> list[TDoc]:
        """One page of documents, newest first."""
        raise NotImplementedError("a pipeline must say how it lists its documents")

    def _fetch_document(self, app: PollApp, document_id: str) -> TDoc:
        """The authoritative record for *document_id*, customer details included."""
        raise NotImplementedError("a pipeline must say how it fetches one document")

    def _label(self, doc: TDoc) -> str:
        """The human-facing identifier for a document, as the customer would see it.

        The invoice number, the payment reference code. It is what log lines and
        the summary rows print, so it must be something a person can look up in
        the ERP — never an internal id.
        """
        return doc.number

    def _needs_detail(self, doc: TDoc) -> bool:
        """Whether this listing row is too thin to send from as-is.

        The default is "no reachable phone", which every pipeline needs. Sources
        that carry less than that in their listing rows override it; see
        :meth:`InvoicePoller._needs_detail`.
        """
        return not doc.customer_phones

    def _after_detail_fetch(self, app: PollApp, doc: TDoc) -> None:
        """Warn about a document the detail fetch still could not complete.

        A no-op by default: it exists so a pipeline whose detail is genuinely
        incomplete can say so out loud rather than quietly shipping a message
        that understates what happened.
        """

    def _make_outcome(
        self,
        app: PollApp,
        doc: TDoc,
        document_id: str,
        status: str,
        *,
        recipient: str | None = None,
        error: str | None = None,
        fallback: bool = False,
        wamid: str | None = None,
    ) -> SendOutcome:
        """Map a document onto the report row that records its send attempt."""
        raise NotImplementedError("a pipeline must say how a document is reported")

    def run(self) -> dict:
        """Run cycles until a bound is hit or a stop signal interrupts."""
        summary = None
        cycles = 0
        interrupted = False
        deadline = self._clock.monotonic() + self._timeout if self._timeout is not None else None
        try:
            while True:
                if self._max_cycles is not None and cycles >= self._max_cycles:
                    break
                if deadline is not None and self._clock.monotonic() >= deadline:
                    break
                summary = self.run_once()
                cycles += 1
                if self._once:
                    break
                if self._max_cycles is not None and cycles >= self._max_cycles:
                    break
                if deadline is not None and self._clock.monotonic() >= deadline:
                    break
                self._sleep(self._interval, deadline)
        except KeyboardInterrupt:
            interrupted = True
            log.info("interrupted; stopping after %d cycle(s)", cycles)
        result = summary or {"apps": [], "all_failed": False}
        result["interrupted"] = interrupted
        return result

    def run_once(self) -> dict:
        """Run a single cycle over every configured app, in order.

        Each app is isolated: an *unexpected* error from one tenant's source (a
        KeyError/TypeError/OSError that is none of the classified API errors)
        must not skip the tenants behind it and must not kill the poll loop, so
        it is logged with its traceback and reported as a failed app for this
        cycle. ``KeyboardInterrupt`` is not an ``Exception``, so it still
        propagates to the run() handler.

        This is the *outer* net only: ``_poll_app`` catches the same class of
        error itself once its result exists, so it can keep the counts the cycle
        accumulated; this handler is left for the failures that happen before
        that, which have nothing to preserve.
        """
        results = []
        # The budget is per cycle, not per process: a long-running daemon must
        # get a fresh allowance each cycle, otherwise it would stop sending
        # forever after the first cycle that used its budget.
        self._sends_this_cycle = 0
        # Apps whose last cycle deferred sends because of the cap. While an app
        # is listed here, _list_candidates pages past an all-seen page so the
        # older backlog the cap stranded stays reachable. Deliberately NOT
        # cleared here: _poll_app reads it to decide how deep to page, and
        # clearing it here would discard the previous cycle's signal before it
        # could be read. _poll_app owns the per-app lifecycle instead.
        for app in self._apps:
            try:
                results.append(self._poll_app(app))
            except Exception as exc:  # noqa: BLE001 - one tenant must not stop the rest
                log.exception("app %s: unexpected failure while polling", app.name)
                results.append(self._failed_app_result(app, exc))
        all_failed = bool(results) and all(not result["ok"] for result in results)
        return {"apps": results, "all_failed": all_failed}

    def _new_result(self, app: PollApp) -> dict:
        """The per-app summary skeleton, so a failed app reports the same keys
        as a healthy one (the CLI summary printer relies on them)."""
        return {
            "app": app.name,
            "kind": self.KIND,
            "ok": True,
            "error": None,
            "listed": 0,
            "new": 0,
            "sent": 0,
            "would_send": 0,
            "fallback_sends": 0,
            "skipped_no_phone": 0,
            "failed": 0,
            "pending": 0,
            "abandoned": 0,
            "deferred_by_cap": 0,
            "first_run": False,
            "seeded": 0,
            # Named for the invoice pipeline, which is the one that existed
            # first; ``kind`` is what says what the rows hold. Renaming it would
            # break every existing consumer for no gain.
            "invoices": [],
        }

    def _failed_app_result(self, app: PollApp, exc: Exception) -> dict:
        result = self._new_result(app)
        result["ok"] = False
        result["error"] = str(exc)
        return result

    def _poll_app(self, app: PollApp) -> dict:
        result = self._new_result(app)
        # ``first_run`` has to be decided *before* listing: the paging peek in
        # _list_candidates reads the state, and the store creates the app entry on
        # that read as a side effect. Computed after the listing, an app whose
        # first page comes back full would look like it already had state, skip
        # the seeding below, and then mass-send up to max_pages*limit historical
        # documents.
        first_run = not self._state.has_app(app.name)
        # This app's backlog is only still draining if *this* app deferred sends
        # last cycle, so the flag is reset here rather than globally.
        # Was *this* app still draining a capped backlog when the last cycle
        # ended? Read from the state store, not memory: production runs one
        # ``poll --once`` process per cron tick, so an in-memory flag is gone by
        # the time the next cycle needs it. The clear is deliberately deferred
        # until after a successful listing so a transient Daftra error does not
        # discard the signal and strand the backlog.
        draining = self._state.is_draining(app.name)
        try:
            listed = self._list_candidates(app, draining=draining)
        except _HANDLED_ERRORS as exc:
            log.error("app %s: listing %s failed: %s", app.name, self._plural, exc)
            result["ok"] = False
            result["error"] = str(exc)
            return result
        result["listed"] = len(listed)
        ids = [str(doc.id) for doc in listed]
        self._warn_if_source_went_quiet(app, listed)
        result["first_run"] = first_run
        result["dry_run"] = self._dry_run
        # Anything unexpected from here on (a failed batch-exit state write, an
        # OSError, a bug) is reported as a failed app, but the counts the cycle
        # had already accumulated are kept: a send that already went out must not
        # be reported as ``sent 0`` to the operator reading the summary.
        try:
            with self._state.batch():
                if first_run and not self._send_existing:
                    if self._dry_run:
                        log.info(
                            "app %s: first run; would seed %d existing %s(s) without "
                            "sending (dry-run; nothing was written)",
                            app.name, len(ids), self._noun,
                        )
                        result["seeded"] = len(ids)
                        return result
                    self._state.mark_many_seen(app.name, ids)
                    result["seeded"] = len(ids)
                    log.info(
                        "app %s: first run; seeded %d existing %s(s) without sending "
                        "(pass --send-existing to send them on the first run)",
                        app.name, len(ids), self._noun,
                    )
                    self._state.set_last_poll_at(app.name, self._clock.now())
                    return result
                for doc in listed:
                    document_id = str(doc.id)
                    if self._state.seen(app.name, document_id):
                        continue
                    if self._send_cap_reached():
                        # Deliberately left unseen: the cap is a throttle, not a
                        # decision about this document. A later cycle picks it up.
                        result["deferred_by_cap"] += 1
                        self._state.set_draining(app.name, True)
                        continue
                    if self._is_deferred(app.name, document_id):
                        result["pending"] += 1
                        result["invoices"].append(
                            {"number": self._label(doc), "id": document_id, "status": "pending", "to": None}
                        )
                        continue
                    result["new"] += 1
                    outcome = self._handle_document(app, document_id, doc)
                    result["invoices"].append(outcome)
                    if outcome["status"] == "sent":
                        if outcome.get("dry_run"):
                            result["would_send"] += 1
                        else:
                            result["sent"] += 1
                        if outcome.get("fallback"):
                            result["fallback_sends"] += 1
                    elif outcome["status"] == "skipped_no_phone":
                        result["skipped_no_phone"] += 1
                    elif outcome["status"] == "abandoned":
                        result["abandoned"] += 1
                    elif outcome["status"] == "deferred_by_cap":
                        # The cap bit *between* this document's recipients rather
                        # than between documents, so it got here having already
                        # counted as new. It is still unseen and a later cycle will
                        # finish it, which is exactly what the other cap-deferred
                        # path means — so it is counted there and taken back out of
                        # "new", rather than falling through to "failed" and
                        # implying a delivery problem that does not exist.
                        result["new"] -= 1
                        result["deferred_by_cap"] += 1
                    else:
                        result["failed"] += 1
                if not self._dry_run:
                    self._state.set_last_poll_at(app.name, self._clock.now())
        except Exception as exc:  # noqa: BLE001 - one tenant must not stop the rest
            log.exception("app %s: unexpected failure while polling", app.name)
            result["ok"] = False
            result["error"] = str(exc)
            return result
        log.info(
            "app %s: listed %d, new %d, sent %d, skipped_no_phone %d, failed %d, "
            "pending %d, abandoned %d, %d via free-form fallback",
            app.name, result["listed"], result["new"], result["sent"] + result["would_send"],
            result["skipped_no_phone"], result["failed"], result["pending"],
            result["abandoned"], result["fallback_sends"],
        )
        if result["deferred_by_cap"]:
            # Persist the draining signal for the next cycle. Written here, not
            # right after the listing, so a listing failure keeps it set. Skipped
            # on a dry run, which must not touch the state file at all.
            if not self._dry_run:
                self._state.set_draining(app.name, True)
            log.warning(
                "app %s: hit the per-run send cap of %s; %d %s(s) were left "
                "unseen for a later cycle (raise %s to send more per run)",
                app.name, self._max_sends_per_run, result["deferred_by_cap"],
                self._noun, self.CAP_ENV_VAR,
            )
        else:
            if not self._dry_run:
                self._state.set_draining(app.name, False)
        return result

    def _warn_if_source_went_quiet(self, app: PollApp, listed: list[TDoc]) -> None:
        """Report a listing that has gone suspiciously quiet for an established app.

        A quiet cycle is normal and must stay silent: an app with nothing new is the
        steady state, and a warning on every one of them would train the operator to
        ignore the log. What is *not* normal is a source offering **less of the
        account than it used to** — and that is a real failure here, not a
        hypothetical one. ``/invoice_payments.json`` excludes ``client_credit`` rows
        by default, and on a live tenant that was 124 of 125 payments: the pipeline
        spent weeks reporting ``listed 1, new 0`` with a zero exit code and nothing
        in the log to say the endpoint was showing it one old row out of a hundred
        and twenty-five.

        Two checks, because they catch different faults and each is blind to the
        other's case:

        - **The listing came back empty** for an app that has already handled
          documents. Every one of those documents exists, so an empty page is not a
          quiet day — the filter, the endpoint, or the account changed under us.
        - **The source's own count is below what we have already handled.** This is
          the one that catches a narrowing: the rows are unchanged from our point of
          view (a page of already-seen documents looks the same however many there
          are behind it), so only the source's ``total_results`` can tell that the
          account did not shrink. Read with ``getattr`` because a source that cannot
          report a total — the offline stub, a test double — has no opinion to give,
          and no opinion must never read as "none exist".
        """
        # ``seen_ids`` is on the port, so this works for every store; reading it
        # before the ``listed`` test below costs one list copy on a cycle that is
        # about to walk the seen-set anyway.
        handled = len(self._state.seen_ids(app.name))
        if not listed:
            if handled:
                log.warning(
                    "app %s: the %s listing came back empty although %d %s(s) have "
                    "already been handled; the source is offering none of them, so "
                    "new %s cannot be seen. Check the listing's filters and the "
                    "account, not the send path",
                    app.name, self._noun, handled, self._noun, self._plural,
                )
            return
        total = getattr(app.source, "last_listing_total", None)
        shortfall = handled - total if isinstance(total, int) else 0
        # The tolerance is load-bearing and was found in production, not reasoned
        # out in advance: Daftra's ``total_results`` does not agree with itself. The
        # same tenant, same filter, same rows answers ``126`` at ``limit=500`` and
        # ``125`` at ``limit=10``, so an exact ``total < handled`` comparison warns
        # every cycle on a perfectly healthy pipeline — which is precisely the
        # "warns so often it gets ignored" failure this check exists to avoid, and
        # it would have hidden the real fault it was written for. So the shortfall
        # has to be *material*: the exclusion this guards against dropped a listing
        # from 126 rows to 1, and nothing that small survives a 5% band.
        if shortfall >= max(2, handled // 20):
            log.warning(
                "app %s: the source reports %s %s(s) matching the listing but %d "
                "have already been handled; the listing has narrowed and newer %s "
                "may be invisible to this pipeline",
                app.name, total, self._noun, handled, self._plural,
            )

    def _list_candidates(self, app: PollApp, *, draining: bool = False) -> list[TDoc]:
        """List documents, paging forward while the page is saturated and still
        contains unseen documents.

        A burst of new documents between two cycles can otherwise push the older
        ones off the first page, where they would be silently missed. Paging
        stops as soon as a page is not full or its oldest row is already seen,
        and is bounded by ``max_pages`` so a pathological backlog cannot turn
        one cycle into a full-table scan.
        """
        candidates: list[TDoc] = []
        page = 1
        while True:
            listed = self._list_documents(app, limit=self._limit, page=page)
            if not listed:
                break
            candidates.extend(listed)
            if len(listed) < self._limit:
                break
            # Stop when this page holds nothing new *and* nothing new can be
            # waiting behind it.
            #
            # The listing is newest-first, so the usual steady-state signal is
            # "this page is full but its oldest row is already seen": the rows
            # behind it are older history that was handled in an earlier cycle.
            # A full page of seen rows therefore stops the walk.
            #
            # That inference breaks while a send cap is draining a backlog. A
            # capped cycle sends the *newest* documents and leaves older ones
            # unseen, so the next cycle can face a page that is entirely seen
            # while deeper pages are not — and stopping there would strand the
            # backlog forever: reachable, never paged to. That is the silent
            # loss this method exists to prevent, so while the previous cycle
            # deferred work we keep paging to reach it.
            if self._state.seen(app.name, str(listed[-1].id)) and not draining:
                # This read creates the app's state entry as a side effect, so
                # _poll_app has to capture ``first_run`` before listing (see the
                # note there): otherwise a saturated first page would make a
                # first run look like an established one.
                break
            page += 1
            if page > self._max_pages:
                log.warning(
                    "app %s: more than %d pages of unseen %s; the listing is "
                    "saturated and older %s may be missed — raise --limit",
                    app.name, self._max_pages, self._plural, self._plural,
                )
                break
        # A full page only means the listing is saturated if it still holds rows
        # this cycle has not handled: once every listed row is already seen, a
        # full page 1 is the steady state of a busy tenant and warning on it
        # every cycle buries the real saturation signal above.
        if len(candidates) >= self._limit and any(
            not self._state.seen(app.name, str(candidate.id)) for candidate in candidates
        ):
            log.warning(
                "app %s: the %s listing came back full (%d rows); if %s "
                "are created faster than the poll interval, raise --limit",
                app.name, self._noun, len(candidates), self._plural,
            )
        return candidates

    def _send_cap_reached(self) -> bool:
        """Whether this cycle has already attempted its budget of sends.

        ``max_sends_per_run <= 0`` disables the cap entirely, which is how an
        operator opts out after tuning. It must not be read as a budget of zero,
        or "disabled" would silently mean "never send".
        """
        return (
            self._max_sends_per_run is not None
            and self._max_sends_per_run > 0
            and self._sends_this_cycle >= self._max_sends_per_run
        )

    def _budget_send(self) -> None:
        """Claim one unit of the per-run send budget.

        Called immediately before each send POST, so a send that fails still
        consumes budget — the cap exists to bound requests to Meta, not to bound
        successes.
        """
        self._sends_this_cycle += 1

    def _recipients(self, app: PollApp, doc: TDoc) -> tuple[str, ...]:
        """The normalized, de-duplicated numbers this document must be sent to.

        A document is addressed to a *set* of numbers: Daftra keeps two phone
        fields on a client and either may be filled, so an invoice with both
        filled goes out twice and one with a single filled field goes out once.
        Order is preserved, so the most-preferred number is the first.

        Normalization is repeated here even though the adapters already do it,
        because this engine is written against a *port*: a source may hand over a
        raw ``010…`` value (the offline stub deliberately does) and Meta rejects
        an un-normalized number per recipient. Deduplication is repeated for the
        same reason — two fields holding the same number must not become two
        messages to one person.
        """
        recipients: list[str] = []
        seen: set[str] = set()
        for raw in doc.customer_phones:
            if not raw:
                continue
            try:
                phone = normalize_phone(raw, self._country_code)
            except ValueError:
                log.warning(
                    "app %s: %s %s has an unusable WhatsApp phone (%r); it is "
                    "skipped, and every remaining number still goes out",
                    app.name, self._noun, self._label(doc), raw,
                )
                continue
            if phone in seen:
                continue
            seen.add(phone)
            recipients.append(phone)
        return tuple(recipients)

    def _remember_delivered(self, app: PollApp, document_id: str, recipients: list[str]) -> None:
        """Persist the recipients reached, so a retry finishes only the rest.

        Written at the *first* thing that leaves work outstanding — a failed
        send, or the send cap biting between two recipients — and never on the
        happy path, where ``mark_seen`` retires the document and clears it. That
        keeps the common case at exactly one state write, which matters because
        this file is rewritten on every mark.

        Skipped on a dry run, which must leave the state file exactly as it found
        it (invariant 2).
        """
        if self._dry_run or not recipients:
            return
        self._state.record_delivered(app.name, document_id, recipients)

    def _handle_document(self, app: PollApp, document_id: str, candidate: TDoc) -> dict:
        # A listing row is only used as-is when the pipeline says it can answer
        # everything the message depends on; otherwise the detail fetch supplies
        # the rest. The send cap gates this, so the extra source traffic is
        # bounded by what is actually sent and never by the size of the listing.
        doc = candidate
        if self._needs_detail(doc):
            try:
                doc = self._fetch_document(app, document_id)
            except _HANDLED_ERRORS as exc:
                # Classified like any other fetch error, so a transient one
                # retries with backoff and a 4xx is abandoned for an operator.
                log.error(
                    "app %s: fetching %s %s failed: %s",
                    app.name, self._noun, self._label(candidate), exc,
                )
                return self._fail(
                    app, self._label(candidate), document_id, None, exc, "fetch"
                )
        self._after_detail_fetch(app, doc)
        recipients = self._recipients(app, doc)
        if not recipients:
            log.warning(
                "app %s: %s %s has no usable WhatsApp phone; skipping%s",
                app.name, self._noun, self._label(doc),
                "" if self._dry_run else " and marking seen",
            )
            if not self._dry_run:
                self._state.mark_seen(app.name, document_id)
            return {"number": self._label(doc), "id": document_id, "status": "skipped_no_phone", "to": None}
        label = self._label(doc)
        # A document half-delivered by an earlier cycle (a send failed, or the cap
        # ran out, after the first recipient had already been reached) resumes with
        # only the numbers that never got it. Skipping the delivered ones is the
        # whole point of tracking them: a customer must not be told about the same
        # invoice twice because the second number was briefly undeliverable.
        already = self._state.delivered(app.name, document_id)
        outstanding = [phone for phone in recipients if phone not in set(already)]
        if not outstanding:
            # Every number was reached on an earlier cycle; the only thing left was
            # to retire it. Marking seen here is what stops this running forever.
            log.info(
                "app %s: %s %s was already delivered to all of its numbers (%s); "
                "retiring it",
                app.name, self._noun, label, ", ".join(recipients),
            )
            if not self._dry_run:
                self._state.mark_seen(app.name, document_id)
            return {
                "number": label, "id": document_id, "status": "sent",
                "to": already[0] if already else None, "delivered": list(already),
                "dry_run": self._dry_run,
            }
        delivered: list[str] = []
        fallbacks: list[tuple[str, str]] = []
        for index, recipient in enumerate(outstanding):
            if self._send_cap_reached():
                # The cap is a throttle, not a decision about this document, so it
                # stays unseen and the numbers it did reach are remembered for the
                # cycle that finishes it. This is the one place the cap can land
                # *between* recipients rather than between documents.
                self._remember_delivered(app, document_id, already + delivered)
                log.info(
                    "app %s: per-run send cap reached after %d of %d recipients for "
                    "%s %s; deferring %s to a later cycle (raise %s)",
                    app.name, len(delivered), len(outstanding),
                    self._noun, label, ", ".join(outstanding[index:]), self.CAP_ENV_VAR,
                )
                return {
                    "number": label, "id": document_id, "status": "deferred_by_cap",
                    "to": delivered[0] if delivered else None,
                    "delivered": delivered, "remaining": outstanding[index:],
                }
            failure = self._deliver(app, doc, document_id, recipient, fallbacks)
            if failure is not None:
                # Only a *retryable* failure leaves the document coming back, so
                # only then is a delivered record worth keeping: it is what stops
                # the retry messaging a number that already got the document. A
                # permanent failure has already retired the document via
                # mark_seen, and writing a record after that would leave stale
                # state behind for a document nobody will send again.
                if failure.get("retryable"):
                    self._remember_delivered(app, document_id, already + delivered)
                failure["delivered"] = delivered
                return failure
            delivered.append(recipient)
        # Every number reached it, so the document is retired — which also clears
        # the delivered record, leaving nothing behind in the state file.
        if not self._dry_run:
            self._state.mark_seen(app.name, document_id)
        if len(delivered) > 1:
            log.info(
                "app %s: sent %s %s to %d numbers (%s)",
                app.name, self._noun, label, len(delivered), ", ".join(delivered),
            )
        else:
            log.info("app %s: sent %s %s to %s", app.name, self._noun, label, delivered[0])
        outcome = {
            "number": label, "id": document_id, "status": "sent",
            "to": delivered[0], "delivered": delivered, "dry_run": self._dry_run,
        }
        if fallbacks:
            # The summary counts *documents* delivered this way, not recipients.
            # The kind is reported only when every fallback agreed on it, since a
            # document half-served by the document bridge and half by plain text is
            # neither kind on its own.
            kinds = {kind for _, kind in fallbacks}
            outcome["fallback"] = True
            outcome["fallback_kind"] = kinds.pop() if len(kinds) == 1 else "mixed"
            outcome["error"] = "template rejected; delivered as a free-form message"
        return outcome

    def _deliver(
        self,
        app: PollApp,
        doc: TDoc,
        document_id: str,
        recipient: str,
        fallbacks: list[tuple[str, str]],
    ) -> dict | None:
        """Send the document to one recipient.

        Returns ``None`` when the message went out (or would have, on a dry run),
        or the failure outcome dict when it did not. The caller owns the
        document-level bookkeeping — the ``mark_seen`` that retires the document
        and the ``delivered`` record of who was reached — because only the caller
        can see whether *every* recipient was served.

        ``fallbacks`` collects ``(recipient, kind)`` pairs delivered by the
        free-form bridge, so the caller can label the document once it knows the
        whole picture.

        A failure for one number never cancels the others already reached: a
        document that reached one of its two numbers is delivered as far as it
        got, and the caller resumes the remainder on a later cycle.
        """
        try:
            payload = self._builder.build(doc, recipient)
        except _HANDLED_ERRORS as exc:
            log.error(
                "app %s: building the payload for %s %s to %s failed: %s",
                app.name, self._noun, self._label(doc), recipient, exc,
            )
            return self._fail(app, self._label(doc), document_id, recipient, exc, "build")
        if self._dry_run:
            log.info(
                "app %s: dry-run %s %s to %s", app.name, self._noun, self._label(doc), recipient
            )
            return None
        if self._sender is None:
            return self._fail(
                app, self._label(doc), document_id, recipient,
                RuntimeError("WhatsApp credentials are not configured; cannot send."), "send", doc,
            )
        try:
            # One unit of budget per POST, so a two-number document costs two and
            # the cap keeps bounding what actually reaches Meta.
            self._budget_send()
            response = self._sender.send(payload)
        except _HANDLED_ERRORS as exc:
            if self._is_template_failure(exc):
                if self._freeform_fallback:
                    return self._send_freeform_fallback(
                        app, doc, document_id, recipient, payload, exc, fallbacks
                    )
                log.error(
                    "app %s: %s %s template send failed with [code %s] and the "
                    "free-form fallback is disabled (WHATSAPP_FREEFORM_FALLBACK=off); the "
                    "%s is kept pending on the template error, because approving the "
                    "template is what makes the next attempt succeed",
                    app.name, self._noun, self._label(doc), _error_code(exc), self._noun,
                )
                return self._fail_retryable(app, self._label(doc), document_id, recipient, exc, "send", doc)
            return self._fail(app, self._label(doc), document_id, recipient, exc, "send", doc)
        log.info("app %s: sent %s %s to %s", app.name, self._noun, self._label(doc), recipient)
        self._record(app, doc, document_id, SENT, recipient=recipient, wamid=_wamid(response))
        return None

    def _is_template_failure(self, exc: Exception) -> bool:
        """Whether a failed send failed because the *template* is the problem.

        Only a Meta template-contract rejection (``132000``-series) qualifies: a
        free-form message does not use the template, so it can get the invoice
        out when the template is what Meta refused. Everything else keeps the
        classification it always had.
        """
        return isinstance(exc, WhatsAppApiError) and is_template_error(exc)

    def _fallback_payload(self, doc: TDoc, recipient: str, payload: dict) -> tuple[str, dict] | None:
        """The free-form message to send for an unusable template, and its kind.

        The document case reuses the media id the *failed* template send already
        paid for: the PDF is not rendered and not uploaded a second time, it is
        referenced by the id sitting in the payload we were about to send. Without
        a reusable header the text fallback is the existing free-form builder —
        the very payload ``send --freeform`` produces, so the Arabic body text is
        never spelled twice. ``None`` means no fallback can be built at all.
        """
        document = _header_document(payload)
        if document is not None:
            return "document", build_fallback_document(recipient, document)
        build_text = getattr(self._builder, "build_text", None)
        if build_text is None:
            return None
        return "text", build_text(doc, recipient)

    def _send_freeform_fallback(
        self,
        app: PollApp,
        doc: TDoc,
        document_id: str,
        recipient: str,
        payload: dict,
        exc: Exception,
        fallbacks: list[tuple[str, str]],
    ) -> dict | None:
        """Deliver a template-rejected document as a free-form message.

        This is a *bridge*, not a normal path: the template is unusable because
        the account has no approved translation of it in the language the sender
        asks for, and the owner wants the pipeline exercised end to end until it
        is approved. Two consequences are baked into the logging: the customer may
        not even receive the message (Meta only delivers free-form messages inside
        the 24-hour customer service window), and every send here is a message the
        approved template would have sent better.

        Failures here are recorded as retryable with the *template* error, never
        the fallback's: the template error is the one an operator can act on, and
        the document must come back once the template is fixed.

        Returns ``None`` when the fallback message went out — reaching this
        recipient, not finishing the document — and the failure outcome otherwise.
        The caller owns the ``mark_seen`` and the free-form flag, because with more
        than one recipient only the caller knows when the last one is done.

        ``fallbacks`` collects ``(recipient, kind)`` for each number that went out
        this way, because the *kind* (``document``/``text``) is known only here and
        the document-level outcome has to report it.
        """
        code = _error_code(exc)
        log.warning(
            "app %s: %s %s template send failed with [code %s] — the template is "
            "not usable (no approved translation for the configured language); "
            "falling back to a free-form message. Approve the template to stop this; "
            "note Meta only delivers free-form messages inside the 24-hour customer "
            "service window (131047 outside it)",
            app.name, self._noun, self._label(doc), code,
        )
        fallback = self._fallback_payload(doc, recipient, payload)
        if fallback is None:
            log.warning(
                "app %s: %s %s has no free-form fallback available (the builder "
                "has no build_text and the payload has no reusable header document)",
                app.name, self._noun, self._label(doc),
            )
            return self._fail_retryable(app, self._label(doc), document_id, recipient, exc, "send", doc)
        kind, message = fallback
        # A template-contract failure has already spent one unit of budget on the
        # rejected template POST, so the fallback would be the (cap+1)-th message
        # in front of Meta unless it is checked. Skipping it keeps the cap hard
        # and leaves the document pending on the template error, which is where it
        # already sits on this path.
        if self._send_cap_reached():
            log.info(
                "app %s: %s %s reached the per-run send cap after its template "
                "rejection; deferring the free-form fallback to a later cycle",
                app.name, self._noun, self._label(doc),
            )
            return self._fail_retryable(app, self._label(doc), document_id, recipient, exc, "send", doc)
        try:
            self._budget_send()
            response = self._sender.send(message)
        except _HANDLED_ERRORS as fallback_exc:
            log.error(
                "app %s: %s %s free-form %s fallback also failed: %s (the template "
                "error it replaces: %s); the %s stays pending on the template error",
                app.name, self._noun, self._label(doc), kind, fallback_exc, exc,
                self._noun,
            )
            return self._fail_retryable(app, self._label(doc), document_id, recipient, exc, "send", doc)
        # Deliberately no ``mark_seen`` here: the caller decides whether the
        # document is finished, and a document with a second number is not finished
        # because one of them got its message.
        log.warning(
            "app %s: delivered %s %s to %s as a free-form %s message after the "
            "template send failed with [code %s]. Approve the template; free-form only "
            "reaches the customer inside the 24-hour customer service window",
            app.name, self._noun, self._label(doc), recipient, kind, code,
        )
        self._record(
            app, doc, document_id, SENT,
            recipient=recipient, error=str(exc), fallback=True, wamid=_wamid(response),
        )
        fallbacks.append((recipient, kind))
        return None

    def _record(
        self,
        app: PollApp,
        doc: TDoc,
        document_id: str,
        status: str,
        *,
        recipient: str | None = None,
        error: str | None = None,
        fallback: bool = False,
        wamid: str | None = None,
    ) -> None:
        """Log one send attempt to the HTML report, if a recorder is attached.

        Only genuine attempts reach here: the dry-run branch returns before the
        send, ``skipped_no_phone`` returns without a send, and listing/build
        failures are filtered out by the ``action == "send"`` guard at the call
        sites. A dry run never records, matching the non-mutating contract that
        the state writes honour.
        """
        if self._recorder is None or self._dry_run:
            return
        self._recorder.record(
            self._make_outcome(
                app, doc, document_id, status,
                recipient=recipient,
                error=error,
                fallback=fallback,
                wamid=wamid,
            )
        )

    def _fail(self, app: PollApp, number: str, document_id: str, to: str | None, exc: Exception, action: str, doc: TDoc | None = None) -> dict:
        if _is_retryable(exc):
            return self._fail_retryable(app, number, document_id, to, exc, action, doc)
        return self._fail_permanent(app, number, document_id, to, exc, action, doc)

    def _fail_retryable(self, app: PollApp, number: str, invoice_id: str, to: str | None, exc: Exception, action: str, doc: TDoc | None = None) -> dict:
        previous = self._state.pending(app.name).get(invoice_id, {})
        count = int(previous.get("count", 0)) + 1
        backoff = self._backoff_for(count)
        next_attempt_at = self._clock.now() + backoff
        # A dry run must leave the state file exactly as it found it (the CLI
        # help and docs/design.md both promise a non-mutating --dry-run), and
        # `poll --dry-run` on a broken payload would otherwise leave a pending
        # record that the next real run honours as a backoff.
        if not self._dry_run:
            self._state.record_pending(
                app.name, invoice_id,
                error=str(exc), action=action, count=count, next_attempt_at=next_attempt_at,
            )
        log.error(
            "app %s: %s %s %s failed (retryable, attempt %d): %s; will retry in %gs",
            app.name, self._noun, number, action, count, exc, backoff,
        )
        if doc is not None and action == "send":
            self._record(app, doc, invoice_id, FAILED, recipient=to, error=str(exc))
        return {"number": number, "id": invoice_id, "status": "failed", "to": to, "error": str(exc), "retryable": True}

    def _fail_permanent(self, app: PollApp, number: str, invoice_id: str, to: str | None, exc: Exception, action: str, doc: TDoc | None = None) -> dict:
        previous = self._state.abandoned(app.name).get(invoice_id, {})
        count = int(previous.get("count", 0)) + 1
        # Guarded like the skipped_no_phone branches: a dry run reports the
        # outcome but must not mark the document seen or write it to abandoned,
        # or the rehearsal would silently retire it for real runs.
        if not self._dry_run:
            self._state.mark_seen(app.name, invoice_id)
            self._state.record_abandoned(
                app.name, invoice_id,
                error=str(exc), action=action, count=count,
            )
        log.error(
            "app %s: %s %s %s failed permanently (attempt %d): %s; giving up",
            app.name, self._noun, number, action, count, exc,
        )
        if doc is not None and action == "send":
            self._record(app, doc, invoice_id, ABANDONED, recipient=to, error=str(exc))
        return {"number": number, "id": invoice_id, "status": "abandoned", "to": to, "error": str(exc)}

    def _backoff_for(self, count: int) -> float:
        # The exponent is capped so a pathological attempt count cannot overflow:
        # 2 ** (count - 1) raises OverflowError from attempt 1025 on, and that
        # unhandled error inside a failure path would kill the whole poll loop
        # instead of just recording a (already long) wait.
        return min(self._interval * (2 ** min(count - 1, 64)), self._max_backoff)

    def _is_deferred(self, app_name: str, invoice_id: str) -> bool:
        entry = self._state.pending(app_name).get(invoice_id)
        if not entry:
            return False
        next_at = entry.get("next_attempt_at")
        if next_at is None:
            return False
        return self._clock.now() < next_at

    def _sleep(self, seconds: float, deadline: float | None) -> None:
        remaining = seconds
        if deadline is not None:
            remaining = min(remaining, max(0.0, deadline - self._clock.monotonic()))
        if remaining > 0:
            self._clock.sleep(remaining)


class InvoicePoller(DocumentPoller[Invoice, InvoiceSource]):
    """Announce new invoices under the ``aizen_invoice`` template.

    The production pipeline, and the one whose behaviour every invariant in
    :class:`DocumentPoller` was written for. It carries no policy of its own:
    what follows is only how an invoice is listed, fetched and reported.
    """

    KIND = KIND_INVOICE

    def _list_documents(self, app: PollApp, *, limit: int, page: int) -> list[Invoice]:
        return app.source.list_invoices(limit=limit, page=page)

    def _fetch_document(self, app: PollApp, document_id: str) -> Invoice:
        return app.source.get_invoice(document_id)

    def _needs_detail(self, invoice: Invoice) -> bool:
        """Whether this listing row is too thin to build the message from.

        Two things can be missing, and the second is the expensive one to discover
        late:

        - **No phone.** The row's ``Client`` may carry none, and nothing else knows
          where the invoice is going.
        - **No line items.** Daftra's ``/invoices.json`` does not embed
          ``InvoiceItem`` at all — only ``/invoices/{id}.json`` does — so *every*
          listed row comes back with an empty items tuple. That was harmless while
          the message was text only, and it stopped being harmless the moment the
          template grew a header document: the document is this sender's own
          rendered PDF, so an item-less row renders a PDF whose items table says the
          invoice has no products on it, and the customer is told their invoice is
          empty. The detail fetch is the only way to get the rows, and one request
          per *sent* invoice is a price worth paying for a document that is not a lie.
        """
        return not invoice.customer_phones or not invoice.items

    def _after_detail_fetch(self, app: PollApp, invoice: Invoice) -> None:
        if invoice.items:
            return
        # The detail says so too, so this is Daftra's own answer rather than a
        # fetch we failed to make. Send it anyway — an operator has to be able to
        # see the invoice — but say plainly what the customer will be told.
        log.warning(
            "app %s: invoice %s has no line items even on the detail fetch; the "
            "PDF sent with it will state that it has none",
            app.name, invoice.number,
        )

    def _make_outcome(
        self,
        app: PollApp,
        invoice: Invoice,
        document_id: str,
        status: str,
        *,
        recipient: str | None = None,
        error: str | None = None,
        fallback: bool = False,
        wamid: str | None = None,
    ) -> SendOutcome:
        return SendOutcome(
            app=app.name,
            invoice_id=document_id,
            invoice_number=invoice.number,
            customer_name=invoice.customer_name,
            customer_phone=recipient,
            currency=invoice.currency,
            total=invoice.total,
            issue_date=invoice.issue_date,
            status=status,
            attempted_at=self._clock.now(),
            error=error,
            fallback=fallback,
            wamid=wamid,
            kind=KIND_INVOICE,
        )


class PaymentPoller(DocumentPoller[Payment, PaymentSource]):
    """Announce recorded payments under the ``aizen_new_payment`` template.

    Structurally identical to the invoice pipeline — same state machine, same
    invariants, same failure classification — and deliberately so. The only real
    difference is in what has to be fetched: a Daftra payment record names no
    client and carries no phone, so reaching the customer means reading the
    linked invoice as well. That cost is paid here, in the adapter, and the
    engine above neither knows nor cares.
    """

    KIND = KIND_PAYMENT
    CAP_ENV_VAR = "POLL_PAYMENTS_MAX_SENDS_PER_RUN"

    def _list_documents(self, app: PollApp, *, limit: int, page: int) -> list[Payment]:
        return app.source.list_payments(limit=limit, page=page)

    def _fetch_document(self, app: PollApp, document_id: str) -> Payment:
        return app.source.get_payment(document_id)

    def _needs_detail(self, payment: Payment) -> bool:
        """Always: the listing row cannot address the customer at all.

        ``/invoice_payments.json`` returns the amount, the date, the reference
        code and the invoice id, but ``client_id`` is usually null and the payer
        contact fields are empty — the payer is only identified through the
        invoice it settles. A payment whose invoice cannot be read therefore
        arrives with no phone and is skipped, never guessed at.

        Asking the question anyway rather than hardcoding ``True`` keeps the
        intent readable next to the invoice rule it mirrors, and leaves room for
        a Daftra account whose payment rows do carry the payer.
        """
        return True

    def _make_outcome(
        self,
        app: PollApp,
        payment: Payment,
        document_id: str,
        status: str,
        *,
        recipient: str | None = None,
        error: str | None = None,
        fallback: bool = False,
        wamid: str | None = None,
    ) -> SendOutcome:
        return SendOutcome(
            app=app.name,
            invoice_id=document_id,
            invoice_number=payment.number,
            customer_name=payment.customer_name,
            customer_phone=recipient,
            currency=payment.currency,
            total=payment.amount,
            issue_date=payment.payment_date,
            status=status,
            attempted_at=self._clock.now(),
            error=error,
            fallback=fallback,
            wamid=wamid,
            kind=KIND_PAYMENT,
        )

class CustomerPoller(DocumentPoller[Customer, CustomerSource]):
    """Welcome newly created customers under the ``aizen_new_customer`` template.

    The third pipeline, and the smallest subclass of the three: it overrides five
    things and inherits everything that makes a notification pipeline safe. Every
    invariant above — seed-without-send on first run, dry-run writing nothing,
    retryable failures never abandoned, the send cap leaving work unseen rather
    than dropped — applies here unchanged, because it lives in
    :class:`DocumentPoller`, not in a per-document subclass.

    What makes it smaller than :class:`PaymentPoller` is that it needs **no
    detail-fetch policy of its own**. A Daftra client row carries the name and
    phone itself, so the inherited ``_needs_detail`` default ("no reachable
    phone") is exactly right: a normal listing row needs no second request, and
    only a row with a missing phone triggers one. The two-hop join that defines
    the payment pipeline has no analogue here.

    ``_label`` and the nouns are likewise inherited: ``Customer.number`` is the
    ``client_number`` an operator reads in the ERP, and the noun derives from
    ``KIND``.

    One requirement is pushed down into the adapter rather than enforced here:
    ``CustomerSource.list_customers`` must answer newest-first, because this
    engine walks pages forward and stops at the first seen record.
    """

    KIND = KIND_CUSTOMER
    CAP_ENV_VAR = "POLL_CUSTOMERS_MAX_SENDS_PER_RUN"

    def _list_documents(self, app: PollApp, *, limit: int, page: int) -> list[Customer]:
        return app.source.list_customers(limit=limit, page=page)

    def _fetch_document(self, app: PollApp, document_id: str) -> Customer:
        return app.source.get_customer(document_id)

    def _make_outcome(
        self,
        app: PollApp,
        customer: Customer,
        document_id: str,
        status: str,
        *,
        recipient: str | None = None,
        error: str | None = None,
        fallback: bool = False,
        wamid: str | None = None,
    ) -> SendOutcome:
        # ``invoice_id``/``invoice_number`` carry the customer id and client
        # number: the report's columns are named for the first pipeline, and every
        # field is optional on the model, so a customer row reuses them rather
        # than the report growing a parallel set. ``kind`` is what tells the two
        # apart when the page is read back.
        #
        # ``issue_date`` is the account creation date, not an invoice date — for a
        # welcome that is the more useful date anyway, since it is what makes a
        # "new customer" claim checkable after the fact.
        return SendOutcome(
            app=app.name,
            invoice_id=document_id,
            invoice_number=customer.number,
            customer_name=customer.customer_name,
            customer_phone=recipient,
            issue_date=customer.created,
            status=status,
            attempted_at=self._clock.now(),
            error=error,
            fallback=fallback,
            wamid=wamid,
            kind=KIND_CUSTOMER,
        )
