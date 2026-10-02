from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

from dotenv import load_dotenv

from sender.infrastructure.config import (
    ATTACH_LINK,
    ATTACH_NONE,
    ATTACH_UPLOAD,
    ATTACHMENT_MODES,
    Settings,
)
from sender.infrastructure.daftra.client import DaftraClient
from sender.infrastructure.whatsapp.client import WhatsAppClient
from sender.infrastructure.whatsapp.media import MetaMediaUploader
from sender.infrastructure.whatsapp.template_registry import NEW, TemplateRegistry
from sender.domain.attachments import HostedLinkProvider, NoAttachmentProvider
from sender.domain.errors import ApiError, WhatsAppApiError, is_template_error
from sender.domain.ports import InvoiceAttachmentProvider
from sender.domain.templates import (
    CleanTextTemplateBuilder,
    InvoiceTemplateBuilder,
    LegacyInvoiceTemplateBuilder,
)
from sender.application.poller import InvoicePoller, PollApp
from sender.application.services import InvoiceNotificationService
from sender.infrastructure.clock import SystemClock
from sender.infrastructure.state import JsonPollStateStore, PollStateLock
from sender.infrastructure.attachments import UploadedMediaProvider
from sender.infrastructure.reporting import JsonlSendOutcomeRecorder, ReportStore
from sender.presentation.stubs import StubInvoiceSource, default_stub_invoices


def _template_registry(settings: Settings) -> TemplateRegistry:
    return TemplateRegistry(
        access_token=settings.wa_access_token,
        waba_id=settings.wa_waba_id,
        api_version=settings.wa_api_version,
        template_name=settings.wa_template_name,
        language=settings.wa_template_lang,
        timeout=settings.wa_timeout,
        cache_path=settings.wa_template_cache,
        ttl_seconds=settings.wa_template_ttl,
    )


def _attachment_provider(
    settings: Settings, mode: str | None = None, session: object | None = None
) -> InvoiceAttachmentProvider | None:
    """Build the invoice-PDF attachment provider for *mode*.

    ``upload`` (the default) renders the PDF and pushes it to Meta, so it needs
    the same credentials the sender has. ``link`` is pure domain — no
    credentials, no network. ``none`` returns a provider that never produces a
    document, which drops the header component; it is deliberately *not* the same
    as passing no provider at all, which keeps the historic link behaviour.
    """
    resolved = (mode or settings.invoice_attachment or ATTACH_UPLOAD).strip().lower()
    if resolved == ATTACH_NONE:
        return NoAttachmentProvider()
    if resolved == ATTACH_LINK:
        return HostedLinkProvider(caption=settings.invoice_attachment_caption)
    if resolved != ATTACH_UPLOAD:
        raise RuntimeError(
            f"Unknown attachment mode {resolved!r}; use one of "
            f"{', '.join(ATTACHMENT_MODES)}"
        )
    logging.info(
        "invoice attachment mode is %s: whenever the active template builder attaches "
        "documents, a PDF is rendered per invoice and uploaded to Meta to obtain a "
        "media id (this happens even for preview and --dry-run, because the payload "
        "has to carry the real id). The clean text template attaches no document, so "
        "nothing is rendered and nothing is uploaded there",
        ATTACH_UPLOAD,
    )
    return UploadedMediaProvider(
        uploader=MetaMediaUploader(
            access_token=settings.wa_access_token,
            phone_number_id=settings.wa_phone_number_id,
            api_version=settings.wa_api_version,
            # An upload moves a whole file, so give it more headroom than a
            # send, and reuse the configured timeout as the base.
            timeout=max(60.0, settings.wa_timeout),
            session=session,
        ),
        caption=settings.invoice_attachment_caption,
    )


def _builder_for_mode(
    mode: str,
    settings: Settings,
    attachment: InvoiceAttachmentProvider | None = None,
) -> InvoiceTemplateBuilder:
    """Construct a concrete builder without any network (used by --stub and by
    commands that never build a message)."""
    name = settings.wa_template_name
    lang = settings.wa_template_lang
    country = settings.default_country_code
    if mode == "new":
        return CleanTextTemplateBuilder(name, lang, country)
    return LegacyInvoiceTemplateBuilder(name, lang, country, attachment=attachment)


