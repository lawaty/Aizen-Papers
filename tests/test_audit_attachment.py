"""Offline pins for the template-approval / attachment half of the audit.

Three properties, all pinned against the current code:

1. With the approved TEXT-header template (``header=TEXT;header_vars=0;
   body_vars=4;footer=True``, which is what ``template_state.json`` records as
   ``active_builder: "new"``) the registry hands back the body-only builder and
   warns that the attachment provider is discarded: nothing is rendered and
   nothing is uploaded, so a clean-approval costs zero media calls.
2. The same registry auto-selects the legacy document-header builder (and
   attaches the uploaded media id) the moment the approved template has a
   DOCUMENT header — no production change, no config change, no redeploy.
3. The *other* swap path (``cli._other_builder``, the one a template-error
   retry takes) carries the provider across, because the clean builder it is
   handed was built with it. The retry therefore still emits the uploaded PDF
   by its media id instead of degrading to a session-gated Daftra ``link``.

Everything is offline: the Meta Graph API is a fake session, the attachment
provider is a local recorder, and no message is ever sent.
"""

from fakes import FakeResponse, FakeSession
from sender.domain.attachments import pdf_link
from sender.domain.templates import CleanTextTemplateBuilder, LegacyInvoiceTemplateBuilder
from sender.infrastructure.config import Settings
from sender.infrastructure.whatsapp.template_registry import TemplateRegistry
from sender.presentation.stubs import make_stub_invoice


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RecordingAttachmentProvider:
    """A local ``InvoiceAttachmentProvider`` that records every ``build()``.

    Returns the media id an upload would have produced, so the payload shape can
    be asserted, while counting the calls — which is the whole point: proving a
    provider is (or is not) consulted at all.
    """

    mode = "upload"

    def __init__(self, document: dict | None = None) -> None:
        self.document = document if document is not None else {"id": "MID-AUDIT", "filename": "INV.pdf"}
        self.calls: list[str] = []

    def build(self, invoice):
        self.calls.append(invoice.number)
        return dict(self.document)


#: The approved revision that is live: a TEXT header with no placeholders, a
#: four-variable body, and a footer — i.e. the clean structure, fingerprint
#: ``header=TEXT;header_vars=0;body_vars=4;footer=True``.
APPROVED_TEXT_COMPONENTS = [
    {"type": "HEADER", "format": "TEXT", "text": "فاتورة مبيعات | Aizen Paper"},
    {
        "type": "BODY",
        "text": "مرحبا {{1}}، رقم {{2}}، تاريخ {{3}}، إجمالي {{4}}",
        "example": {"body_text": [["Print Home", "18A", "18/09/2026", "2,500"]]},
    },
    {"type": "FOOTER", "text": "Aizen Paper"},
]

#: The same template re-approved with a DOCUMENT header (what re-approving the
#: old PDF-attaching structure looks like to the Graph API).
APPROVED_DOCUMENT_COMPONENTS = [
    {"type": "HEADER", "format": "DOCUMENT", "example": {"header_handle": ["https://x/INV-900.pdf"]}},
    APPROVED_TEXT_COMPONENTS[1],
    APPROVED_TEXT_COMPONENTS[2],
]


def _approved(components: list[dict]) -> dict:
    return {
        "data": [
            {
                "name": "aizen_invoice",
                "status": "APPROVED",
                "language": "en",
                "components": components,
            }
        ]
    }


def _registry(components: list[dict], path) -> TemplateRegistry:
    session = FakeSession({"get": FakeResponse(200, _approved(components))})
    return TemplateRegistry(
        access_token="tok",
        waba_id="waba123",
        api_version="v25.0",
        template_name="aizen_invoice",
        language="en",
        timeout=5.0,
        session=session,
        cache_path=str(path),
        ttl_seconds=300.0,
        clock=FakeClock(),
    )


def _components(payload: dict) -> list[dict]:
    return payload["template"]["components"]


def _types(payload: dict) -> list[str]:
    return [component["type"] for component in _components(payload)]


# --- 1. approved clean template: body only, zero uploads ------------------------


def test_approved_text_template_builds_body_only_and_never_uploads(tmp_path):
    """The live approval makes the attachment path dead code: no PDF is rendered
    and nothing is uploaded, because the clean template has no header document to
    put a media id in. The provider handed to ``choose_builder`` is retained on
    the builder (so a later builder swap can carry it) but ``build()`` never
    consults it, so this costs zero media calls per invoice."""
    registry = _registry(APPROVED_TEXT_COMPONENTS, tmp_path / "template_state.json")
    provider = RecordingAttachmentProvider()

    snapshot = registry.current()
    # The fingerprint pinned here is the one recorded in the repo's
    # template_state.json for the approved aizen_invoice.
    assert snapshot["status"] == "APPROVED"
    assert snapshot["fingerprint"] == "header=TEXT;header_vars=0;body_vars=4;footer=True"
    assert snapshot["active_builder"] == "new"

    builder = registry.choose_builder("aizen_invoice", "en", "20", attachment=provider)
    assert isinstance(builder, CleanTextTemplateBuilder)
    assert builder.attachment is provider

    payload = builder.build(make_stub_invoice(id="900", number="INV-900"), "20127693262")

    assert _types(payload) == ["body"]
    assert len(_components(payload)) == 1
    # No header component anywhere in the payload, in any spelling.
    assert all(component["type"] != "header" for component in _components(payload))
    assert [p for p in _components(payload) if p["type"] == "header"] == []
    # Neither render nor upload ever happened.
    assert provider.calls == []


