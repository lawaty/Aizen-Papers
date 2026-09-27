"""Tests for the attachment mode plumbing: config, CLI flags, and the builder.

The provider itself is covered in ``test_invoice_attachment.py``; what matters
here is that the *default* is the upload path, that an operator can turn it off,
and that a failure to attach degrades to a message without a header instead of
failing the notification.
"""

from __future__ import annotations

import pytest

from sender.application.services import InvoiceNotificationService
from sender.domain.attachments import HostedLinkProvider, NoAttachmentProvider
from sender.domain.errors import WhatsAppApiError
from sender.domain.models import Invoice, InvoiceItem
from sender.domain.ports import InvoiceAttachmentProvider
from sender.domain.templates import LegacyInvoiceTemplateBuilder
from sender.infrastructure.attachments import UploadedMediaProvider
from sender.infrastructure.config import (
    ATTACH_LINK,
    ATTACH_NONE,
    ATTACH_UPLOAD,
    Settings,
    _env_attachment,
)
from sender.infrastructure.whatsapp.media import MetaMediaUploader
from tests.fakes import CapturingSender, FakeResponse, FakeSession, make_stub_invoice

BASE_ENV = {
    "DAFTRA_API_KEY": "k",
    "WHATSAPP_ACCESS_TOKEN": "t",
    "WHATSAPP_PHONE_NUMBER_ID": "123",
}


def settings_with(**env) -> Settings:
    return Settings.from_env({**BASE_ENV, **env}, require_daftra=False, require_whatsapp=False)


class StubUploader:
    def __init__(self, media_id: str = "MID", error: Exception | None = None) -> None:
        self.media_id = media_id
        self.error = error
        self.calls: list[tuple[bytes, str, str | None]] = []

    def upload_pdf(self, pdf: bytes, filename: str, *, cache_key: str | None = None) -> str:
        self.calls.append((pdf, filename, cache_key))
        if self.error:
            raise self.error
        return self.media_id


class ExplodingProvider:
    mode = "upload"

    def build(self, invoice: Invoice):
        raise RuntimeError("media service is down")


# --- configuration ---------------------------------------------------------------


def test_upload_is_the_default_attachment_mode() -> None:
    assert settings_with().invoice_attachment == ATTACH_UPLOAD
    assert Settings.from_env(BASE_ENV, require_daftra=False).invoice_attachment == ATTACH_UPLOAD


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("upload", ATTACH_UPLOAD),
        ("UPLOAD", ATTACH_UPLOAD),
        ("link", ATTACH_LINK),
        ("hosted", ATTACH_LINK),
        ("url", ATTACH_LINK),
        ("none", ATTACH_NONE),
        ("off", ATTACH_NONE),
        ("", ATTACH_UPLOAD),
    ],
)
def test_the_env_value_is_parsed_and_aliased(raw: str, expected: str) -> None:
    assert _env_attachment({"INVOICE_ATTACHMENT": raw}, "INVOICE_ATTACHMENT", ATTACH_UPLOAD) == expected
    assert settings_with(INVOICE_ATTACHMENT=raw).invoice_attachment == expected


def test_a_misspelled_mode_is_rejected_rather_than_silently_defaulted() -> None:
    """``uplod`` must not quietly downgrade to a mode nobody asked for."""
    with pytest.raises(RuntimeError, match="Invalid INVOICE_ATTACHMENT"):
        settings_with(INVOICE_ATTACHMENT="uplod")


def test_the_caption_is_read_from_the_environment() -> None:
    assert settings_with(INVOICE_ATTACHMENT_CAPTION="  Your invoice  ").invoice_attachment_caption == "Your invoice"
    assert settings_with().invoice_attachment_caption == ""


# --- builder wiring --------------------------------------------------------------


def test_the_builder_uses_the_provider_document_when_one_is_given() -> None:
    provider = UploadedMediaProvider(StubUploader("MID-1"))
    builder = LegacyInvoiceTemplateBuilder("t", "en", "20", attachment=provider)

    header = builder.header_parameter(make_stub_invoice())

    assert header == [{"type": "document", "document": {"id": "MID-1", "filename": "INV-001.pdf"}}]
    assert builder.attachment is provider
    assert builder.attachment_mode == "upload"


def test_without_a_provider_the_builder_still_falls_back_to_a_link() -> None:
    """The pre-attachment behaviour, kept intact for existing callers."""
    builder = LegacyInvoiceTemplateBuilder("t", "en", "20")
    assert builder.header_parameter(make_stub_invoice())[0]["document"]["link"]
    assert builder.attachment is None
    assert builder.attachment_mode is None


