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

from dotenv import find_dotenv, load_dotenv

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
    CustomerTemplateBuilder,
    InvoiceTemplateBuilder,
    LegacyInvoiceTemplateBuilder,
    PaymentTemplateBuilder,
)
from sender.application.poller import CustomerPoller, InvoicePoller, PaymentPoller, PollApp
from sender.application.services import (
    CustomerNotificationService,
    InvoiceNotificationService,
    PaymentNotificationService,
)
from sender.infrastructure.clock import SystemClock
from sender.infrastructure.state import JsonPollStateStore, PollStateLock
from sender.infrastructure.attachments import UploadedMediaProvider
from sender.infrastructure.reporting import JsonlSendOutcomeRecorder, ReportStore
from sender.presentation.stubs import (
    StubCustomerSource,
    StubInvoiceSource,
    StubMessageSender,
    StubPaymentSource,
    default_stub_customers,
    default_stub_invoices,
)


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


def _meta_sender(settings: Settings, *, meta_stub: bool = False, session: object | None = None):
    """The WhatsApp sender for a command: the real client, or an offline capture.

    The one place the "will this run reach Meta?" decision is made, so the answer
    is the same for every command. ``--meta-stub`` is the *only* way to rehearse
    without sending: stubbing a Daftra source says nothing about the sender, and
    conflating the two is what let an offline-looking rehearsal message a real
    customer.
    """
    if meta_stub:
        logging.info(
            "--meta-stub: no message will reach Meta; every payload is captured and "
            "logged instead"
        )
        return StubMessageSender()
    return WhatsAppClient(
        access_token=settings.wa_access_token,
        phone_number_id=settings.wa_phone_number_id,
        api_version=settings.wa_api_version,
        timeout=settings.wa_timeout,
        default_country_code=settings.default_country_code,
        own_number=settings.wa_own_number or None,
        max_retry_wait=settings.wa_max_retry_wait,
        session=session,
    )


def _stub_source(settings: Settings) -> StubInvoiceSource:
    """The offline stub source: the persistent fixture when it exists, otherwise
    the built-in sample invoices (without creating the fixture file)."""
    path = Path(settings.stub_invoices_path)
    if path.exists():
        return StubInvoiceSource(path=str(path))
    return StubInvoiceSource(default_stub_invoices())


def _stub_payment_source(settings: Settings) -> StubPaymentSource:
    """The payments twin of :func:`_stub_source`, on its own fixture and filter."""
    path = Path(settings.stub_payments_path)
    status = settings.payments_status_filter
    if path.exists():
        return StubPaymentSource(path=str(path), status=status)
    return StubPaymentSource(status=status)


def _payment_builder(settings: Settings) -> PaymentTemplateBuilder:
    """The payment template builder, constructed from config and nothing else.

    No registry lookup and no network: the payment template has one approved
    shape (body only, no header component), so there is no builder to choose
    between, and resolving one would cost a Graph API round-trip per run to learn
    something already known.
    """
    return PaymentTemplateBuilder(
        settings.wa_payment_template_name,
        settings.wa_payment_template_lang,
        settings.default_country_code,
    )


def build_payment_service(
    settings: Settings,
    with_whatsapp: bool = True,
    payment_stub: bool = False,
    meta_stub: bool = False,
    session: object | None = None,
) -> PaymentNotificationService:
    """Wire the payments pipeline. Mirrors :func:`build_service` minus the parts
    that do not apply: no attachment provider (no header document to attach) and
    no builder resolution (one legal payload shape)."""
    whatsapp = (
        _meta_sender(settings, meta_stub=meta_stub, session=session)
        if with_whatsapp
        else None
    )
    if payment_stub:
        source = _stub_payment_source(settings)
    else:
        app = settings.primary_app
        source = DaftraClient(
            api_key=app.api_key,
            base_url=app.base_url,
            timeout=app.timeout,
            country_code=settings.default_country_code,
            payments_status=settings.payments_status_filter,
            session=session,
        )
    return PaymentNotificationService(
        source=source,
        sender=whatsapp,
        builder=_payment_builder(settings),
    )


def _stub_customer_source(settings: Settings) -> StubCustomerSource:
    """The customers twin of :func:`_stub_source`, on its own fixture.

    No status filter, unlike :func:`_stub_payment_source`: the payments client
    narrows its listing server-side, so the stub mirrors that narrowing. The
    customers pipeline decides who is new from its seen-set instead, so there is
    no query parameter to mirror and none is invented here.
    """
    path = Path(settings.stub_customers_path)
    if path.exists():
        return StubCustomerSource(path=str(path))
    return StubCustomerSource(default_stub_customers())