def test_choose_builder_warns_that_the_clean_template_discards_the_attachment(tmp_path, caplog):
    """The discard is a deliberate, logged decision, not a silent one.

    An operator who configured ``INVOICE_ATTACHMENT=upload`` and then approved a
    text-header template would otherwise see the mode reported on startup and
    never learn that no PDF is attached any more.
    """
    registry = _registry(APPROVED_TEXT_COMPONENTS, tmp_path / "template_state.json")
    provider = RecordingAttachmentProvider()

    with caplog.at_level("WARNING"):
        builder = registry.choose_builder("aizen_invoice", "en", "20", attachment=provider)

    assert isinstance(builder, CleanTextTemplateBuilder)
    assert any(
        "aizen_invoice" in record.message and "discarded" in record.message
        for record in caplog.records
    )
    # Still body only: discarding the provider must not have added a header.
    payload = builder.build(make_stub_invoice(id="900", number="INV-900"), "20127693262")
    assert [component["type"] for component in _components(payload)] == ["body"]
    assert provider.calls == []


# --- 2. approved document header: auto-switch, PDF attached ---------------------


def test_approved_document_header_template_selects_legacy_builder_and_attaches_pdf(tmp_path):
    """Re-approving a DOCUMENT-header template needs ZERO production code
    changes: the same registry, given the same attachment provider, returns the
    legacy builder and the PDF is attached by the media id we uploaded. This is
    the rollback path if the clean template has to be abandoned."""
    registry = _registry(APPROVED_DOCUMENT_COMPONENTS, tmp_path / "template_state.json")
    provider = RecordingAttachmentProvider()

    snapshot = registry.current()
    assert snapshot["status"] == "APPROVED"
    assert snapshot["fingerprint"] == "header=DOCUMENT;header_vars=0;body_vars=4;footer=True"
    assert snapshot["active_builder"] == "legacy"

    builder = registry.choose_builder("aizen_invoice", "en", "20", attachment=provider)
    assert isinstance(builder, LegacyInvoiceTemplateBuilder)
    assert builder.attachment is provider

    payload = builder.build(make_stub_invoice(id="900", number="INV-900"), "20127693262")

    assert _types(payload) == ["header", "body"]
    header = _components(payload)[0]
    assert header["type"] == "header"
    assert header["parameters"] == [
        {"type": "document", "document": {"id": "MID-AUDIT", "filename": "INV.pdf"}}
    ]
    # The uploaded media id, never a link: Meta is handed an asset we own.
    assert "link" not in header["parameters"][0]["document"]
    assert provider.calls == ["INV-900"]


# --- 3. the retry swap carries the attachment across -----------------------------


def test_builder_swap_from_clean_to_legacy_carries_the_attachment_across(tmp_path):
    """``cli._other_builder`` is careful to carry the attachment across the swap
    ("the retry has to produce the same kind of header document the first
    attempt did") — and it can, because ``template_registry.choose_builder``
    hands the clean builder the same provider. So the rescue send still attaches
    the PDF we uploaded by its media id, instead of degrading to a session-gated
    Daftra ``link`` (or raising outright when the invoice has no link at all).
    """
    from sender.presentation import cli

    settings = Settings.from_env(
        {"DAFTRA_API_KEY": "k", "WHATSAPP_ACCESS_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "123"},
        require_daftra=False,
        require_whatsapp=False,
    )
    registry = _registry(APPROVED_TEXT_COMPONENTS, tmp_path / "template_state.json")
    provider = RecordingAttachmentProvider()
    current = registry.choose_builder("aizen_invoice", "en", "20", attachment=provider)
    assert isinstance(current, CleanTextTemplateBuilder)
    # Retained by the clean builder but never consulted by it.
    assert current.attachment is provider
    assert provider.calls == []

    swapped = cli._other_builder(current, settings)

    assert isinstance(swapped, LegacyInvoiceTemplateBuilder)
    assert swapped.attachment is provider
    assert swapped.attachment_mode == "upload"

    invoice = make_stub_invoice(
        id="900", number="INV-900", pdf_url="https://daftra.example.invalid/INV-900.pdf"
    )
    assert pdf_link(invoice) == "https://daftra.example.invalid/INV-900.pdf"
    payload = swapped.build(invoice, "20127693262")
    assert _types(payload) == ["header", "body"]
    header = _components(payload)[0]
    assert header["type"] == "header"
    assert header["parameters"] == [
        {"type": "document", "document": {"id": "MID-AUDIT", "filename": "INV.pdf"}}
    ]
    # An asset we uploaded, never a link: the customer-facing message keeps the
    # real PDF instead of "a URL Meta has to fetch behind Daftra's login".
    assert "link" not in header["parameters"][0]["document"]
    assert provider.calls == ["INV-900"]

    # No link at all: the rescue path still produces a document, because it
    # never needed the url in the first place.
    linkless = make_stub_invoice(id="901", number="INV-901", public_url=None, pdf_url=None)
    assert pdf_link(linkless) is None
    assert _types(swapped.build(linkless, "20127693262")) == ["header", "body"]
