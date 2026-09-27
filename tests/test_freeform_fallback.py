"""Offline specs for the free-form fallback that bridges an unusable template.

The live WABA has no approved ``aizen_invoice`` template in the ``en``
translation, so every real send comes back with Meta's 132001. These specs pin
what the poller does about it: reuse the media id the rejected send already
uploaded, fall back to plain text when there is none, and never silently
consume an invoice while the template is broken.
"""

import pytest

from sender.application.poller import InvoicePoller, PollApp
from sender.domain.errors import WhatsAppApiError, is_template_error
from sender.domain.templates import (
    CleanTextTemplateBuilder,
    LegacyInvoiceTemplateBuilder,
    build_fallback_document,
)
from sender.infrastructure.attachments import UploadedMediaProvider
from sender.infrastructure.config import Settings
from sender.infrastructure.state import InMemoryPollStateStore
from sender.presentation.cli import _format_poll_summary
from sender.presentation.stubs import StubInvoiceSource, make_stub_invoice


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


class CountingUploader:
    """A ``MediaUploader`` that records every upload, so a test can prove the
    PDF was rendered and uploaded exactly once — the fallback must reference the
    media id the rejected template send already paid for, not pay again."""

    def __init__(self, media_id: str = "MEDIA-ID-1") -> None:
        self.media_id = media_id
        self.uploads: list[str] = []

    def upload_pdf(self, pdf: bytes, filename: str, *, cache_key: str | None = None) -> str:
        self.uploads.append(filename)
        return self.media_id


def _fake_render(invoice) -> bytes:
    return b"%PDF-1.4 fake"


def _meta_error(code: int, message: str) -> WhatsAppApiError:
    """A rejected send shaped the way the Cloud API reports one.

    Meta appends the code to the message it returns, so the recorded reason in the
    state file carries it too — the test double mirrors that instead of inventing
    a message the real API would never produce.
    """
    text = f"{message} (#{code})"
    return WhatsAppApiError(400, text, {"error": {"code": code, "message": text}})


def _template_error() -> WhatsAppApiError:
    """132001: the template (or its translation) is not usable — the live case."""
    return _meta_error(132001, "Template does not exist for the language en")


def _out_of_window_error() -> WhatsAppApiError:
    """131047: the free-form send is outside the 24-hour customer service window."""
    return _meta_error(131047, "Re-engagement message (24h window closed)")


class TemplateErrorSender:
    """Rejects the template send, then handles the fallback.

    ``fallback_error`` makes the fallback send fail too. Every payload is kept,
    in order, so a test can assert both the shape and the number of attempts.
    """

    def __init__(self, template_error: Exception, fallback_error: Exception | None = None) -> None:
        self.template_error = template_error
        self.fallback_error = fallback_error
        self.payloads: list[dict] = []

    def send(self, payload: dict) -> dict:
        self.payloads.append(payload)
        if len(self.payloads) == 1:
            raise self.template_error
        if self.fallback_error is not None:
            raise self.fallback_error
        return {"messages": [{"id": "wamid.FALLBACK"}]}


def _app(invoice=None) -> PollApp:
    return PollApp("app1", StubInvoiceSource([invoice or make_stub_invoice()]))


def _poller(apps, sender, state=None, builder=None, **kwargs) -> InvoicePoller:
    return InvoicePoller(
        apps=apps,
        sender=sender,
        builder=builder or LegacyInvoiceTemplateBuilder("aizen_invoice", "en", "20"),
        state=state or InMemoryPollStateStore(),
        clock=FakeClock(),
        **kwargs,
    )


# --- the document fallback reuses the media id the rejected send uploaded ---


def test_template_error_falls_back_to_the_document_the_template_send_already_uploaded():
    uploader = CountingUploader()
    sender = TemplateErrorSender(_template_error())
    state = InMemoryPollStateStore()
    poller = _poller(
        [_app()],
        sender,
        state=state,
        builder=LegacyInvoiceTemplateBuilder(
            "aizen_invoice", "en", "20",
            attachment=UploadedMediaProvider(uploader, render=_fake_render),
        ),
        send_existing=True,
    )
    summary = poller.run_once()
    app = summary["apps"][0]

    assert len(sender.payloads) == 2
    assert sender.payloads[0]["type"] == "template"
    # The fallback is a plain document message, and its id is the media id the
    # rejected template send uploaded — no caption (unverified for free-form
    # documents, and the template header drops one for the same reason).
    assert sender.payloads[1] == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "201027693262",
        "type": "document",
        "document": {"id": "MEDIA-ID-1", "filename": "INV-001.pdf"},
    }
    assert "caption" not in sender.payloads[1]["document"]
    # Re-rendering and re-uploading would be a second wasted upload per invoice.
    assert uploader.uploads == ["INV-001.pdf"]
    assert state.seen("app1", "1") is True
    assert (app["sent"], app["fallback_sends"]) == (1, 1)
    assert (app["failed"], app["abandoned"], app["pending"]) == (0, 0, 0)
    assert app["invoices"][0]["fallback_kind"] == "document"
    assert app["invoices"][0]["status"] == "sent"