def _resolve_builder(
    settings: Settings,
    override: str | None = None,
    attachment: InvoiceAttachmentProvider | None = None,
) -> InvoiceTemplateBuilder:
    name = settings.wa_template_name
    lang = settings.wa_template_lang
    country = settings.default_country_code
    mode = (override or settings.wa_template_builder or "auto").strip().lower()
    if mode == "legacy":
        return _builder_for_mode("legacy", settings, attachment)
    if mode == "new":
        return _builder_for_mode("new", settings, attachment)
    if not settings.wa_waba_id:
        logging.warning("WHATSAPP_WABA_ID is not set; using the legacy template builder")
        return _builder_for_mode("legacy", settings, attachment)
    registry = _template_registry(settings)
    return registry.choose_builder(name, lang, country, attachment)


def _stub_source(settings: Settings) -> StubInvoiceSource:
    """The offline stub source: the persistent fixture when it exists, otherwise
    the built-in sample invoices (without creating the fixture file)."""
    path = Path(settings.stub_invoices_path)
    if path.exists():
        return StubInvoiceSource(path=str(path))
    return StubInvoiceSource(default_stub_invoices())


def build_service(
    settings: Settings,
    with_whatsapp: bool = True,
    use_stub: bool = False,
    builder_override: str | None = None,
    resolve_builder: bool = True,
    attachment_mode: str | None = None,
    session: object | None = None,
) -> InvoiceNotificationService:
    whatsapp = (
        WhatsAppClient(
            access_token=settings.wa_access_token,
            phone_number_id=settings.wa_phone_number_id,
            api_version=settings.wa_api_version,
            timeout=settings.wa_timeout,
            default_country_code=settings.default_country_code,
            own_number=settings.wa_own_number or None,
            max_retry_wait=settings.wa_max_retry_wait,
            session=session,
        )
        if with_whatsapp
        else None
    )
    if use_stub:
        source = _stub_source(settings)
    else:
        app = settings.primary_app
        source = DaftraClient(
            api_key=app.api_key,
            base_url=app.base_url,
            timeout=app.timeout,
            country_code=settings.default_country_code,
        )
    if use_stub:
        # Stub mode must stay offline: never resolve via the Graph API, and do
        # not push a PDF to Meta on a `--stub` run unless asked to by name.
        attachment_mode = _attachment_mode_for(settings, attachment_mode, use_stub=True)
        if attachment_mode == ATTACH_UPLOAD:
            logging.warning(
                "--stub with an explicit %s attachment uploads a real PDF to Meta; "
                "use --attachment link for a fully offline run",
                ATTACH_UPLOAD,
            )
        builder = _builder_for_mode(
            builder_override if builder_override in ("legacy", "new") else "legacy",
            settings,
            _attachment_provider(settings, attachment_mode, session),
        )
    elif resolve_builder:
        builder = _resolve_builder(
            settings,
            builder_override,
            _attachment_provider(settings, attachment_mode, session),
        )
    else:
        # show/list never build a message; skip the (possibly network) resolution.
        builder = _builder_for_mode("legacy", settings)
    return InvoiceNotificationService(
        source=source,
        sender=whatsapp,
        builder=builder,
    )


def _is_param_mismatch(error: Exception) -> bool:
    # The predicate is a domain rule (which failures are about the template), so
    # the poller asks the same question and the two can never disagree.
    return is_template_error(error)


def _other_builder(
    current: InvoiceTemplateBuilder, settings: Settings
) -> InvoiceTemplateBuilder:
    name = settings.wa_template_name
    lang = settings.wa_template_lang
    country = settings.default_country_code
    # Carry the attachment across the swap: the retry has to produce the same
    # kind of header document the first attempt did.
    attachment = getattr(current, "attachment", None)
    if isinstance(current, LegacyInvoiceTemplateBuilder):
        return CleanTextTemplateBuilder(name, lang, country)
    return LegacyInvoiceTemplateBuilder(name, lang, country, attachment=attachment)


def _is_auto_builder(args: argparse.Namespace, settings: Settings) -> bool:
    # Normalized exactly like _resolve_builder and config._env_builder: "AUTO"
    # from the environment is the same request as "auto", and comparing it raw
    # silently skipped the builder-retry path.
    mode = (getattr(args, "builder", None) or settings.wa_template_builder or "auto").strip().lower()
    return mode == "auto"