def _customer_builder(settings: Settings) -> CustomerTemplateBuilder:
    """The welcome template builder, constructed from config and nothing else.

    No registry lookup and no network: like the payment template, this one has a
    single approved shape (body only, no header), so there is no builder to
    choose between and resolving one would spend a Graph API round-trip to learn
    something already known.
    """
    return CustomerTemplateBuilder(
        settings.wa_customer_template_name,
        settings.wa_customer_template_lang,
        settings.default_country_code,
    )


def build_customer_service(
    settings: Settings,
    with_whatsapp: bool = True,
    customer_stub: bool = False,
    meta_stub: bool = False,
    session: object | None = None,
) -> CustomerNotificationService:
    """Wire the customers pipeline. Mirrors :func:`build_payment_service` minus
    the parts that do not apply: no attachment provider (no header document to
    attach) and no builder resolution (one legal payload shape)."""
    whatsapp = (
        _meta_sender(settings, meta_stub=meta_stub, session=session)
        if with_whatsapp
        else None
    )
    if customer_stub:
        source = _stub_customer_source(settings)
    else:
        app = settings.primary_app
        source = DaftraClient(
            api_key=app.api_key,
            base_url=app.base_url,
            timeout=app.timeout,
            country_code=settings.default_country_code,
            session=session,
        )
    return CustomerNotificationService(
        source=source,
        sender=whatsapp,
        builder=_customer_builder(settings),
    )