def test_the_document_fallback_is_built_by_the_domain_from_the_header_document():
    assert build_fallback_document("201027693262", {"id": "M1", "filename": "INV-001.pdf"}) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "201027693262",
        "type": "document",
        "document": {"id": "M1", "filename": "INV-001.pdf"},
    }


# --- no reusable header document -> the existing free-form text payload ---


def test_template_error_without_a_header_document_falls_back_to_the_freeform_text():
    invoice = make_stub_invoice()
    builder = CleanTextTemplateBuilder("aizen_invoice", "en", "20")
    sender = TemplateErrorSender(_template_error())
    state = InMemoryPollStateStore()
    poller = _poller([_app(invoice)], sender, state=state, builder=builder, send_existing=True)
    summary = poller.run_once()
    app = summary["apps"][0]

    assert len(sender.payloads) == 2
    fallback = sender.payloads[1]
    assert fallback["type"] == "text"
    assert "template" not in fallback
    assert fallback["to"] == "201027693262"
    # Byte-for-byte what `send --freeform` would have sent: one builder, one body.
    assert fallback == builder.build_text(invoice, "01027693262")
    assert "INV-001" in fallback["text"]["body"]
    assert state.seen("app1", "1") is True
    assert (app["sent"], app["fallback_sends"]) == (1, 1)
    assert app["invoices"][0]["fallback_kind"] == "text"


def test_a_link_only_header_document_is_not_reused_by_the_fallback():
    """Only an id we uploaded is safe to reference: a ``link`` document is a URL
    Meta has to fetch, and Daftra's own PDF url is session-gated, so the fallback
    degrades to text instead of repeating the failure."""
    builder = LegacyInvoiceTemplateBuilder("aizen_invoice", "en", "20")
    sender = TemplateErrorSender(_template_error())
    poller = _poller([_app()], sender, builder=builder, send_existing=True)
    poller.run_once()
    assert sender.payloads[0]["template"]["components"][0]["parameters"][0]["document"].keys() == {"link", "filename"}
    assert sender.payloads[1]["type"] == "text"


# --- the fallback is a bridge, never a place invoices go to die ---


def test_template_error_with_the_fallback_disabled_keeps_the_invoice_pending():
    sender = TemplateErrorSender(_template_error())
    state = InMemoryPollStateStore()
    poller = _poller([_app()], sender, state=state, send_existing=True, freeform_fallback=False)
    summary = poller.run_once()
    app = summary["apps"][0]

    assert len(sender.payloads) == 1  # no second attempt
    assert app["failed"] == 1
    assert app["fallback_sends"] == 0
    # Never abandoned and never marked seen: approving the template is an operator
    # action, and a bridge that quietly retired the invoice would be a silent loss.
    assert app["abandoned"] == 0
    assert state.seen("app1", "1") is False
    assert state.abandoned("app1") == {}
    pending = state.pending("app1")["1"]
    assert "132001" in pending["error"]
    assert pending["next_attempt_at"] > 0


def test_a_failing_fallback_keeps_the_invoice_pending_on_the_template_error(caplog):
    sender = TemplateErrorSender(_template_error(), fallback_error=_out_of_window_error())
    state = InMemoryPollStateStore()
    poller = _poller([_app()], sender, state=state, send_existing=True)
    with caplog.at_level("WARNING"):
        summary = poller.run_once()
    app = summary["apps"][0]

    assert len(sender.payloads) == 2
    assert app["failed"] == 1
    assert app["sent"] == 0
    assert app["abandoned"] == 0
    assert state.seen("app1", "1") is False
    # The template error is the recorded reason, not the 131047 the fallback hit.
    assert "132001" in state.pending("app1")["1"]["error"]
    assert any("132001" in record.message and "131047" in record.message for record in caplog.records)


def test_the_fallback_is_logged_loudly_because_the_customer_may_not_even_receive_it(caplog):
    sender = TemplateErrorSender(_template_error())
    poller = _poller([_app()], sender, send_existing=True)
    with caplog.at_level("WARNING"):
        poller.run_once()
    warnings = [record.message for record in caplog.records if record.levelname == "WARNING"]
    assert any("INV-001" in message and "132001" in message for message in warnings)
    assert any("24-hour" in message for message in warnings)


# --- errors that are not about the template keep their classification ---


