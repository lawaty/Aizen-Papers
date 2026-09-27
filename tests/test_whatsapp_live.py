"""Live WhatsApp Cloud API tests using real credentials from .env.

These tests send **real** WhatsApp messages to a real phone number and cost
money on the business account, so they are opt-in and skipped by default. They
only run when all of the following hold:

- ``WHATSAPP_LIVE_TESTS`` is truthy (``1``/``true``/``yes``/``on``,
  case-insensitive). Credentials merely *looking* real is not consent, so a
  plain ``pytest`` run on a checkout that happens to have a filled-in ``.env``
  still skips every test below.
- ``WHATSAPP_ACCESS_TOKEN`` and ``WHATSAPP_PHONE_NUMBER_ID`` are configured and
  the token is not a placeholder.
- ``DAFTRA_API_KEY`` is configured.
- ``WHATSAPP_VERIFIED_RECIPIENT`` names a number added to the app's verified
  numbers. There is deliberately no default recipient: guessing one would send
  real messages to a number nobody verified.

To run them:

    WHATSAPP_LIVE_TESTS=1 WHATSAPP_VERIFIED_RECIPIENT=<your verified number> \\
        .venv/bin/python -m pytest tests/test_whatsapp_live.py
"""

import re
from datetime import date
from decimal import Decimal

import pytest

from sender.domain.errors import WhatsAppApiError
from sender.domain.models import Invoice
from sender.domain.phones import normalize_phone
from sender.domain.templates import LegacyInvoiceTemplateBuilder
from sender.infrastructure.whatsapp.client import WhatsAppClient


def _live_invoice() -> Invoice:
    return Invoice(
        id="1",
        number="INV-001",
        customer_name="Aizen Test",
        issue_date=date(2026, 9, 1),
        total=Decimal("1500.00"),
        public_url="https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf",
    )


def test_live_send_template_to_verified_recipient(live_settings, live_recipient):
    builder = LegacyInvoiceTemplateBuilder(
        template_name=live_settings.wa_template_name,
        language=live_settings.wa_template_lang,
        country_code=live_settings.default_country_code,
    )
    expected_to = normalize_phone(live_recipient, live_settings.default_country_code)
    payload = builder.build(_live_invoice(), live_recipient)
    assert payload["to"] == expected_to
    client = WhatsAppClient(
        access_token=live_settings.wa_access_token,
        phone_number_id=live_settings.wa_phone_number_id,
        api_version=live_settings.wa_api_version,
        timeout=live_settings.wa_timeout,
        default_country_code=live_settings.default_country_code,
    )
    response = client.send(payload)
    print("response:", response)
    wamid = response["messages"][0]["id"]
    wa_id = response["contacts"][0]["wa_id"]
    assert isinstance(wamid, str) and wamid
    assert wa_id == expected_to


def test_live_fake_token_raises_whatsapp_api_error(live_settings, live_recipient):
    builder = LegacyInvoiceTemplateBuilder(
        template_name=live_settings.wa_template_name,
        language=live_settings.wa_template_lang,
        country_code=live_settings.default_country_code,
    )
    payload = builder.build(_live_invoice(), live_recipient)
    client = WhatsAppClient(
        access_token="FAKE_TOKEN",
        phone_number_id=live_settings.wa_phone_number_id,
        api_version=live_settings.wa_api_version,
        timeout=live_settings.wa_timeout,
        default_country_code=live_settings.default_country_code,
    )
    with pytest.raises(WhatsAppApiError) as excinfo:
        client.send(payload)
    print("meta_error:", str(excinfo.value))
    print("meta_status:", excinfo.value.status)
    assert re.search(r"\[code \d+\]", str(excinfo.value))
    assert excinfo.value.status is not None and 400 <= excinfo.value.status < 500
    assert excinfo.value.payload.get("error", {}).get("code")


def test_live_send_freeform_to_verified_recipient(live_settings, live_recipient):
    """Freeform text send — currently fails with Meta 131037 on the 555 test
    number (display name not approved). Will turn green on a BYO number swap
    once the recipient has an open 24h customer-service window."""
    builder = LegacyInvoiceTemplateBuilder(
        template_name=live_settings.wa_template_name,
        language=live_settings.wa_template_lang,
        country_code=live_settings.default_country_code,
    )
    expected_to = normalize_phone(live_recipient, live_settings.default_country_code)
    payload = builder.build_text(_live_invoice(), live_recipient)
    assert payload["to"] == expected_to
    assert payload["type"] == "text"
    client = WhatsAppClient(
        access_token=live_settings.wa_access_token,
        phone_number_id=live_settings.wa_phone_number_id,
        api_version=live_settings.wa_api_version,
        timeout=live_settings.wa_timeout,
        default_country_code=live_settings.default_country_code,
    )
    response = client.send(payload)
    print("response:", response)
    wamid = response["messages"][0]["id"]
    wa_id = response["contacts"][0]["wa_id"]
    assert isinstance(wamid, str) and wamid
    assert wa_id == expected_to