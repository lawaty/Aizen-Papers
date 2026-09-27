"""Shared fixtures for the WhatsApp sender test suite."""

import os

import pytest
from dotenv import load_dotenv

from sender.infrastructure.config import Settings

RECIPIENT_ENV = "WHATSAPP_VERIFIED_RECIPIENT"
LIVE_OPT_IN_ENV = "WHATSAPP_LIVE_TESTS"
#: Truthy spellings for the live opt-in, mirroring the boolean parsing used for
#: WHATSAPP_DRY_RUN: anything else (including unset) means "do not send for real".
TRUTHY = ("1", "true", "yes", "on")
PLACEHOLDER_MARKERS = (
    "placeholder",
    "your",
    "xxx",
    "todo",
    "replace",
    "changeme",
    "dummy",
    "fake",
    "example",
)


def _looks_placeholder(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) >= 40:
        return False
    lowered = stripped.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _live_skip_reasons() -> list[str]:
    load_dotenv()
    reasons = []
    # Live tests really send WhatsApp messages to a real number, so they are
    # opt-in: a full-looking .env is not consent. Without this flag a checkout
    # that happens to carry credentials silently messages a customer on every
    # `pytest` run.
    if (os.getenv(LIVE_OPT_IN_ENV) or "").strip().lower() not in TRUTHY:
        reasons.append(f"set {LIVE_OPT_IN_ENV}=1 to run live tests")
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    daftra_key = os.getenv("DAFTRA_API_KEY", "").strip()
    # No default recipient: a hardcoded fallback would aim real messages at a
    # number nobody verified, so an unset/placeholder value is a skip, not a guess.
    recipient = os.getenv(RECIPIENT_ENV, "").strip()
    if not token or _looks_placeholder(token):
        reasons.append("real WHATSAPP_ACCESS_TOKEN not configured")
    if not phone_id:
        reasons.append("WHATSAPP_PHONE_NUMBER_ID not configured")
    if not daftra_key:
        reasons.append("DAFTRA_API_KEY not configured")
    if not recipient or _looks_placeholder(recipient):
        reasons.append(f"set {RECIPIENT_ENV} to a verified test number")
    return reasons


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    reasons = _live_skip_reasons()
    if reasons:
        pytest.skip("live WhatsApp tests skipped: " + "; ".join(reasons))
    return Settings.from_env(require_whatsapp=True)


@pytest.fixture(scope="session")
def live_recipient() -> str:
    reasons = _live_skip_reasons()
    if reasons:
        pytest.skip("live WhatsApp tests skipped: " + "; ".join(reasons))
    return os.getenv(RECIPIENT_ENV, "").strip()