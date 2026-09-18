from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from typing import Sequence

from dotenv import load_dotenv

from sender.infrastructure.config import Settings
from sender.infrastructure.daftra.client import DaftraClient
from sender.infrastructure.whatsapp.client import WhatsAppClient
from sender.domain.errors import ApiError
from sender.domain.templates import InvoiceTemplateBuilder
from sender.application.services import InvoiceNotificationService
from sender.presentation.stubs import StubInvoiceSource, default_stub_invoices


def build_service(settings: Settings, with_whatsapp: bool = True, use_stub: bool = False) -> InvoiceNotificationService:
    whatsapp = (
        WhatsAppClient(
            access_token=settings.wa_access_token,
            phone_number_id=settings.wa_phone_number_id,
            api_version=settings.wa_api_version,
            timeout=settings.wa_timeout,
            default_country_code=settings.default_country_code,
        )
        if with_whatsapp
        else None
    )
    source = (
        StubInvoiceSource(default_stub_invoices())
        if use_stub
        else DaftraClient(
            api_key=settings.daftra_api_key,
            base_url=settings.daftra_base_url,
            timeout=settings.daftra_timeout,
        )
    )
    return InvoiceNotificationService(
        source=source,
        sender=whatsapp,
        builder=InvoiceTemplateBuilder(
            settings.wa_template_name, settings.wa_template_lang, settings.default_country_code
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

    preview = sub.add_parser("preview", help="Print the WhatsApp payload without sending")
    preview.add_argument("--invoice-id", required=True, help="Daftra invoice id")
    preview.add_argument("--to", help="Recipient phone number (defaults to the customer phone on the invoice)")
    preview.add_argument("--freeform", action="store_true", help="Preview as plain text instead of template")
    preview.add_argument("--stub", action="store_true", help="Use the offline stub invoice source instead of Daftra")

    show = sub.add_parser("show", help="Print a normalized invoice (use --raw for the Daftra JSON)")
    show.add_argument("--invoice-id", required=True, help="Daftra invoice id")
    show.add_argument("--raw", action="store_true", help="Print the raw Daftra JSON response")
    show.add_argument("--stub", action="store_true", help="Use the offline stub invoice source instead of Daftra")

    listing = sub.add_parser("list", help="List recent invoices")
    listing.add_argument("--limit", type=int, default=10, help="Number of invoices to list")
    listing.add_argument("--stub", action="store_true", help="Use the offline stub invoice source instead of Daftra")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    try:
        args = _build_parser().parse_args(argv)
        need_whatsapp = args.command in ("send", "preview")
        settings = Settings.from_env(require_whatsapp=need_whatsapp, require_daftra=not args.stub)
        logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
        service = build_service(settings, with_whatsapp=need_whatsapp, use_stub=args.stub)

        if args.command == "send":
            send_method = service.send_freeform if args.freeform else service.send_invoice
            result = send_method(
                args.invoice_id, to_phone=args.to, dry_run=args.dry_run or settings.dry_run
            )
            if result.get("dry_run"):
                print(json.dumps(result["payload"], indent=2))
            else:
                print(
                    f"Sent invoice {result['invoice'].number} to {result['to']} "
                    f"(wamid: {result['response']['messages'][0]['id']})"
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
    except (ApiError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0