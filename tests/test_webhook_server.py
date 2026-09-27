"""Offline tests for the WhatsApp webhook receiver and subscribe helper."""

import json

import requests

from sender.domain.errors import WhatsAppApiError
from sender.infrastructure.whatsapp.webhook_server import serve, subscribe

TOKEN = "sekrit"
TOKEN_WRONG = "nope"


def _start(events_file: str):
    server = serve("127.0.0.1", 0, TOKEN, events_file, daemon=True)
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}/"


def test_webhook_verify_echoes_challenge(tmp_path) -> None:
    events = str(tmp_path / "events.jsonl")
    server, base = _start(events)
    try:
        response = requests.get(
            base,
            params={"hub.mode": "subscribe", "hub.verify_token": TOKEN, "hub.challenge": "12345"},
            timeout=5,
        )
        assert response.status_code == 200
        assert response.text == "12345"
        bad = requests.get(
            base,
            params={"hub.mode": "subscribe", "hub.verify_token": TOKEN_WRONG, "hub.challenge": "x"},
            timeout=5,
        )
        assert bad.status_code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_webhook_posts_append_status_events(tmp_path) -> None:
    events = str(tmp_path / "events.jsonl")
    server, base = _start(events)
    try:
        status = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "1094775866378896",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "+20 12 80805534",
                                    "phone_number_id": "1304588702742851",
                                },
                                "statuses": [
                                    {"id": "wamid.X", "status": "failed", "errors": [{"code": 131042}]}
                                ],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }
        response = requests.post(base, json=status, timeout=5)
        assert response.status_code == 200
        lines = open(events, encoding="utf-8").read().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        statuses = parsed["entry"][0]["changes"][0]["value"]["statuses"]
        assert statuses[0]["status"] == "failed"
        assert statuses[0]["errors"][0]["code"] == 131042
        bad = requests.post(base, data=b"not json", timeout=5)
        assert bad.status_code == 400
    finally:
        server.shutdown()
        server.server_close()