def test_a_non_template_error_is_still_abandoned_and_never_retried():
    sender = TemplateErrorSender(_meta_error(131009, "Parameter value is not valid"))
    state = InMemoryPollStateStore()
    poller = _poller([_app()], sender, state=state, send_existing=True)
    summary = poller.run_once()
    app = summary["apps"][0]

    assert len(sender.payloads) == 1  # no fallback attempt
    assert app["abandoned"] == 1
    assert app["fallback_sends"] == 0
    assert state.seen("app1", "1") is True
    assert state.pending("app1") == {}
    assert "131009" in state.abandoned("app1")["1"]["error"]


def test_a_retryable_error_is_still_retried_without_a_fallback():
    sender = TemplateErrorSender(WhatsAppApiError(500, "boom"))
    state = InMemoryPollStateStore()
    poller = _poller([_app()], sender, state=state, send_existing=True)
    poller.run_once()
    assert len(sender.payloads) == 1
    assert state.seen("app1", "1") is False
    assert state.pending("app1")["1"]["count"] == 1


def test_dry_run_never_sends_anything_including_a_fallback():
    sender = TemplateErrorSender(_template_error())
    state = InMemoryPollStateStore()
    poller = _poller([_app()], sender, state=state, send_existing=True, dry_run=True)
    summary = poller.run_once()
    app = summary["apps"][0]

    assert sender.payloads == []
    assert app["would_send"] == 1
    assert app["fallback_sends"] == 0
    assert state.seen("app1", "1") is False
    assert state.pending("app1") == {}


# --- the summary reports the bridge ---


def test_the_poll_summary_surfaces_the_fallback_count():
    text = _format_poll_summary({
        "apps": [
            {
                "app": "app1", "ok": True, "error": None, "listed": 1, "new": 1,
                "sent": 1, "would_send": 0, "fallback_sends": 1, "skipped_no_phone": 0,
                "failed": 0, "pending": 0, "abandoned": 0, "first_run": False, "seeded": 0,
                "invoices": [
                    {
                        "number": "INV-001", "id": "1", "status": "sent", "to": "201027693262",
                        "fallback": True, "fallback_kind": "document", "error": "…[code 132001]",
                    },
                ],
            },
        ],
    })
    assert "1 via free-form fallback" in text
    assert "INV-001 -> 201027693262 sent via free-form document fallback" in text


def test_the_poll_summary_says_nothing_about_a_fallback_that_did_not_fire():
    text = _format_poll_summary({
        "apps": [
            {
                "app": "app1", "ok": True, "error": None, "listed": 1, "new": 1,
                "sent": 1, "would_send": 0, "fallback_sends": 0, "skipped_no_phone": 0,
                "failed": 0, "pending": 0, "abandoned": 0, "first_run": False, "seeded": 0,
                "invoices": [{"number": "INV-001", "id": "1", "status": "sent", "to": "201"}],
            },
        ],
    })
    assert "fallback" not in text


# --- the kill switch ---


def test_the_fallback_is_on_by_default_and_takes_the_usual_boolean_spellings():
    assert Settings.from_env({"DAFTRA_API_KEY": "k"}).wa_freeform_fallback is True
    assert Settings.from_env({"DAFTRA_API_KEY": "k", "WHATSAPP_FREEFORM_FALLBACK": ""}).wa_freeform_fallback is True
    assert Settings.from_env({"DAFTRA_API_KEY": "k", "WHATSAPP_FREEFORM_FALLBACK": " ON "}).wa_freeform_fallback is True
    assert Settings.from_env({"DAFTRA_API_KEY": "k", "WHATSAPP_FREEFORM_FALLBACK": "0"}).wa_freeform_fallback is False
    assert Settings.from_env({"DAFTRA_API_KEY": "k", "WHATSAPP_FREEFORM_FALLBACK": "off"}).wa_freeform_fallback is False


# --- what counts as a template-contract error ---


@pytest.mark.parametrize("code", [132000, 132001, 132012, 132999])
def test_the_132000_series_is_a_template_error(code):
    assert is_template_error(_meta_error(code, "template")) is True


@pytest.mark.parametrize(
    "exc",
    [
        _out_of_window_error(),
        _meta_error(133000, "just past the template class"),
        WhatsAppApiError(400, "no payload at all"),
        Exception("plain"),
    ],
)
def test_anything_else_is_not_a_template_error(exc):
    assert is_template_error(exc) is False


def test_a_payload_without_an_integer_code_is_not_a_template_error():
    assert is_template_error(WhatsAppApiError(400, "odd", {"error": {"code": "132001"}})) is False
    assert is_template_error(WhatsAppApiError(400, "odd", {"error": {}})) is False
