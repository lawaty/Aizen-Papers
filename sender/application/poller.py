"""Polling use case: watch Daftra for new invoices and notify customers.

The poller is deliberately thin: it lists invoices per app, decides what is new
via the injected PollStateStore, and sends each new invoice to the customer's
phone. All I/O (Daftra, WhatsApp, the state file, sleeping) arrives through
ports so the whole loop is testable offline.

Failure policy (see docs/design.md §8):

- **Retryable** failures (network/timeout, HTTP 429, HTTP 5xx) are never given
  up. They are recorded in the state as ``pending`` with a bounded exponential
  backoff and retried on later cycles; a retryable failure can never become a
  silent permanent loss.
- **Permanent** failures (validation, missing ``public_url``, template
  rejection, self-send, any other non-transient error) are given up on the
  first attempt: the invoice is marked seen and recorded in the state as
  ``abandoned`` so an operator can see it and re-drive it.
- **Template contract failures** (the ``132000``-series) are the one exception
  to that split, because they are fixable by an operator rather than by waiting:
  a ``132001`` (no usable ``aizen_invoice`` translation) retried unchanged would
  fail identically forever, so the poller falls back to a free-form message when
  ``freeform_fallback`` is on (see :meth:`InvoicePoller._send_freeform_fallback`)
  and records the invoice as ``pending`` with the *template* error when it is
  off or when the fallback send fails too. It is never ``abandoned`` and never
  marked seen on that path — a bridge must not consume invoices.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

from sender.domain.errors import ApiError, WhatsAppApiError, WhatsAppSelfSendError, is_template_error
from sender.domain.models import Invoice
from sender.domain.phones import normalize_phone
from sender.domain.ports import Clock, InvoiceSource, MessageSender, PollStateStore
from sender.domain.templates import InvoiceTemplateBuilder, build_fallback_document

log = logging.getLogger(__name__)

_HANDLED_ERRORS = (ApiError, ValueError, RuntimeError)


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


def _is_retryable(exc: Exception) -> bool:
    """Classify a handled error as transient (retry) or permanent (give up).

    Network/timeout errors carry ``status=None``; HTTP 429 and 5xx are the
    transient classes. Everything else (4xx, validation, template rejection,
    self-send) can never succeed on retry.
    """
    if isinstance(exc, WhatsAppSelfSendError):
        return False
    if isinstance(exc, ApiError):
        status = exc.status
        if status is None:
            return True
        if status == 429 or (500 <= status < 600):
            return True
    return False


@dataclass(frozen=True)
class PollApp:
    name: str
    source: InvoiceSource


class InvoicePoller:
    def __init__(
        self,
        apps: Sequence[PollApp],
        sender: MessageSender | None,
        builder: InvoiceTemplateBuilder,
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
        for app in self._apps:
            try:
                results.append(self._poll_app(app))
            except Exception as exc:  # noqa: BLE001 - one tenant must not stop the rest
                log.exception("app %s: unexpected failure while polling", app.name)
                results.append(self._failed_app_result(app, exc))
        all_failed = bool(results) and all(not result["ok"] for result in results)
        return {"apps": results, "all_failed": all_failed}

    @staticmethod
    def _new_result(app: PollApp) -> dict:
        """The per-app summary skeleton, so a failed app reports the same keys
        as a healthy one (the CLI summary printer relies on them)."""
        return {
            "app": app.name,
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
            "first_run": False,
            "seeded": 0,
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
        # invoices.
        first_run = not self._state.has_app(app.name)
        try:
            listed = self._list_candidates(app)
        except _HANDLED_ERRORS as exc:
            log.error("app %s: listing invoices failed: %s", app.name, exc)
            result["ok"] = False
            result["error"] = str(exc)
            return result
        result["listed"] = len(listed)
        ids = [str(invoice.id) for invoice in listed]
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
                            "app %s: first run; would seed %d existing invoice(s) without "
                            "sending (dry-run; nothing was written)",
                            app.name, len(ids),
                        )
                        result["seeded"] = len(ids)
                        return result
                    self._state.mark_many_seen(app.name, ids)
                    result["seeded"] = len(ids)
                    log.info(
                        "app %s: first run; seeded %d existing invoice(s) without sending "
                        "(pass --send-existing to send them on the first run)",
                        app.name, len(ids),
                    )
                    self._state.set_last_poll_at(app.name, self._clock.now())
                    return result
                for invoice in listed:
                    invoice_id = str(invoice.id)
                    if self._state.seen(app.name, invoice_id):
                        continue
                    if self._is_deferred(app.name, invoice_id):
                        result["pending"] += 1
                        result["invoices"].append(
                            {"number": invoice.number, "id": invoice_id, "status": "pending", "to": None}
                        )
                        continue
                    result["new"] += 1
                    outcome = self._handle_invoice(app, invoice_id, invoice)
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
        return result

    def _list_candidates(self, app: PollApp) -> list[Invoice]:
        """List invoices, paging forward while the page is saturated and still
        contains unseen invoices.

        A burst of new invoices between two cycles can otherwise push the older
        ones off the first page, where they would be silently missed. Paging
        stops as soon as a page is not full or its oldest row is already seen,
        and is bounded by ``max_pages`` so a pathological backlog cannot turn
        one cycle into a full-table scan.
        """
        candidates: list[Invoice] = []
        page = 1
        while True:
            listed = app.source.list_invoices(limit=self._limit, page=page)
            if not listed:
                break
            candidates.extend(listed)
            if len(listed) < self._limit:
                break
            if self._state.seen(app.name, str(listed[-1].id)):
                # This read creates the app's state entry as a side effect, so
                # _poll_app has to capture ``first_run`` before listing (see the
                # note there): otherwise a saturated first page would make a
                # first run look like an established one.
                break
            page += 1
            if page > self._max_pages:
                log.warning(
                    "app %s: more than %d pages of unseen invoices; the listing is "
                    "saturated and older invoices may be missed — raise --limit",
                    app.name, self._max_pages,
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
                "app %s: the invoice listing came back full (%d rows); if invoices "
                "are created faster than the poll interval, raise --limit",
                app.name, len(candidates),
            )
        return candidates

    def _handle_invoice(self, app: PollApp, invoice_id: str, candidate: Invoice) -> dict:
        # The list row already carries everything the template builders need
        # (number, date, totals, public_url, Client with phone1/phone2), so the
        # only reason to fetch the detail is a missing phone. The template
        # payload is identical either way; see docs/layers/application.md.
        invoice = candidate
        if not invoice.customer_phone:
            try:
                invoice = app.source.get_invoice(invoice_id)
            except _HANDLED_ERRORS as exc:
                log.error("app %s: fetching invoice %s failed: %s", app.name, invoice.number, exc)
                return self._fail(app, invoice.number, invoice_id, None, exc, "fetch")
        recipient = invoice.customer_phone
        if not recipient:
            log.warning(
                "app %s: invoice %s has no usable WhatsApp phone; skipping%s",
                app.name, invoice.number,
                "" if self._dry_run else " and marking seen",
            )
            if not self._dry_run:
                self._state.mark_seen(app.name, invoice_id)
            return {"number": invoice.number, "id": invoice_id, "status": "skipped_no_phone", "to": None}
        try:
            recipient = normalize_phone(recipient, self._country_code)
        except ValueError:
            log.warning(
                "app %s: invoice %s has an unusable WhatsApp phone (%r); skipping%s",
                app.name, invoice.number, invoice.customer_phone,
                "" if self._dry_run else " and marking seen",
            )
            if not self._dry_run:
                self._state.mark_seen(app.name, invoice_id)
            return {"number": invoice.number, "id": invoice_id, "status": "skipped_no_phone", "to": None}
        try:
            payload = self._builder.build(invoice, recipient)
        except _HANDLED_ERRORS as exc:
            log.error("app %s: building the payload for invoice %s failed: %s", app.name, invoice.number, exc)
            return self._fail(app, invoice.number, invoice_id, recipient, exc, "build")
        if self._dry_run:
            log.info("app %s: dry-run invoice %s to %s", app.name, invoice.number, recipient)
            return {"number": invoice.number, "id": invoice_id, "status": "sent", "to": recipient, "dry_run": True}
        if self._sender is None:
            return self._fail(
                app, invoice.number, invoice_id, recipient,
                RuntimeError("WhatsApp credentials are not configured; cannot send."), "send",
            )
        try:
            self._sender.send(payload)
        except _HANDLED_ERRORS as exc:
            if self._is_template_failure(exc):
                if self._freeform_fallback:
                    return self._send_freeform_fallback(app, invoice, invoice_id, recipient, payload, exc)
                log.error(
                    "app %s: invoice %s template send failed with [code %s] and the "
                    "free-form fallback is disabled (WHATSAPP_FREEFORM_FALLBACK=off); the "
                    "invoice is kept pending on the template error, because approving the "
                    "template is what makes the next attempt succeed",
                    app.name, invoice.number, _error_code(exc),
                )
                return self._fail_retryable(app, invoice.number, invoice_id, recipient, exc, "send")
            return self._fail(app, invoice.number, invoice_id, recipient, exc, "send")
        self._state.mark_seen(app.name, invoice_id)
        log.info("app %s: sent invoice %s to %s", app.name, invoice.number, recipient)
        return {"number": invoice.number, "id": invoice_id, "status": "sent", "to": recipient}

    def _is_template_failure(self, exc: Exception) -> bool:
        """Whether a failed send failed because the *template* is the problem.

        Only a Meta template-contract rejection (``132000``-series) qualifies: a
        free-form message does not use the template, so it can get the invoice
        out when the template is what Meta refused. Everything else keeps the
        classification it always had.
        """
        return isinstance(exc, WhatsAppApiError) and is_template_error(exc)

    def _fallback_payload(self, invoice: Invoice, recipient: str, payload: dict) -> tuple[str, dict] | None:
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
        return "text", build_text(invoice, recipient)

    def _send_freeform_fallback(
        self,
        app: PollApp,
        invoice: Invoice,
        invoice_id: str,
        recipient: str,
        payload: dict,
        exc: Exception,
    ) -> dict:
        """Deliver a template-rejected invoice as a free-form message.

        This is a *bridge*, not a normal path: the template is unusable because
        the account has no approved ``aizen_invoice`` translation in the language
        the sender asks for, and the owner wants the pipeline exercised end to
        end until it is approved. Two consequences are baked into the logging:
        the customer may not even receive the message (Meta only delivers
        free-form messages inside the 24-hour customer service window), and every
        send here is a message the approved template would have sent better.

        Failures here are recorded as retryable with the *template* error, never
        the fallback's: the template error is the one an operator can act on, and
        the invoice must come back once the template is fixed.
        """
        code = _error_code(exc)
        log.warning(
            "app %s: invoice %s template send failed with [code %s] — the template is "
            "not usable (no approved translation for the configured language); "
            "falling back to a free-form message. Approve the template to stop this; "
            "note Meta only delivers free-form messages inside the 24-hour customer "
            "service window (131047 outside it)",
            app.name, invoice.number, code,
        )
        fallback = self._fallback_payload(invoice, recipient, payload)
        if fallback is None:
            log.warning(
                "app %s: invoice %s has no free-form fallback available (the builder "
                "has no build_text and the payload has no reusable header document)",
                app.name, invoice.number,
            )
            return self._fail_retryable(app, invoice.number, invoice_id, recipient, exc, "send")
        kind, message = fallback
        try:
            self._sender.send(message)
        except _HANDLED_ERRORS as fallback_exc:
            log.error(
                "app %s: invoice %s free-form %s fallback also failed: %s (the template "
                "error it replaces: %s); the invoice stays pending on the template error",
                app.name, invoice.number, kind, fallback_exc, exc,
            )
            return self._fail_retryable(app, invoice.number, invoice_id, recipient, exc, "send")
        self._state.mark_seen(app.name, invoice_id)
        log.warning(
            "app %s: delivered invoice %s to %s as a free-form %s message after the "
            "template send failed with [code %s]. Approve the template; free-form only "
            "reaches the customer inside the 24-hour customer service window",
            app.name, invoice.number, recipient, kind, code,
        )
        return {
            "number": invoice.number,
            "id": invoice_id,
            "status": "sent",
            "to": recipient,
            "fallback": True,
            "fallback_kind": kind,
            "error": str(exc),
        }

    def _fail(self, app: PollApp, number: str, invoice_id: str, to: str | None, exc: Exception, action: str) -> dict:
        if _is_retryable(exc):
            return self._fail_retryable(app, number, invoice_id, to, exc, action)
        return self._fail_permanent(app, number, invoice_id, to, exc, action)

    def _fail_retryable(self, app: PollApp, number: str, invoice_id: str, to: str | None, exc: Exception, action: str) -> dict:
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
            "app %s: invoice %s %s failed (retryable, attempt %d): %s; will retry in %gs",
            app.name, number, action, count, exc, backoff,
        )
        return {"number": number, "id": invoice_id, "status": "failed", "to": to, "error": str(exc), "retryable": True}

    def _fail_permanent(self, app: PollApp, number: str, invoice_id: str, to: str | None, exc: Exception, action: str) -> dict:
        previous = self._state.abandoned(app.name).get(invoice_id, {})
        count = int(previous.get("count", 0)) + 1
        # Guarded like the skipped_no_phone branches: a dry run reports the
        # outcome but must not mark the invoice seen or write it to abandoned,
        # or the rehearsal would silently retire the invoice for real runs.
        if not self._dry_run:
            self._state.mark_seen(app.name, invoice_id)
            self._state.record_abandoned(
                app.name, invoice_id,
                error=str(exc), action=action, count=count,
            )
        log.error(
            "app %s: invoice %s %s failed permanently (attempt %d): %s; giving up",
            app.name, number, action, count, exc,
        )
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