def test_a_provider_without_a_url_omits_the_header_instead_of_failing() -> None:
    builder = LegacyInvoiceTemplateBuilder(
        "t", "en", "20", attachment=HostedLinkProvider()
    )
    assert builder.header_parameter(make_stub_invoice(public_url=None, pdf_url=None)) == []


def test_a_failing_provider_degrades_to_a_header_less_message(caplog) -> None:
    """A media outage must not stop invoices from being notified."""
    builder = LegacyInvoiceTemplateBuilder("t", "en", "20", attachment=ExplodingProvider())
    with caplog.at_level("WARNING"):
        assert builder.header_parameter(make_stub_invoice()) == []
    assert any("media service is down" in r.message for r in caplog.records)

    payload = builder.build(make_stub_invoice(), "201000000000")
    assert payload["template"]["components"][0]["type"] == "body"


def test_the_provider_only_renders_a_pdf_for_invoices_that_need_one() -> None:
    uploader = StubUploader()
    builder = LegacyInvoiceTemplateBuilder(
        "t", "en", "20", attachment=UploadedMediaProvider(uploader)
    )
    builder.build(make_stub_invoice(), "201000000000")
    assert len(uploader.calls) == 1
    pdf, filename, cache_key = uploader.calls[0]
    assert pdf.startswith(b"%PDF-1.4")
    assert filename == "INV-001.pdf"
    assert cache_key and "INV-001" in cache_key


def test_a_changed_invoice_gets_a_different_cache_key() -> None:
    provider = UploadedMediaProvider(StubUploader())
    first = provider.build(make_stub_invoice())
    second = provider.build(make_stub_invoice(total=None))
    assert first["id"] == second["id"]  # the fake uploader always answers the same
    keys = [call[2] for call in StubUploader().calls]
    assert keys == []
    uploader = StubUploader()
    UploadedMediaProvider(uploader).build(make_stub_invoice())
    UploadedMediaProvider(uploader).build(make_stub_invoice(total=None))
    assert uploader.calls[0][2] != uploader.calls[1][2]


# --- service level ---------------------------------------------------------------


def test_a_media_upload_failure_is_surfaced_as_a_whatsapp_api_error() -> None:
    """The poller classifies from ``status``; an upload failure must look like any
    other transient Meta failure so it is retried instead of abandoned."""
    from sender.infrastructure.whatsapp.media import MetaMediaUploader

    session = FakeSession(
        {"post": [FakeResponse(503, {"error": {"message": "temporarily unavailable"}})]}
    )
    uploader = MetaMediaUploader("t", "123", "v25.0", session=session, max_retries=0)
    with pytest.raises(WhatsAppApiError) as excinfo:
        uploader.upload_pdf(b"%PDF-", "a.pdf")
    assert excinfo.value.status == 503


def test_the_notification_service_builds_a_template_with_an_uploaded_document() -> None:
    sender = CapturingSender()
    uploader = StubUploader("MID-CLI")
    service = InvoiceNotificationService(
        source=_StubSource(),
        sender=sender,
        builder=LegacyInvoiceTemplateBuilder(
            "t", "en", "20", attachment=UploadedMediaProvider(uploader)
        ),
    )

    service.send_invoice("1")

    assert sender.calls[0]["template"]["components"][0]["parameters"][0]["document"]["id"] == "MID-CLI"
    assert uploader.calls


def test_attachment_mode_none_sends_without_a_header() -> None:
    """``none`` means "attach nothing", which is not the same as configuring no
    provider at all (that keeps the historic link document)."""
    disabled = LegacyInvoiceTemplateBuilder(
        "t", "en", "20", attachment=NoAttachmentProvider()
    )
    assert disabled.attachment_mode == "none"
    assert disabled.header_parameter(make_stub_invoice()) == []
    payload = disabled.build(make_stub_invoice(), "201000000000")
    assert [c["type"] for c in payload["template"]["components"]] == ["body"]

    # No provider configured at all keeps the pre-existing link document.
    assert LegacyInvoiceTemplateBuilder("t", "en", "20").header_parameter(
        make_stub_invoice()
    )[0]["document"]["link"]


def test_the_cli_maps_the_none_mode_to_the_sentinel_provider() -> None:
    from sender.presentation.cli import _attachment_provider

    provider = _attachment_provider(settings_with(), ATTACH_NONE)
    assert isinstance(provider, NoAttachmentProvider)
    assert provider is not None


