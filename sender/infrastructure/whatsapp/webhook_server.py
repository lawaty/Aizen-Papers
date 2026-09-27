"""Minimal WhatsApp Cloud API webhook receiver for delivery-status observation.

Adds no dependencies beyond the standard library. Expects to be exposed publicly
(e.g. via a cloudflared quick tunnel) and registered as the callback URL in the
Meta App Dashboard, then subscribed with POST /{WABA}/subscribed_apps.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from sender.domain.errors import WhatsAppApiError
from sender.infrastructure.util import json_or_none


class WebhookHandler(BaseHTTPRequestHandler):
    verify_token: str = ""
    events_file: str = ""

    def do_GET(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        mode = (query.get("hub.mode") or [""])[0]
        token = (query.get("hub.verify_token") or [""])[0]
        challenge = (query.get("hub.challenge") or [""])[0]
        if mode == "subscribe" and token and token == self.verify_token and challenge:
            self._respond(200, "text/plain", challenge.encode())
        else:
            self._respond(403, "text/plain", b"invalid verification request")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            event = json.loads(raw or b"{}")
        except ValueError:
            self._respond(400, "text/plain", b"invalid json")
            return
        self._log_event(event)
        self._respond(200, "application/json", b"{}")

    def _log_event(self, event: dict) -> None:
        path = Path(self.events_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        pass


def serve(host: str, port: int, verify_token: str, events_file: str, daemon: bool) -> HTTPServer:
    from threading import Thread

    token = verify_token
    file = events_file

    class Handler(WebhookHandler):
        verify_token = token
        events_file = file

    server = HTTPServer((host, port), Handler)
    target = Thread(target=server.serve_forever, daemon=True)
    target.start()
    if not daemon:
        target.join()
    return server


def subscribe(access_token: str, waba_id: str, api_version: str = "v25.0") -> dict:
    url = f"https://graph.facebook.com/{api_version}/{waba_id}/subscribed_apps"
    response = requests.post(url, params={"access_token": access_token}, timeout=15.0)
    if not response.ok:
        raise WhatsAppApiError(response.status_code, response.text[:300], json_or_none(response))
    return response.json()