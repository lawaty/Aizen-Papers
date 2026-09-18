"""Shared fixtures for the WhatsApp sender test suite."""

import os

import pytest
from dotenv import load_dotenv

from sender.infrastructure.config import Settings

DEFAULT_RECIPIENT = "201027693262"
RECIPIENT_ENV = "WHATSAPP_VERIFIED_RECIPIENT"
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
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    daftra_key = os.getenv("DAFTRA_API_KEY", "")
    recipient = os.getenv(RECIPIENT_ENV, DEFAULT_RECIPIENT).strip()
    reasons = []
    if not token or _looks_placeholder(token):
        reasons.append("real WHATSAPP_ACCESS_TOKEN not configured")
    if not phone_id:
        reasons.append("WHATSAPP_PHONE_NUMBER_ID not configured")
    if not daftra_key:
        reasons.append("DAFTRA_API_KEY not configured (Settings.from_env requires it)")
    if not recipient:
        reasons.append("verified recipient not configured")
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
    return os.getenv(RECIPIENT_ENV, DEFAULT_RECIPIENT).strip()