def build_service(
    settings: Settings,
    with_whatsapp: bool = True,
    invoice_stub: bool = False,
    meta_stub: bool = False,
    builder_override: str | None = None,
    resolve_builder: bool = True,
    attachment_mode: str | None = None,
    session: object | None = None,
) -> InvoiceNotificationService:
    whatsapp = (
        _meta_sender(settings, meta_stub=meta_stub, session=session)
        if with_whatsapp
        else None
    )
    if invoice_stub:
        source = _stub_source(settings)
    else:
        app = settings.primary_app
        source = DaftraClient(
            api_key=app.api_key,
            base_url=app.base_url,
            timeout=app.timeout,
            country_code=settings.default_country_code,
        )
    if invoice_stub:
        # A stubbed source must not resolve the builder via the Graph API (that is
        # the other half of "offline"), and must not push a PDF to Meta unless it
        # was asked for by name.
        attachment_mode = _attachment_mode_for(settings, attachment_mode, use_stub=True)
        if attachment_mode == ATTACH_UPLOAD:
            logging.warning(
                "--invoice-stub with an explicit %s attachment uploads a real PDF to "
                "Meta; use --attachment link, or add --meta-stub, for a fully "
                "offline run",
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
    send.add_argument("--invoice-stub", action="store_true", help="Use the offline stub invoice source instead of Daftra")
    send.add_argument("--meta-stub", action="store_true", help="Do not call Meta at all: capture the payloads instead of sending them. Needed for a fully offline rehearsal")
    send.add_argument("--builder", choices=("auto", "legacy", "new"), default=None, help="Template builder: auto resolves via template status (default), legacy/new pin one")
    _add_attachment_argument(send)

    preview = sub.add_parser("preview", help="Print the WhatsApp payload without sending")
    preview.add_argument("--invoice-id", required=True, help="Daftra invoice id")
    preview.add_argument("--to", help="Recipient phone number (defaults to the customer phone on the invoice)")
    preview.add_argument("--freeform", action="store_true", help="Preview as plain text instead of template")
    preview.add_argument("--invoice-stub", action="store_true", help="Use the offline stub invoice source instead of Daftra")
    preview.add_argument("--meta-stub", action="store_true", help="Do not call Meta at all: capture the payloads instead of sending them. Needed for a fully offline rehearsal")
    preview.add_argument("--builder", choices=("auto", "legacy", "new"), default=None, help="Template builder: auto resolves via template status (default), legacy/new pin one")
    _add_attachment_argument(preview, note="upload resolves a real media id (and so uploads the PDF) but never sends")

    show = sub.add_parser("show", help="Print a normalized invoice (use --raw for the Daftra JSON)")
    show.add_argument("--invoice-id", required=True, help="Daftra invoice id")
    show.add_argument("--raw", action="store_true", help="Print the raw Daftra JSON response")
    show.add_argument("--invoice-stub", action="store_true", help="Use the offline stub invoice source instead of Daftra")

    listing = sub.add_parser("list", help="List recent invoices")
    listing.add_argument("--limit", type=int, default=10, help="Number of invoices to list")
    listing.add_argument("--invoice-stub", action="store_true", help="Use the offline stub invoice source instead of Daftra")

    poll = sub.add_parser("poll", help="Poll Daftra for new invoices and send them automatically")
    poll.add_argument("--once", action="store_true", help="Run exactly one cycle and exit")
    poll.add_argument("--interval", type=float, default=None, help="Seconds between cycles (default: POLL_INTERVAL env, 60)")
    poll.add_argument("--limit", type=int, default=None, help="List page size (default: POLL_LIMIT env, 10)")
    poll.add_argument("--max-cycles", type=int, default=None, help="Stop after this many cycles")
    poll.add_argument("--max-sends", type=int, default=None, help="Cap send attempts per cycle across all apps (default: POLL_MAX_SENDS_PER_RUN env, 10; 0 disables)")
    poll.add_argument("--timeout", type=float, default=None, help="Stop after this many seconds")
    poll.add_argument("--dry-run", action="store_true", help="Build payloads without sending and without touching the poll state")
    poll.add_argument("--send-existing", action="store_true", help="On the first run, send the currently existing invoices instead of seeding them as seen")
    poll.add_argument("--invoice-stub", action="store_true", help="Use the offline stub invoice source instead of Daftra")
    poll.add_argument("--meta-stub", action="store_true", help="Do not call Meta at all: capture the payloads instead of sending them. Needed for a fully offline rehearsal")
    poll.add_argument("--builder", choices=("auto", "legacy", "new"), default=None, help="Template builder: auto resolves via template status (default), legacy/new pin one")
    _add_attachment_argument(poll, note="poll --dry-run does not upload unless the mode is upload")

    payments_listing = sub.add_parser("payments", help="List recent payments")
    payments_listing.add_argument("--limit", type=int, default=10, help="Number of payments to list")
    payments_listing.add_argument("--payment-stub", action="store_true", help="Use the offline stub payment source instead of Daftra")

    show_payment = sub.add_parser("show-payment", help="Print a normalized payment (use --raw for the Daftra JSON)")
    show_payment.add_argument("--payment-id", required=True, help="Daftra payment id")
    show_payment.add_argument("--raw", action="store_true", help="Print the raw Daftra JSON response")
    show_payment.add_argument("--payment-stub", action="store_true", help="Use the offline stub payment source instead of Daftra")

    send_payment = sub.add_parser("send-payment", help="Fetch a payment and send its confirmation on WhatsApp")
    send_payment.add_argument("--payment-id", required=True, help="Daftra payment id")
    send_payment.add_argument("--to", help="Recipient phone number (defaults to the payer on the linked invoice)")
    send_payment.add_argument("--dry-run", action="store_true", help="Print the payload without calling Meta")
    send_payment.add_argument("--freeform", action="store_true", help="Send as plain text instead of template (for layout testing)")
    send_payment.add_argument("--payment-stub", action="store_true", help="Use the offline stub payment source instead of Daftra")
    send_payment.add_argument("--meta-stub", action="store_true", help="Do not call Meta at all: capture the payloads instead of sending them. Needed for a fully offline rehearsal")

    poll_payments = sub.add_parser("poll-payments", help="Poll Daftra for new payments and confirm them on WhatsApp")
    poll_payments.add_argument("--once", action="store_true", help="Run exactly one cycle and exit")
    poll_payments.add_argument("--interval", type=float, default=None, help="Seconds between cycles (default: POLL_INTERVAL env, 60)")
    poll_payments.add_argument("--limit", type=int, default=None, help="List page size (default: POLL_PAYMENTS_LIMIT env, 10)")
    poll_payments.add_argument("--max-cycles", type=int, default=None, help="Stop after this many cycles")
    poll_payments.add_argument("--max-sends", type=int, default=None, help="Cap send attempts per cycle across all apps (default: POLL_PAYMENTS_MAX_SENDS_PER_RUN env, 10; 0 disables)")
    poll_payments.add_argument("--timeout", type=float, default=None, help="Stop after this many seconds")
    poll_payments.add_argument("--dry-run", action="store_true", help="Build payloads without sending and without touching the payment poll state")
    poll_payments.add_argument("--send-existing", action="store_true", help="On the first run, confirm the currently existing payments instead of seeding them as seen")
    poll_payments.add_argument("--payment-stub", action="store_true", help="Use the offline stub payment source instead of Daftra")
    poll_payments.add_argument("--meta-stub", action="store_true", help="Do not call Meta at all: capture the payloads instead of sending them. Needed for a fully offline rehearsal")

    customers_listing = sub.add_parser("customers", help="List recent customers")
    customers_listing.add_argument("--limit", type=int, default=10, help="Number of customers to list")
    customers_listing.add_argument("--customer-stub", action="store_true", help="Use the offline stub customer source instead of Daftra")

    show_customer = sub.add_parser("show-customer", help="Print a normalized customer (use --raw for the Daftra JSON)")
    show_customer.add_argument("--customer-id", required=True, help="Daftra client id")
    show_customer.add_argument("--raw", action="store_true", help="Print the raw Daftra JSON response")
    show_customer.add_argument("--customer-stub", action="store_true", help="Use the offline stub customer source instead of Daftra")

    send_customer = sub.add_parser("send-customer", help="Fetch a customer and send its welcome on WhatsApp")
    send_customer.add_argument("--customer-id", required=True, help="Daftra client id")
    send_customer.add_argument("--to", help="Recipient phone number (defaults to the client on file)")
    send_customer.add_argument("--dry-run", action="store_true", help="Print the payload without calling Meta")
    send_customer.add_argument("--freeform", action="store_true", help="Send as plain text instead of template (for layout testing)")
    send_customer.add_argument("--customer-stub", action="store_true", help="Use the offline stub customer source instead of Daftra")
    send_customer.add_argument("--meta-stub", action="store_true", help="Do not call Meta at all: capture the payloads instead of sending them. Needed for a fully offline rehearsal")

    poll_customers = sub.add_parser("poll-customers", help="Poll Daftra for new customers and welcome them on WhatsApp")
    poll_customers.add_argument("--once", action="store_true", help="Run exactly one cycle and exit")
    poll_customers.add_argument("--interval", type=float, default=None, help="Seconds between cycles (default: POLL_INTERVAL env, 60)")
    poll_customers.add_argument("--limit", type=int, default=None, help="List page size (default: POLL_CUSTOMERS_LIMIT env, 10)")
    poll_customers.add_argument("--max-cycles", type=int, default=None, help="Stop after this many cycles")
    poll_customers.add_argument("--max-sends", type=int, default=None, help="Cap send attempts per cycle across all apps (default: POLL_CUSTOMERS_MAX_SENDS_PER_RUN env, 10; 0 disables)")
    poll_customers.add_argument("--timeout", type=float, default=None, help="Stop after this many seconds")
    poll_customers.add_argument("--dry-run", action="store_true", help="Build payloads without sending and without touching the customer poll state")
    poll_customers.add_argument("--send-existing", action="store_true", help="On the first run, welcome the currently existing customers instead of seeding them as seen")
    poll_customers.add_argument("--customer-stub", action="store_true", help="Use the offline stub customer source instead of Daftra")
    poll_customers.add_argument("--meta-stub", action="store_true", help="Do not call Meta at all: capture the payloads instead of sending them. Needed for a fully offline rehearsal")

    poll_status = sub.add_parser("poll-status", help="Show the poll state (seen/pending/abandoned) per app")
    poll_status.add_argument("--invoice-stub", action="store_true", help="Inspect the offline stub invoice poll state instead of the real one")
    poll_status.add_argument("--payment-stub", action="store_true", help="Inspect the offline stub payment poll state instead of the real one")
    poll_status.add_argument("--payments", action="store_true", help="Inspect the payments poll state instead of the invoice one")
    poll_status.add_argument("--customer-stub", action="store_true", help="Inspect the offline stub customer poll state instead of the real one")
    poll_status.add_argument("--customers", action="store_true", help="Inspect the customers poll state instead of the invoice one")

    poll_reset = sub.add_parser("poll-reset", help="Reset poll state for an app (requires --yes)")
    poll_reset.add_argument("--app", required=True, help="App name to reset")
    # One dest, three spellings: a record in this store is an invoice id, a
    # payment id or a client id depending on --payments/--customers, and argparse
    # cannot validate which.
    poll_reset.add_argument(
        "--invoice-id", "--payment-id", "--customer-id", dest="document_id", default=None,
        help="Reset only this record instead of the whole app (an invoice id, or a payment/client id with --payments/--customers)",
    )
    poll_reset.add_argument("--yes", action="store_true", help="Confirm the reset")
    poll_reset.add_argument("--invoice-stub", action="store_true", help="Reset the offline stub invoice poll state instead of the real one")
    poll_reset.add_argument("--payment-stub", action="store_true", help="Reset the offline stub payment poll state instead of the real one")
    poll_reset.add_argument("--payments", action="store_true", help="Reset the payments poll state instead of the invoice one")
    poll_reset.add_argument("--customer-stub", action="store_true", help="Reset the offline stub customer poll state instead of the real one")
    poll_reset.add_argument("--customers", action="store_true", help="Reset the customers poll state instead of the invoice one")

    stub_add = sub.add_parser("stub-add", help="Add a new invoice to the offline stub fixture")

    stub_payment_add = sub.add_parser("stub-payment-add", help="Add a new payment to the offline stub payment fixture")

    stub_customer_add = sub.add_parser("stub-customer-add", help="Add a new customer to the offline stub customer fixture")

    report = sub.add_parser("report", help="Regenerate the HTML pages of sent messages from the send log")
    report.add_argument("--date", default=None, help="Regenerate only this YYYY-MM-DD page (default: every logged date)")
    report.add_argument("--stub-dir", action="store_true", help="Use the offline rehearsal report directory instead of the real one")

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
        # One summary format for two pipelines: the word for the document kind
        # comes from the result itself, defaulting to an invoice so a summary
        # built by anything older still reads correctly.
        noun = {"payment": "payment", "customer": "customer"}.get(app.get("kind"), "invoice")
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
            lines.append(f"  first run: {verb} {app['seeded']} existing {noun}(s) without sending")
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


def _run_poll(args: argparse.Namespace, settings: Settings, *, invoice_stub: bool = False, meta_stub: bool = False) -> int:
    attachment_mode = _attachment_mode_for(
        settings, getattr(args, "attachment", None), use_stub=invoice_stub
    )
    dry_run = args.dry_run or settings.dry_run
    interval = args.interval if args.interval is not None else settings.poll_interval
    limit = args.limit if args.limit is not None else settings.poll_limit
    if not args.once and interval <= 0:
        raise RuntimeError(f"poll --interval must be > 0, got {interval:g}")
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
    if invoice_stub:
        # A stubbed *source* keeps the source and the state offline; the sender is
        # a separate decision (see --meta-stub). With the invoice source stubbed the
        # builder is pinned rather than resolved, because resolving it would call
        # the Graph API — which is the other half of "offline".
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
    recorder = _report_recorder(settings, stub=invoice_stub)
    sender = _meta_sender(settings, meta_stub=meta_stub)
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
    return _run_poll_common(poller, lock, args, settings, recorder, stub=invoice_stub)


def _run_poll_payments(args: argparse.Namespace, settings: Settings, *, payment_stub: bool = False, meta_stub: bool = False) -> int:
    """The payments cycle: same engine, same invariants, its own state and cap.

    Note what is absent compared with :func:`_run_poll`: no attachment mode (the
    payment template has no header document, so nothing is ever rendered or
    uploaded), no ``--builder`` (one approved shape, nothing to resolve), and no
    separate budget (the payments cap is its own env knob).
    """
    dry_run = args.dry_run or settings.dry_run
    interval = args.interval if args.interval is not None else settings.poll_interval
    limit = args.limit if args.limit is not None else settings.payments_limit
    if not args.once and interval <= 0:
        raise RuntimeError(f"poll-payments --interval must be > 0, got {interval:g}")
    builder = _payment_builder(settings)
    if payment_stub:
        source = StubPaymentSource(
            path=settings.stub_payments_path, status=settings.payments_status_filter
        )
        apps = [PollApp(name="stub", source=source)]
        state = JsonPollStateStore(
            settings.payments_stub_state_path, max_seen=settings.poll_max_seen
        )
        lock = None
    else:
        apps = [
            PollApp(
                name=app.name,
                source=DaftraClient(
                    api_key=app.api_key,
                    base_url=app.base_url,
                    timeout=app.timeout,
                    country_code=settings.default_country_code,
                    payments_status=settings.payments_status_filter,
                ),
            )
            for app in settings.apps
        ]
        state = JsonPollStateStore(settings.payments_state_path, max_seen=settings.poll_max_seen)
        lock = PollStateLock(settings.payments_state_path)
    max_sends = (
        args.max_sends if args.max_sends is not None else settings.poll_payments_max_sends_per_run
    )
    # The report is shared with the invoice pipeline on purpose: one audit trail
    # of everything this sender put in front of a customer, with `kind` saying
    # which template each row went out under.
    recorder = _report_recorder(settings, stub=payment_stub)
    sender = _meta_sender(settings, meta_stub=meta_stub)
    poller = PaymentPoller(
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
    return _run_poll_common(poller, lock, args, settings, recorder, stub=payment_stub)


def _run_poll_customers(
    args: argparse.Namespace,
    settings: Settings,
    *,
    customer_stub: bool = False,
    meta_stub: bool = False,
) -> int:
    """The customers cycle: same engine, same invariants, its own state and cap.

    Absent compared with :func:`_run_poll_payments`, for the same reasons: no
    attachment mode (the welcome template has no header document), no
    ``--builder`` (one approved shape), and a separate budget.

    The one thing that differs *in kind* from both other pipelines is the
    free-form fallback, which this pipeline leaves off. A welcome goes to a
    brand-new number, which is outside the 24-hour customer-service window where
    free-form text is deliverable at all, so a fallback here could only ever burn
    a doomed request. With it off, a rejected template leaves the customer
    **pending** instead — fix the template and they flow on a later cycle.
    """
    dry_run = args.dry_run or settings.dry_run
    interval = args.interval if args.interval is not None else settings.poll_interval
    limit = args.limit if args.limit is not None else settings.customers_limit
    if not args.once and interval <= 0:
        raise RuntimeError(f"poll-customers --interval must be > 0, got {interval:g}")
    builder = _customer_builder(settings)
    if customer_stub:
        source = StubCustomerSource(path=settings.stub_customers_path)
        apps = [PollApp(name="stub", source=source)]
        state = JsonPollStateStore(
            settings.customers_stub_state_path, max_seen=settings.poll_max_seen
        )
        lock = None
    else:
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
        state = JsonPollStateStore(
            settings.customers_state_path, max_seen=settings.poll_max_seen
        )
        lock = PollStateLock(settings.customers_state_path)
    max_sends = (
        args.max_sends if args.max_sends is not None else settings.poll_customers_max_sends_per_run
    )
    # Shared report, as with the other two pipelines: one audit trail of everything
    # this sender put in front of a customer, with `kind` naming the template.
    recorder = _report_recorder(settings, stub=customer_stub)
    sender = _meta_sender(settings, meta_stub=meta_stub)
    poller = CustomerPoller(
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
        # Deliberately its own knob, not the shared default: see the docstring.
        freeform_fallback=settings.wa_customer_freeform_fallback,
        max_sends_per_run=max_sends,
        recorder=recorder,
    )
    return _run_poll_common(poller, lock, args, settings, recorder, stub=customer_stub)


def _run_poll_common(
    poller,
    lock,
    args: argparse.Namespace,
    settings: Settings,
    recorder,
    *,
    stub: bool = False,
) -> int:
    """Run a poller under the lock and report the result, for either pipeline.

    Shared verbatim so the two pipelines cannot drift on the parts an operator
    depends on: signal handling, that the state lock is held for the whole run,
    that the one-shot summary goes to stdout while the daemon's goes to the log,
    that the report pages are refreshed *after* the lock is released, and the
    exit-code rules.

    *recorder* is passed in rather than read back off the poller: the caller is
    what built it, and taking it as an argument keeps this function working with
    any poller-shaped object (the tests substitute one).
    """
    import signal

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
    _refresh_report_pages(settings, recorder, stub=stub)
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
        # ``logging``, not ``log``: this module never binds a logger (it is the
        # presentation layer and uses the root logger throughout), so a ``log``
        # here raised NameError *inside the handler* — which is not in main()'s
        # except tuple, so a report that failed to render crashed the poll and
        # destroyed the original error. The whole point of this block is that a
        # reporting failure is invisible.
        logging.exception("could not refresh the HTML send report")


def _run_report(args: argparse.Namespace, settings: Settings) -> int:
    """Rebuild the HTML report pages from the send log."""
    report_dir, data_dir = _report_paths(settings, stub=args.stub_dir)
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


def _selected_pipeline(args: argparse.Namespace) -> str:
    """Which of the three pipelines a ``poll-status`` / ``poll-reset`` run is about.

    Each pipeline keeps its own state file because the three id spaces overlap in
    the same account — a client id, a payment id and an invoice id can all be
    ``1`` — so a record in this store is only meaningful together with the flag
    that says which pipeline wrote it. argparse cannot validate that, so the flag
    is the only safe way to say which one is meant, and everything downstream
    (the file, the lock, the noun) is derived from this one answer.
    """
    if bool(getattr(args, "customers", False)):
        return "customer"
    if bool(getattr(args, "payments", False)):
        return "payment"
    return "invoice"


def _poll_state_path(settings: Settings, args: argparse.Namespace) -> str:
    """Which state file a ``poll-status`` / ``poll-reset`` invocation is about.

    ``--payments`` or ``--customers`` picks that pipeline's file, which is a
    genuinely different document space — client, payment and invoice ids all
    overlap in the same account — so there is nothing to disambiguate by
    inspection and the flag is the only safe way to say which one is meant. See
    :func:`_selected_pipeline`.
    """
    pipeline = _selected_pipeline(args)
    stub = bool(getattr(args, f"{pipeline}_stub", False))
    # Each entry is (stub state path, real state path), so the stub flag picks
    # index 0 and the default picks index 1.
    return {
        "invoice": (settings.poll_stub_state_path, settings.poll_state_path),
        "payment": (settings.payments_stub_state_path, settings.payments_state_path),
        "customer": (settings.customers_stub_state_path, settings.customers_state_path),
    }[pipeline][0 if stub else 1]


def _run_poll_status(args: argparse.Namespace, settings: Settings) -> int:
    path = _poll_state_path(settings, args)
    noun = _selected_pipeline(args)
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
        print(f"  seen: {len(seen)} {noun}(s)")
        if pending:
            print(f"  pending (retryable, will retry): {len(pending)}")
            for sid, rec in sorted(pending.items()):
                remaining = max(0.0, float(rec.get("next_attempt_at", 0)) - time.time())
                print(
                    f"    {noun} {sid} ({rec.get('action')}): {rec.get('error')} "
                    f"(attempt {rec.get('count')}, retry in {remaining:.0f}s)"
                )
        if abandoned:
            print(f"  abandoned (permanent, will not retry): {len(abandoned)}")
            for sid, rec in sorted(abandoned.items()):
                print(
                    f"    {noun} {sid} ({rec.get('action')}): {rec.get('error')} "
                    f"(attempt {rec.get('count')})"
                )
        print(f"  last_poll_at: {_format_timestamp(last)}")
    return 0


def _run_poll_reset(args: argparse.Namespace, settings: Settings) -> int:
    if not args.yes:
        raise RuntimeError("poll-reset requires --yes to confirm")
    noun = _selected_pipeline(args)
    path = _poll_state_path(settings, args)
    state = JsonPollStateStore(path, max_seen=settings.poll_max_seen)
    # The stub demo state is single-user; only the real file is locked, so a
    # rehearsal can never collide with (or block) the production poller.
    lock = None if getattr(args, f"{noun}_stub", False) else PollStateLock(path)

    def _do_reset() -> None:
        if getattr(args, "document_id", None):
            state.clear_invoice(args.app, args.document_id)
            print(f"cleared {noun} {args.document_id} for app {args.app!r} in {path}")
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
    # Resolved from the working directory, deliberately. python-dotenv's default
    # walks up from the *calling file*, which for ``python -m sender`` means the
    # package's own location — so a run started anywhere on the machine with the
    # package importable would silently pick up this checkout's live credentials,
    # however unrelated its working directory was. The cron wrapper already
    # ``cd``s into the project, so production is unaffected; what changes is that a
    # command's credentials now belong to the deployment it was started in.
    #
    # The guard matters as much as the argument: ``load_dotenv(None)`` is *not*
    # "load nothing", it re-runs the frame-walking search this line exists to
    # avoid. So when no ``.env`` is found in the working directory, nothing is
    # loaded at all.
    _dotenv = find_dotenv(usecwd=True)
    if _dotenv:
        load_dotenv(_dotenv)
    try:
        args = _build_parser().parse_args(argv)
        service_cmds = ("send", "preview", "show", "list")
        payment_cmds = ("send-payment", "show-payment", "payments")
        customer_cmds = ("send-customer", "show-customer", "customers")
        template_cmd = args.command in ("template-status", "template-watch", "template-drop-legacy")
        # Each external dependency is stubbed by its own flag, so "what does this
        # run actually touch?" is answerable by reading the command line. That is
        # the point: a single generic --stub said only "not Daftra", and left a
        # real Meta client wired up behind it, so a rehearsal believed to be
        # offline could still message a real customer. Stubbing the source does
        # not imply stubbing Meta; you have to ask for both.
        invoice_stub = bool(getattr(args, "invoice_stub", False))
        payment_stub = bool(getattr(args, "payment_stub", False))
        customer_stub = bool(getattr(args, "customer_stub", False))
        meta_stub = bool(getattr(args, "meta_stub", False))
        # Credentials are required exactly when a command can actually reach Meta.
        # ``--meta-stub`` never can, which is what makes a fully offline rehearsal
        # possible on a machine that has no token at all.
        need_whatsapp = args.command in (
            "send", "preview", "webhook-subscribe", "poll", "poll-payments", "send-payment",
            "poll-customers", "send-customer",
        ) and not meta_stub
        # Which pipeline's source stub applies, decided per command rather than
        # per flag: `poll-status` and `poll-reset` accept all three stub flags and
        # all three pipeline selectors at once, so "is the source stubbed" is not
        # a property of any one flag there.
        if args.command in service_cmds or args.command == "poll":
            source_stubbed = invoice_stub
        elif args.command in payment_cmds or args.command == "poll-payments":
            source_stubbed = payment_stub
        else:
            source_stubbed = customer_stub
        require_daftra = (
            not source_stubbed and args.command in service_cmds + payment_cmds + customer_cmds
        )
        require_apps = (
            args.command == "poll" and not invoice_stub
            or args.command == "poll-payments" and not payment_stub
            or args.command == "poll-customers" and not customer_stub
        )
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
            return _run_poll(args, settings, invoice_stub=invoice_stub, meta_stub=meta_stub)
        elif args.command == "poll-payments":
            return _run_poll_payments(args, settings, payment_stub=payment_stub, meta_stub=meta_stub)
        elif args.command == "poll-customers":
            return _run_poll_customers(
                args, settings, customer_stub=customer_stub, meta_stub=meta_stub
            )
        elif args.command == "poll-status":
            return _run_poll_status(args, settings)
        elif args.command == "poll-reset":
            return _run_poll_reset(args, settings)
        elif args.command == "stub-add":
            source = StubInvoiceSource(path=settings.stub_invoices_path)
            invoice = source.add_new_invoice()
            print(f"added {invoice.number} (id {invoice.id}) to {settings.stub_invoices_path}")
        elif args.command == "stub-payment-add":
            source = StubPaymentSource(path=settings.stub_payments_path)
            payment = source.add_new_payment()
            print(
                f"added {payment.number} (id {payment.id}) to {settings.stub_payments_path}"
            )
        elif args.command == "stub-customer-add":
            source = StubCustomerSource(path=settings.stub_customers_path)
            customer = source.add_new_customer()
            print(
                f"added {customer.number} (id {customer.id}) to {settings.stub_customers_path}"
            )
        elif args.command == "report":
            return _run_report(args, settings)
        elif template_cmd:
            if not settings.wa_waba_id:
                raise RuntimeError("WHATSAPP_WABA_ID must be set in the environment")
            return _run_template_command(args, settings)
        else:
            # send/preview/show/list and their payment twins. show/list (and
            # show-payment/payments) never build a message, so the template builder
            # is not resolved — for invoices that avoids a Meta Graph API
            # round-trip, and for payments the builder needs no resolution at all.
            if args.command in payment_cmds + customer_cmds:
                # Handled entirely below, so no invoice service is constructed:
                # building one would require a Daftra client and an invoice
                # builder that this command has no use for.
                service = None
            elif args.command in ("send", "preview"):
                service = build_service(
                    settings,
                    with_whatsapp=True,
                    invoice_stub=invoice_stub,
                    meta_stub=meta_stub,
                    builder_override=getattr(args, "builder", None),
                    attachment_mode=getattr(args, "attachment", None),
                )
            else:
                service = build_service(
                    settings,
                    with_whatsapp=False,
                    invoice_stub=invoice_stub,
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
            elif args.command in payment_cmds:
                # show-payment/payments never send, so they get a service with no
                # WhatsApp client at all — which also means they work with only
                # the Daftra key, exactly like show/list.
                payment_service = build_payment_service(
                    settings,
                    with_whatsapp=args.command == "send-payment",
                    payment_stub=payment_stub,
                    meta_stub=meta_stub,
                )
                if args.command == "send-payment":
                    dry_run = args.dry_run or settings.dry_run
                    result = (
                        payment_service.send_freeform(
                            args.payment_id, to_phone=args.to, dry_run=dry_run
                        )
                        if args.freeform
                        else payment_service.send_payment(
                            args.payment_id, to_phone=args.to, dry_run=dry_run
                        )
                    )
                    if result.get("dry_run"):
                        print(json.dumps(result["payload"], indent=2))
                    else:
                        wamid = (
                            (result.get("response") or {}).get("messages") or [{}]
                        )[0].get("id", "?")
                        print(
                            f"Sent payment {result['payment'].number} to {result['to']} "
                            f"(wamid: {wamid})"
                        )
                elif args.command == "show-payment":
                    if args.raw:
                        print(
                            json.dumps(
                                payment_service.get_raw_payment(args.payment_id), indent=2
                            )
                        )
                    else:
                        payment = payment_service.get_payment(args.payment_id)
                        print(json.dumps(asdict(payment), indent=2, default=str))
                else:
                    for payment in payment_service.list_payments(args.limit):
                        # The payer column can be empty on a listing row — the
                        # customer only comes with the linked invoice — so it is
                        # allowed to print blank rather than pretending otherwise.
                        print(
                            f"{payment.number:<10} {payment.customer_name:<28} "
                            f"{payment.amount} {payment.currency:<5} "
                            f"invoice {payment.invoice_id or '-'}"
                        )
            elif args.command in customer_cmds:
                # Same shape as the payment commands: show/customers never send, so
                # they get a service with no WhatsApp client and work with only the
                # Daftra key.
                customer_service = build_customer_service(
                    settings,
                    with_whatsapp=args.command == "send-customer",
                    customer_stub=customer_stub,
                    meta_stub=meta_stub,
                )
                if args.command == "send-customer":
                    dry_run = args.dry_run or settings.dry_run
                    result = (
                        customer_service.send_freeform(
                            args.customer_id, to_phone=args.to, dry_run=dry_run
                        )
                        if args.freeform
                        else customer_service.send_customer(
                            args.customer_id, to_phone=args.to, dry_run=dry_run
                        )
                    )
                    if result.get("dry_run"):
                        print(json.dumps(result["payload"], indent=2))
                    else:
                        wamid = (
                            (result.get("response") or {}).get("messages") or [{}]
                        )[0].get("id", "?")
                        print(
                            f"Sent customer {result['customer'].number} to {result['to']} "
                            f"(wamid: {wamid})"
                        )
                elif args.command == "show-customer":
                    if args.raw:
                        print(
                            json.dumps(
                                customer_service.get_raw_customer(args.customer_id), indent=2
                            )
                        )
                    else:
                        customer = customer_service.get_customer(args.customer_id)
                        print(json.dumps(asdict(customer), indent=2, default=str))
                else:
                    for customer in customer_service.list_customers(args.limit):
                        # A client with no usable number still exists, and the
                        # poller would skip it — so print the gap rather than
                        # pretending the row is deliverable.
                        print(
                            f"{customer.number:<10} {customer.customer_name:<28} "
                            f"{customer.customer_phone or '-':<16} "
                            f"since {customer.created or '-'}"
                        )
    except (ApiError, ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0