def _send_template_with_retry(
    service: InvoiceNotificationService, args: argparse.Namespace, settings: Settings
) -> dict:
    try:
        return service.send_invoice(args.invoice_id, to_phone=args.to)
    except WhatsAppApiError as exc:
        if not _is_param_mismatch(exc):
            raise
        code = (exc.payload.get("error") or {}).get("code")
        logging.info(
            "template send rejected with code %s (message not sent); refreshing the "
            "template state and retrying once with the other builder",
            code,
        )
        _template_registry(settings).current(force=True)
        current = service.builder
        if not isinstance(current, (LegacyInvoiceTemplateBuilder, CleanTextTemplateBuilder)):
            raise
        alternate = _other_builder(current, settings)
        if isinstance(alternate, current.__class__):
            raise
        service.swap_builder(alternate)
        return service.send_invoice(args.invoice_id, to_phone=args.to)


def _add_attachment_argument(parser: argparse.ArgumentParser, note: str = "") -> None:
    parser.add_argument(
        "--attachment",
        choices=ATTACHMENT_MODES,
        default=None,
        help=(
            "How the invoice PDF is attached: upload renders the PDF and uploads it "
            "to Meta (default), link points Meta at a publicly reachable PDF, none "
            "sends without an attachment. Overrides INVOICE_ATTACHMENT."
            + (f" Note: {note}." if note else "")
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sender",
        description="Send Daftra invoice details via WhatsApp template messages",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    send = sub.add_parser("send", help="Fetch an invoice and send it on WhatsApp")
    send.add_argument("--invoice-id", required=True, help="Daftra invoice id")
    send.add_argument("--to", help="Recipient phone number (defaults to the customer phone on the invoice)")
    send.add_argument("--dry-run", action="store_true", help="Print the payload without calling Meta")
    send.add_argument("--freeform", action="store_true", help="Send as plain text instead of template (for layout testing)")
    send.add_argument("--stub", action="store_true", help="Use the offline stub invoice source instead of Daftra")
    send.add_argument("--builder", choices=("auto", "legacy", "new"), default=None, help="Template builder: auto resolves via template status (default), legacy/new pin one")
    _add_attachment_argument(send)

    preview = sub.add_parser("preview", help="Print the WhatsApp payload without sending")
    preview.add_argument("--invoice-id", required=True, help="Daftra invoice id")
    preview.add_argument("--to", help="Recipient phone number (defaults to the customer phone on the invoice)")
    preview.add_argument("--freeform", action="store_true", help="Preview as plain text instead of template")
    preview.add_argument("--stub", action="store_true", help="Use the offline stub invoice source instead of Daftra")
    preview.add_argument("--builder", choices=("auto", "legacy", "new"), default=None, help="Template builder: auto resolves via template status (default), legacy/new pin one")
    _add_attachment_argument(preview, note="upload resolves a real media id (and so uploads the PDF) but never sends")

    show = sub.add_parser("show", help="Print a normalized invoice (use --raw for the Daftra JSON)")
    show.add_argument("--invoice-id", required=True, help="Daftra invoice id")
    show.add_argument("--raw", action="store_true", help="Print the raw Daftra JSON response")
    show.add_argument("--stub", action="store_true", help="Use the offline stub invoice source instead of Daftra")

    listing = sub.add_parser("list", help="List recent invoices")
    listing.add_argument("--limit", type=int, default=10, help="Number of invoices to list")
    listing.add_argument("--stub", action="store_true", help="Use the offline stub invoice source instead of Daftra")

    poll = sub.add_parser("poll", help="Poll Daftra for new invoices and send them automatically")
    poll.add_argument("--once", action="store_true", help="Run exactly one cycle and exit")
    poll.add_argument("--interval", type=float, default=None, help="Seconds between cycles (default: POLL_INTERVAL env, 60)")
    poll.add_argument("--limit", type=int, default=None, help="List page size (default: POLL_LIMIT env, 10)")
    poll.add_argument("--max-cycles", type=int, default=None, help="Stop after this many cycles")
    poll.add_argument("--max-sends", type=int, default=None, help="Cap send attempts per cycle across all apps (default: POLL_MAX_SENDS_PER_RUN env, 10; 0 disables)")
    poll.add_argument("--timeout", type=float, default=None, help="Stop after this many seconds")
    poll.add_argument("--dry-run", action="store_true", help="Build payloads without sending and without touching the poll state")
    poll.add_argument("--send-existing", action="store_true", help="On the first run, send the currently existing invoices instead of seeding them as seen")
    poll.add_argument("--stub", action="store_true", help="Use the offline stub invoice source instead of Daftra")
    poll.add_argument("--builder", choices=("auto", "legacy", "new"), default=None, help="Template builder: auto resolves via template status (default), legacy/new pin one")
    _add_attachment_argument(poll, note="poll --dry-run does not upload unless the mode is upload")

    poll_status = sub.add_parser("poll-status", help="Show the poll state (seen/pending/abandoned) per app")
    poll_status.add_argument("--stub", action="store_true", help="Inspect the offline stub poll state instead of the real one")

    poll_reset = sub.add_parser("poll-reset", help="Reset poll state for an app (requires --yes)")
    poll_reset.add_argument("--app", required=True, help="App name to reset")
    poll_reset.add_argument("--invoice-id", default=None, help="Reset only this invoice's records instead of the whole app")
    poll_reset.add_argument("--yes", action="store_true", help="Confirm the reset")
    poll_reset.add_argument("--stub", action="store_true", help="Reset the offline stub poll state instead of the real one")

    stub_add = sub.add_parser("stub-add", help="Add a new invoice to the offline stub fixture")

    report = sub.add_parser("report", help="Regenerate the HTML pages of sent messages from the send log")
    report.add_argument("--date", default=None, help="Regenerate only this YYYY-MM-DD page (default: every logged date)")
    report.add_argument("--stub", action="store_true", help="Use the offline stub report directory instead of the real one")

    webhook = sub.add_parser("webhook-serve", help="Run the delivery-status webhook receiver")
    webhook.add_argument("--host", default="0.0.0.0", help="Bind host")
    webhook.add_argument("--port", type=int, default=8080, help="Bind port")
    webhook.add_argument("--events-file", default="webhook_events.jsonl", help="JSONL file to append status events to")

    subscribe = sub.add_parser("webhook-subscribe", help="Subscribe this app to WhatsApp webhook events")

    template_status = sub.add_parser("template-status", help="Print the cached/current template state and the active builder")
    template_status.add_argument("--force", action="store_true", help="Re-fetch the template from the Graph API instead of the cache")

    template_watch = sub.add_parser("template-watch", help="Poll until the reviewed template is approved, then exit 0 (exit 1 on timeout)")
    template_watch.add_argument("--interval", type=float, default=60.0, help="Poll delay in seconds (default 60)")
    template_watch.add_argument("--timeout", type=float, default=3600.0, help="Give up after this many seconds (default 3600)")

    template_drop = sub.add_parser("template-drop-legacy", help="Confirm the legacy builder is deprecated and print the removal checklist")
    template_drop.add_argument("--force", action="store_true", help="Print the checklist even if the clean template is not approved yet")

    return parser


def _run_template_command(args: argparse.Namespace, settings: Settings) -> int:
    registry = _template_registry(settings)
    if args.command == "template-status":
        snapshot = registry.refresh() if args.force else registry.current()
        print(json.dumps(snapshot, indent=2))
        return 0
    if args.command == "template-watch":
        return _watch_template(registry, args.interval, args.timeout)
    if args.command == "template-drop-legacy":
        return _drop_legacy(registry, args.force)
    raise RuntimeError(f"unhandled template command {args.command!r}")


def _watch_template(registry: TemplateRegistry, interval: float, timeout: float) -> int:
    import time

    if interval <= 0:
        raise RuntimeError(f"template-watch --interval must be > 0, got {interval:g}")
    deadline = time.monotonic() + timeout
    while True:
        snapshot = registry.current(force=True)
        print(
            f"[template-watch] status={snapshot.get('status')!r} "
            f"active_builder={snapshot.get('active_builder')}",
            flush=True,
        )
        if snapshot.get("active_builder") == NEW:
            print(
                f"Template {snapshot['template']} approved with the new structure; "
                "the clean builder is active and the legacy builder is deprecated"
            )
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(
                f"Timeout: template is still {snapshot.get('status')!r} after {timeout:g}s",
                file=sys.stderr,
            )
            return 1
        time.sleep(min(interval, remaining))


def _drop_legacy(registry: TemplateRegistry, force: bool) -> int:
    snapshot = registry.current(force=True)
    if not force and not snapshot.get("legacy_deprecated"):
        print(
            "The legacy document-header builder is still active (the clean template "
            "is not approved yet). Re-run once the template is approved, or pass --force.",
            file=sys.stderr,
        )
        return 1
    print(
        "Legacy document-header builder is deprecated and is no longer selected "
        f"(active_builder: {snapshot.get('active_builder')}). To fully remove its code, "
        "delete the LegacyInvoiceTemplateBuilder class in sender/domain/templates.py "
        "and the legacy-document tests in tests/test_payload_contract.py; the clean "
        "builder remains the only template builder."
    )
    return 0


def _format_poll_summary(summary: dict) -> str:
    lines = []
    for app in summary.get("apps", []):
        if not app["ok"]:
            lines.append(f"{app['app']}: FAILED ({app['error']})")
            continue
        sent = f"would_send {app['would_send']}" if app.get("dry_run") else f"sent {app['sent']}"
        line = (
            f"{app['app']}: listed {app['listed']}, new {app['new']}, "
            f"{sent}, skipped_no_phone {app['skipped_no_phone']}, "
            f"failed {app['failed']}, pending {app['pending']}, abandoned {app['abandoned']}"
        )
        # Only surfaced when it happened: a run where every invoice went out on the
        # approved template must not imply a fallback was involved.
        fallback = app.get("fallback_sends") or 0
        if fallback:
            line += f", {fallback} via free-form fallback"
        lines.append(line)
        if app["first_run"] and app["seeded"]:
            verb = "would seed" if app.get("dry_run") else "seeded"
            lines.append(f"  first run: {verb} {app['seeded']} existing invoice(s) without sending")
        for invoice in app["invoices"]:
            if invoice["status"] == "sent":
                if invoice.get("dry_run"):
                    lines.append(f"  {invoice['number']} -> {invoice['to']} would send (dry-run)")
                elif invoice.get("fallback"):
                    lines.append(
                        f"  {invoice['number']} -> {invoice['to']} sent via free-form "
                        f"{invoice.get('fallback_kind', 'text')} fallback "
                        f"({invoice.get('error', 'template send failed')})"
                    )
                else:
                    lines.append(f"  {invoice['number']} -> {invoice['to']} sent")
            elif invoice["status"] == "skipped_no_phone":
                lines.append(f"  {invoice['number']} skipped (no usable phone)")
            elif invoice["status"] == "pending":
                lines.append(f"  {invoice['number']} deferred (retryable failure, backoff)")
            elif invoice["status"] == "abandoned":
                lines.append(f"  {invoice['number']} abandoned (permanent failure)")
            else:
                lines.append(f"  {invoice['number']} failed")
    return "\n".join(lines)


def _format_timestamp(value) -> str:
    if value is None:
        return "never"
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return str(value)


def _attachment_mode_for(
    settings: Settings, override: str | None, *, use_stub: bool = False
) -> str:
    """The attachment mode a command will actually use.

    ``--stub`` must stay offline, so an unspecified mode falls back to ``link``
    there; an explicitly requested ``upload`` is honored (the operator named it)
    and warned about by the caller.
    """
    mode = (override or settings.invoice_attachment or ATTACH_UPLOAD).strip().lower()
    if use_stub and mode == ATTACH_UPLOAD and not override:
        return ATTACH_LINK
    return mode


def _run_poll(args: argparse.Namespace, settings: Settings) -> int:
    import signal

    dry_run = args.dry_run or settings.dry_run
    interval = args.interval if args.interval is not None else settings.poll_interval
    limit = args.limit if args.limit is not None else settings.poll_limit
    if not args.once and interval <= 0:
        raise RuntimeError(f"poll --interval must be > 0, got {interval:g}")
    attachment_mode = _attachment_mode_for(
        settings, getattr(args, "attachment", None), use_stub=args.stub
    )
    if dry_run and attachment_mode == ATTACH_UPLOAD:
        logging.warning(
            "poll --dry-run with the %s attachment mode can still upload a PDF to "
            "Meta for every invoice, because a document-header template payload has "
            "to reference a real media id; a clean text-header template attaches no "
            "document and uploads nothing, so check which builder is active "
            "(python -m sender template-status). To guarantee a run that touches "
            "nothing, pass --attachment link",
            ATTACH_UPLOAD,
        )
    if args.stub:
        # Stub mode stays offline: pin a concrete builder instead of resolving
        # via the Graph API, and persist the fixture + state to dedicated files
        # that are obviously separate from the real poll_state.json.
        if attachment_mode == ATTACH_UPLOAD:
            logging.warning(
                "--stub with an explicit %s attachment uploads a real PDF to Meta",
                ATTACH_UPLOAD,
            )
        mode = args.builder if args.builder in ("legacy", "new") else "legacy"
        builder = _builder_for_mode(
            mode, settings, _attachment_provider(settings, attachment_mode)
        )
        source = StubInvoiceSource(path=settings.stub_invoices_path)
        apps = [PollApp(name="stub", source=source)]
        state = JsonPollStateStore(settings.poll_stub_state_path, max_seen=settings.poll_max_seen)
        lock = None
    else:
        builder = _resolve_builder(
            settings, args.builder, _attachment_provider(settings, attachment_mode)
        )
        apps = [
            PollApp(
                name=app.name,
                source=DaftraClient(
                    api_key=app.api_key,
                    base_url=app.base_url,
                    timeout=app.timeout,
                    country_code=settings.default_country_code,
                ),
            )
            for app in settings.apps
        ]
        state = JsonPollStateStore(settings.poll_state_path, max_seen=settings.poll_max_seen)
        lock = PollStateLock(settings.poll_state_path)
    max_sends = args.max_sends if args.max_sends is not None else settings.poll_max_sends_per_run
    recorder = _report_recorder(settings, stub=bool(getattr(args, "stub", False)))
    sender = WhatsAppClient(
        access_token=settings.wa_access_token,
        phone_number_id=settings.wa_phone_number_id,
        api_version=settings.wa_api_version,
        timeout=settings.wa_timeout,
        default_country_code=settings.default_country_code,
        own_number=settings.wa_own_number or None,
        max_retry_wait=settings.wa_max_retry_wait,
    )
    poller = InvoicePoller(
        apps=apps,
        sender=sender,
        builder=builder,
        state=state,
        clock=SystemClock(),
        interval=interval,
        limit=limit,
        dry_run=dry_run,
        send_existing=args.send_existing,
        once=args.once,
        max_cycles=args.max_cycles,
        timeout=args.timeout,
        max_backoff=settings.poll_max_backoff,
        max_pages=settings.poll_max_pages,
        country_code=settings.default_country_code,
        freeform_fallback=settings.wa_freeform_fallback,
        max_sends_per_run=max_sends,
        recorder=recorder,
    )

    def _handle_signal(signum, frame):
        raise KeyboardInterrupt

    previous_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.signal(sig, _handle_signal)
    summary = None
    try:
        if lock is not None:
            with lock:
                summary = poller.run()
        else:
            summary = poller.run()
    except KeyboardInterrupt:
        # A signal can land before the poller's own handler catches it (e.g.
        # during lock acquisition); treat it as a clean interrupt.
        summary = {"apps": [], "all_failed": False, "interrupted": True}
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)

    if args.once:
        print(_format_poll_summary(summary))
    else:
        logging.info("poll finished: %s", _format_poll_summary(summary))
    _refresh_report_pages(settings, recorder, stub=bool(getattr(args, "stub", False)))
    if summary.get("interrupted"):
        # An interrupt mid-cycle means the run did not finish what it set out to
        # do. In daemon mode that is the normal way to stop (exit 0), but a
        # one-shot cron run that was killed mid-cycle must report failure so the
        # scheduler and monitoring can tell "completed" from "killed".
        return 1 if args.once else 0
    return 1 if summary.get("all_failed") else 0


def _report_paths(settings: Settings, *, stub: bool) -> tuple[str, str]:
    """The (html dir, data dir) for the report, real or stub."""
    if stub:
        return settings.report_stub_dir, f"{settings.report_stub_dir}/data"
    return settings.report_dir, settings.report_data_dir


def _report_recorder(settings: Settings, *, stub: bool):
    """The send-outcome recorder, or ``None`` when reporting is off."""
    if not settings.report_enabled:
        return None
    _, data_dir = _report_paths(settings, stub=stub)
    return JsonlSendOutcomeRecorder(data_dir)


def _refresh_report_pages(settings: Settings, recorder, *, stub: bool) -> None:
    """Rebuild the report pages touched by this run, then apply retention.

    Done after the run rather than inside it: the poll-state lock is released
    by then, and regeneration is a pure read of the log followed by atomic
    writes, so it cannot interleave with a send. Best-effort — a report problem
    must not turn a successful send cycle into a failed exit code.
    """
    if recorder is None or not getattr(recorder, "dates_written", None):
        return
    try:
        report_dir, data_dir = _report_paths(settings, stub=stub)
        store = ReportStore(report_dir, data_dir, obfuscate=settings.report_obfuscate_phone)
        store.render(recorder.dates_written)
        store.prune(settings.report_retention_days)
    except Exception:  # noqa: BLE001 - reporting must never fail a poll
        log.exception("could not refresh the HTML send report")


def _run_report(args: argparse.Namespace, settings: Settings) -> int:
    """Rebuild the HTML report pages from the send log."""
    report_dir, data_dir = _report_paths(settings, stub=args.stub)
    store = ReportStore(report_dir, data_dir, obfuscate=settings.report_obfuscate_phone)
    available = store.available_dates()
    if not available:
        print(f"No send history yet in {data_dir}")
        return 0
    days = None
    if args.date:
        try:
            days = [date.fromisoformat(args.date)]
        except ValueError:
            print(f"--date must be YYYY-MM-DD, got {args.date!r}", file=sys.stderr)
            return 2
    written = store.render(days)
    pruned = store.prune(settings.report_retention_days)
    print(f"Wrote {len(written)} file(s) to {report_dir}")
    if pruned:
        print(f"Pruned {len(pruned)} expired day(s): {', '.join(d.isoformat() for d in pruned)}")
    return 0


def _run_poll_status(args: argparse.Namespace, settings: Settings) -> int:
    path = settings.poll_stub_state_path if args.stub else settings.poll_state_path
    state = JsonPollStateStore(path, max_seen=settings.poll_max_seen)
    names = state.app_names()
    if not names:
        print(f"no poll state recorded yet at {path}")
        return 0
    for name in names:
        seen = state.seen_ids(name)
        pending = state.pending(name)
        abandoned = state.abandoned(name)
        last = state.last_poll_at(name)
        print(f"{name}:")
        print(f"  seen: {len(seen)} invoice(s)")
        if pending:
            print(f"  pending (retryable, will retry): {len(pending)}")
            for sid, rec in sorted(pending.items()):
                remaining = max(0.0, float(rec.get("next_attempt_at", 0)) - time.time())
                print(
                    f"    invoice {sid} ({rec.get('action')}): {rec.get('error')} "
                    f"(attempt {rec.get('count')}, retry in {remaining:.0f}s)"
                )
        if abandoned:
            print(f"  abandoned (permanent, will not retry): {len(abandoned)}")
            for sid, rec in sorted(abandoned.items()):
                print(
                    f"    invoice {sid} ({rec.get('action')}): {rec.get('error')} "
                    f"(attempt {rec.get('count')})"
                )
        print(f"  last_poll_at: {_format_timestamp(last)}")
    return 0


def _run_poll_reset(args: argparse.Namespace, settings: Settings) -> int:
    if not args.yes:
        raise RuntimeError("poll-reset requires --yes to confirm")
    path = settings.poll_stub_state_path if args.stub else settings.poll_state_path
    state = JsonPollStateStore(path, max_seen=settings.poll_max_seen)
    lock = None if args.stub else PollStateLock(path)

    def _do_reset() -> None:
        if args.invoice_id:
            state.clear_invoice(args.app, args.invoice_id)
            print(f"cleared invoice {args.invoice_id} for app {args.app!r} in {path}")
        else:
            if not state.has_app(args.app):
                print(f"app {args.app!r} has no recorded state in {path}; nothing to reset")
                return
            state.reset_app(args.app)
            print(f"reset app {args.app!r} in {path}")

    if lock is not None:
        with lock:
            _do_reset()
    else:
        _do_reset()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    try:
        args = _build_parser().parse_args(argv)
        service_cmds = ("send", "preview", "show", "list")
        template_cmd = args.command in ("template-status", "template-watch", "template-drop-legacy")
        need_whatsapp = args.command in ("send", "preview", "webhook-subscribe", "poll")
        require_daftra = not getattr(args, "stub", False) and args.command in service_cmds
        require_apps = args.command == "poll" and not getattr(args, "stub", False)
        settings = Settings.from_env(
            require_whatsapp=need_whatsapp,
            require_daftra=require_daftra,
            require_apps=require_apps,
        )
        logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")

        if args.command == "webhook-serve":
            from sender.infrastructure.whatsapp.webhook_server import serve

            if not settings.wa_verify_token:
                raise RuntimeError("WHATSAPP_VERIFY_TOKEN must be set in the environment")
            print(f"Webhook receiver ready on {args.host}:{args.port}; events -> {args.events_file}")
            serve(args.host, args.port, settings.wa_verify_token, args.events_file, daemon=False)
            raise RuntimeError("webhook receiver exited unexpectedly")

        if args.command == "webhook-subscribe":
            from sender.infrastructure.whatsapp.webhook_server import subscribe

            if not settings.wa_waba_id:
                raise RuntimeError("WHATSAPP_WABA_ID must be set in the environment")
            result = subscribe(settings.wa_access_token, settings.wa_waba_id, settings.wa_api_version)
            print(json.dumps(result, indent=2))
        elif args.command == "poll":
            return _run_poll(args, settings)
        elif args.command == "poll-status":
            return _run_poll_status(args, settings)
        elif args.command == "poll-reset":
            return _run_poll_reset(args, settings)
        elif args.command == "stub-add":
            source = StubInvoiceSource(path=settings.stub_invoices_path)
            invoice = source.add_new_invoice()
            print(f"added {invoice.number} (id {invoice.id}) to {settings.stub_invoices_path}")
        elif args.command == "report":
            return _run_report(args, settings)
        elif template_cmd:
            if not settings.wa_waba_id:
                raise RuntimeError("WHATSAPP_WABA_ID must be set in the environment")
            return _run_template_command(args, settings)
        else:
            # send/preview/show/list. show/list never build a message, so the
            # template builder is not resolved (avoids a Meta Graph API round-trip).
            if args.command in ("send", "preview"):
                service = build_service(
                    settings,
                    with_whatsapp=True,
                    use_stub=args.stub,
                    builder_override=getattr(args, "builder", None),
                    attachment_mode=getattr(args, "attachment", None),
                )
            else:
                service = build_service(
                    settings,
                    with_whatsapp=False,
                    use_stub=args.stub,
                    resolve_builder=False,
                )

            if args.command == "send":
                dry_run = args.dry_run or settings.dry_run
                if args.freeform:
                    result = service.send_freeform(
                        args.invoice_id, to_phone=args.to, dry_run=dry_run
                    )
                elif dry_run or not _is_auto_builder(args, settings):
                    result = service.send_invoice(
                        args.invoice_id, to_phone=args.to, dry_run=dry_run
                    )
                else:
                    result = _send_template_with_retry(service, args, settings)
                if result.get("dry_run"):
                    print(json.dumps(result["payload"], indent=2))
                else:
                    wamid = ((result.get("response") or {}).get("messages") or [{}])[0].get("id", "?")
                    print(
                        f"Sent invoice {result['invoice'].number} to {result['to']} "
                        f"(wamid: {wamid})"
                    )
            elif args.command == "preview":
                _, recipient, payload = service.preview_invoice(args.invoice_id, args.to, freeform=args.freeform)
                print(json.dumps(payload, indent=2))
            elif args.command == "show":
                if args.raw:
                    print(json.dumps(service.get_raw_invoice(args.invoice_id), indent=2))
                else:
                    invoice = service.get_invoice(args.invoice_id)
                    print(json.dumps(asdict(invoice), indent=2, default=str))
            elif args.command == "list":
                for invoice in service.list_invoices(args.limit):
                    print(
                        f"{invoice.number:<18} {invoice.customer_name:<28} "
                        f"{invoice.total} {invoice.currency:<5} {invoice.status}"
                    )
    except (ApiError, ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0