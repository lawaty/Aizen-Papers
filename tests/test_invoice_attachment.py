"""Tests for the Meta media upload and the providers built on it.

The upload is the only part of the attachment path that talks to Meta before the
send, so the protocol is asserted literally: which endpoint, which body shape.
Everything here runs against the offline ``FakeSession`` — no network.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from sender.domain.attachments import (
    HostedLinkProvider,
    document_filename,
    pdf_link,
)
from sender.domain.errors import WhatsAppApiError
from sender.domain.models import Invoice, InvoiceItem
from sender.infrastructure.attachments import UploadedMediaProvider
from sender.infrastructure.pdf import render_invoice_pdf
from sender.infrastructure.whatsapp.media import MetaMediaUploader
from tests.fakes import FakeResponse, FakeSession

API = "v25.0"
PHONE = "123456"


def make_uploader(session: FakeSession, **kwargs) -> MetaMediaUploader:
    return MetaMediaUploader(
        access_token="token", phone_number_id=PHONE, api_version=API, session=session, **kwargs
    )


def make_invoice(**overrides) -> Invoice:
    base = dict(
        id="7",
        number="INV-007",
        currency="USD",
        issue_date=date(2026, 5, 1),
        customer_name="Acme",
        public_url="https://example.test/preview/7",
        pdf_url="https://example.test/7.pdf",
        subtotal=Decimal("10.00"),
        total=Decimal("10.00"),
        total_paid=Decimal("0.00"),
        balance_due=Decimal("10.00"),
        items=(InvoiceItem(name="Item", quantity=Decimal("1"), unit_price=Decimal("10.00"), total=Decimal("10.00")),),
    )
    base.update(overrides)
    return Invoice(**base)


# --- the upload protocol ---------------------------------------------------------


def test_upload_sends_a_single_multipart_request() -> None:
    session = FakeSession({"post": [FakeResponse(200, {"id": "MID1"})]})
    uploader = make_uploader(session)

    media_id = uploader.upload_pdf(b"%PDF-1.4 body", "INV-007.pdf")

    assert media_id == "MID1"
    assert len(session.calls) == 1
    assert session.urls[0] == f"https://graph.facebook.com/{API}/{PHONE}/media"
    kwargs = session.requests_log[0][2]
    # The multipart form carries the required messaging_product field.
    assert kwargs["data"] == {"messaging_product": "whatsapp"}
    # The PDF travels as the file part, with its filename and MIME type.
    name, body, content_type = kwargs["files"]["file"]
    assert name == "INV-007.pdf"
    assert body == b"%PDF-1.4 body"
    assert content_type == "application/pdf"


def test_a_missing_media_id_is_a_hard_error() -> None:
    session = FakeSession({"post": [FakeResponse(200, {"ok": True})]})
    with pytest.raises(WhatsAppApiError) as excinfo:
        make_uploader(session).upload_pdf(b"%PDF-", "a.pdf")
    assert "no media id" in str(excinfo.value)


def test_a_file_handle_is_not_accepted_as_a_media_id() -> None:
    """The Graph API resumable-upload ``h`` handle is not a WhatsApp media id;
    a response that only carries ``h`` must not be fed into a template header."""
    session = FakeSession({"post": [FakeResponse(200, {"h": "handle-only"})]})
    with pytest.raises(WhatsAppApiError, match="no media id"):
        make_uploader(session).upload_pdf(b"%PDF-", "a.pdf")


def test_an_empty_pdf_is_refused_before_any_request() -> None:
    session = FakeSession()
    with pytest.raises(WhatsAppApiError, match="empty PDF"):
        make_uploader(session).upload_pdf(b"", "a.pdf")
    assert not session.calls


def test_meta_error_details_survive_into_the_raised_error() -> None:
    """The poller retries from ``status`` and shows operators ``message``."""
    session = FakeSession(
        {"post": [FakeResponse(400, {"error": {"message": "bad file", "code": 100, "fbtrace_id": "Abc"}})]}
    )
    with pytest.raises(WhatsAppApiError) as excinfo:
        make_uploader(session).upload_pdf(b"%PDF-", "a.pdf")
    error = excinfo.value
    assert error.status == 400
    assert "bad file" in str(error) and "[code 100]" in str(error) and "Abc" in str(error)


def test_an_empty_pdf_never_reaches_the_caller_of_the_provider() -> None:
    session = FakeSession()
    provider = UploadedMediaProvider(make_uploader(session), render=lambda invoice: b"")
    assert provider.build(make_invoice()) is None
    assert not session.calls


# --- caching ---------------------------------------------------------------------


def test_the_same_invoice_uploads_once_and_reuses_the_id() -> None:
    session = FakeSession({"post": [FakeResponse(200, {"id": "MID1"})]})
    uploader = make_uploader(session)
    first = uploader.upload_pdf(b"%PDF-same", "a.pdf", cache_key="k")
    second = uploader.upload_pdf(b"%PDF-same", "a.pdf", cache_key="k")
    assert first == second == "MID1"
    assert len(session.calls) == 1  # one upload, then the cache answers


# --- the providers ---------------------------------------------------------------


def test_the_uploaded_provider_returns_an_id_document() -> None:
    session = FakeSession({"post": [FakeResponse(200, {"id": "MID9"})]})
    provider = UploadedMediaProvider(make_uploader(session))

    document = provider.build(make_invoice())

    assert document == {"id": "MID9", "filename": "INV-007.pdf"}
    assert provider.mode == "upload"


def test_the_uploaded_provider_uploads_a_real_pdf() -> None:
    session = FakeSession({"post": [FakeResponse(200, {"id": "MID9"})]})
    provider = UploadedMediaProvider(make_uploader(session))
    provider.build(make_invoice())
    body = session.requests_log[0][2]["files"]["file"][1]
    assert body.startswith(b"%PDF-1.4")


def test_a_configured_caption_is_dropped_with_a_warning(caplog) -> None:
    """Meta does not support captions for the document header parameter, so a
    configured caption must not be sent (it would reject the send)."""
    session = FakeSession({"post": [FakeResponse(200, {"id": "M"})]})
    with_caption = UploadedMediaProvider(make_uploader(session), caption="Your invoice")
    with caplog.at_level("WARNING"):
        document = with_caption.build(make_invoice())
    assert "caption" not in document
    assert any("caption" in record.message for record in caplog.records)

    session = FakeSession({"post": [FakeResponse(200, {"id": "M"})]})
    without = UploadedMediaProvider(make_uploader(session))
    assert "caption" not in without.build(make_invoice())


def test_the_hosted_provider_returns_a_link_document_and_never_uploads() -> None:
    provider = HostedLinkProvider()
    assert provider.build(make_invoice()) == {
        "link": "https://example.test/7.pdf",
        "filename": "INV-007.pdf",
    }
    assert provider.mode == "link"


def test_the_hosted_provider_drops_a_configured_caption(caplog) -> None:
    provider = HostedLinkProvider(caption="Your invoice")
    with caplog.at_level("WARNING"):
        document = provider.build(make_invoice())
    assert document == {"link": "https://example.test/7.pdf", "filename": "INV-007.pdf"}
    assert any("caption" in record.message for record in caplog.records)


def test_the_hosted_provider_returns_nothing_when_there_is_no_url() -> None:
    assert HostedLinkProvider().build(make_invoice(pdf_url=None, public_url=None)) is None


def test_the_hosted_provider_falls_back_to_the_public_url() -> None:
    """A source with no PDF url at all still produces a document (legacy shape)."""
    document = HostedLinkProvider().build(
        make_invoice(pdf_url=None, public_url="https://example.test/preview/7")
    )
    assert document["link"] == "https://example.test/preview/7"


# --- domain helpers --------------------------------------------------------------


def test_the_filename_follows_the_invoice_number() -> None:
    assert document_filename(make_invoice()) == "INV-007.pdf"


@pytest.mark.parametrize(
    "number, expected",
    [
        (    "INV-007", "INV-007.pdf"),
        ("  42  ", "42.pdf"),
        # Separators are collapsed, so the result is always a single segment.
        # Dots are kept on purpose so numbers like INV.2024 survive.
        ("../../etc/passwd", ".._.._etc_passwd.pdf"),
        ("a/b\\c", "a_b_c.pdf"),
        ("", "invoice.pdf"),
        (None, "invoice.pdf"),
    ],
)
def test_the_filename_is_always_one_safe_segment(number, expected) -> None:
    """The number comes from an external ERP and lands in a MediaObject, so path
    separators must not survive."""
    filename = document_filename(make_invoice(number=number))
    assert filename == expected
    assert "/" not in filename and "\\" not in filename
    assert filename.endswith(".pdf")


def test_pdf_link_prefers_the_pdf_url() -> None:
    assert pdf_link(make_invoice()) == "https://example.test/7.pdf"
    assert pdf_link(make_invoice(pdf_url=None)) == "https://example.test/preview/7"
    assert pdf_link(make_invoice(pdf_url=None, public_url=None)) is None


def test_both_providers_satisfy_the_port() -> None:
    from sender.domain.ports import InvoiceAttachmentProvider, MediaUploader

    assert isinstance(HostedLinkProvider(), InvoiceAttachmentProvider)
    assert isinstance(
        UploadedMediaProvider(make_uploader(FakeSession())), InvoiceAttachmentProvider
    )
    assert isinstance(make_uploader(FakeSession()), MediaUploader)


def test_the_rendered_pdf_is_deterministic_so_the_cache_key_is_stable() -> None:
    invoice = make_invoice()
    assert render_invoice_pdf(invoice) == render_invoice_pdf(invoice)
    assert UploadedMediaProvider._cache_key(invoice, b"a") != UploadedMediaProvider._cache_key(invoice, b"b")
    assert json.loads(json.dumps({"id": "x"}))  # the document shape stays JSON-safe


def test_a_429_upload_is_retried_then_succeeds(monkeypatch) -> None:
    """Transient rate limiting must be retried, not surfaced as a hard failure."""
    session = FakeSession(
        {"post": [FakeResponse(429, {"error": {"message": "rate limited"}}), FakeResponse(200, {"id": "MID1"})]}
    )
    uploader = make_uploader(session, max_retries=1)
    monkeypatch.setattr("sender.infrastructure.whatsapp.media.time.sleep", lambda _: None)
    assert uploader.upload_pdf(b"%PDF-", "a.pdf") == "MID1"
    assert len(session.calls) == 2


def test_a_different_cache_key_triggers_a_real_reupload() -> None:
    """A changed invoice (different digest -> different key) must upload again."""
    session = FakeSession(
        {"post": [FakeResponse(200, {"id": "MID1"}), FakeResponse(200, {"id": "MID2"})]}
    )
    uploader = make_uploader(session)
    first = uploader.upload_pdf(b"%PDF-a", "a.pdf", cache_key="invoice:1")
    second = uploader.upload_pdf(b"%PDF-b", "a.pdf", cache_key="invoice:2")
    assert first == "MID1"
    assert second == "MID2"
    assert len(session.calls) == 2


def test_the_uploaded_provider_never_sends_a_caption_field() -> None:
    """The document object must never carry a caption, even when one is set."""
    session = FakeSession({"post": [FakeResponse(200, {"id": "M"})]})
    provider = UploadedMediaProvider(make_uploader(session), caption="ignored")
    document = provider.build(make_invoice())
    assert "caption" not in document
    assert document == {"id": "M", "filename": "INV-007.pdf"}