class _StubSource:
    def get_invoice(self, invoice_id):
        return make_stub_invoice()

    def get_raw_invoice(self, invoice_id):
        return {}

    def list_invoices(self, limit=10, page=1):
        return [make_stub_invoice()]


# --- CLI composition -------------------------------------------------------------


def test_the_cli_defaults_to_the_upload_provider() -> None:
    from sender.presentation.cli import _attachment_provider

    provider = _attachment_provider(settings_with(), None)
    assert isinstance(provider, UploadedMediaProvider)
    assert isinstance(provider._uploader, MetaMediaUploader)


def test_the_cli_honours_link_and_none() -> None:
    from sender.presentation.cli import _attachment_provider

    assert isinstance(_attachment_provider(settings_with(), ATTACH_LINK), HostedLinkProvider)
    assert isinstance(_attachment_provider(settings_with(), ATTACH_NONE), NoAttachmentProvider)


def test_an_unknown_attachment_mode_from_the_flag_is_rejected() -> None:
    from sender.presentation.cli import _attachment_provider

    with pytest.raises(RuntimeError, match="Unknown attachment mode"):
        _attachment_provider(settings_with(), "carrier-pigeon")


def test_stub_runs_stay_offline_unless_upload_is_explicit() -> None:
    """``--stub`` must not touch Meta for a PDF, but an explicit flag is honored."""
    from sender.presentation.cli import _attachment_mode_for

    settings = settings_with()
    assert _attachment_mode_for(settings, None, use_stub=True) == ATTACH_LINK
    assert _attachment_mode_for(settings, ATTACH_UPLOAD, use_stub=True) == ATTACH_UPLOAD
    assert _attachment_mode_for(settings, ATTACH_UPLOAD, use_stub=False) == ATTACH_UPLOAD


def test_send_preview_and_poll_all_expose_the_attachment_flag() -> None:
    from sender.presentation.cli import _build_parser

    parser = _build_parser()
    for command in ("send", "preview", "poll"):
        args = parser.parse_args([command, "--invoice-id", "1"] if command != "poll" else ["poll"])
        assert getattr(args, "attachment", None) is None
        chosen = parser.parse_args(
            [command, "--invoice-id", "1", "--attachment", "none"]
            if command != "poll"
            else ["poll", "--attachment", "link"]
        )
        assert chosen.attachment in ("none", "link")


def test_the_port_is_satisfied_by_both_providers() -> None:
    assert isinstance(HostedLinkProvider(), InvoiceAttachmentProvider)
    assert isinstance(UploadedMediaProvider(StubUploader()), InvoiceAttachmentProvider)
    assert isinstance(StubUploader(), InvoiceAttachmentProvider) is False


def test_the_stub_fixture_still_builds_a_document() -> None:
    """The offline stub invoice has no pdf_url, so the link fallback is exercised."""
    invoice = make_stub_invoice()
    assert invoice.pdf_url is None
    assert HostedLinkProvider().build(invoice)["filename"] == f"{invoice.number}.pdf"


def test_an_invoice_item_placeholder_is_not_needed_for_the_document() -> None:
    """Guards against the document shape drifting when items are added to the PDF."""
    invoice = make_stub_invoice(
        items=(InvoiceItem(name="Widget", quantity=1, unit_price=1, total=1),)
    )
    assert HostedLinkProvider().build(invoice) is not None


def test_the_poll_flow_attaches_the_uploaded_document(tmp_path) -> None:
    """The poller must attach the PDF too, not just the manual send path."""
    from sender.application.poller import InvoicePoller, PollApp
    from sender.infrastructure.clock import SystemClock
    from sender.infrastructure.state import JsonPollStateStore

    sender = CapturingSender()
    uploader = StubUploader("MID-POLL")
    builder = LegacyInvoiceTemplateBuilder(
        "t", "en", "20", attachment=UploadedMediaProvider(uploader)
    )
    source = _StubSource()
    state = JsonPollStateStore(str(tmp_path / "poll.json"))
    poller = InvoicePoller(
        apps=[PollApp(name="stub", source=source)],
        sender=sender,
        builder=builder,
        state=state,
        clock=SystemClock(),
        interval=60.0,
        limit=10,
        send_existing=True,
        once=True,
        country_code="20",
    )
    summary = poller.run()
    assert summary["apps"][0]["sent"] == 1
    header = sender.payloads[0]["template"]["components"][0]
    assert header["type"] == "header"
    assert header["parameters"][0]["document"]["id"] == "MID-POLL"
    assert uploader.calls
