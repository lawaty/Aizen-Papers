"""Status-aware selection between the legacy and clean template builders.

Queries the WhatsApp Business account for the current structure/status of the
invoice template, caches the result, and switches from the legacy
document-header builder to the clean body-only builder the moment the reviewed
template is approved. While the template is in review the account still serves
the last APPROVED revision, so the active builder tracks the structure of the
last approval (not the raw status): a document-header revision keeps the legacy
payload, a clean revision keeps the body-only payload even while a later edit
is back in review.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import requests

from sender.domain.errors import WhatsAppApiError
from sender.domain.ports import InvoiceAttachmentProvider
from sender.domain.templates import (
    CleanTextTemplateBuilder,
    InvoiceTemplateBuilder,
    LegacyInvoiceTemplateBuilder,
)
from sender.infrastructure.util import json_or_none, write_json_atomic
from sender.infrastructure.whatsapp.errors import to_api_error

log = logging.getLogger(__name__)

LEGACY = "legacy"
NEW = "new"
DEFAULT_CACHE = "template_state.json"
_VARIABLE_PATTERN = re.compile(r"\{\{[^{}]*\}\}")


@dataclass(frozen=True)
class TemplateStatus:
    status: str
    fingerprint: str
    clean: bool
    header_vars: int
    detected_at: float


def _header_of(components) -> dict | None:
    return next((c for c in components if str(c.get("type") or "").upper() == "HEADER"), None)


def _body_of(components) -> dict | None:
    return next((c for c in components if str(c.get("type") or "").upper() == "BODY"), None)


def _footer_of(components) -> dict | None:
    return next((c for c in components if str(c.get("type") or "").upper() == "FOOTER"), None)


def _placeholder_count(text: str) -> int:
    return len(_VARIABLE_PATTERN.findall(str(text or "")))


def _fingerprint(entry: Mapping) -> str:
    components = entry.get("components") or []
    header, body, footer = _header_of(components), _body_of(components), _footer_of(components)
    header_format = str((header or {}).get("format") or "none").upper()
    header_vars = _placeholder_count((header or {}).get("text"))
    body_vars = _placeholder_count((body or {}).get("text"))
    has_footer = footer is not None
    return f"header={header_format};header_vars={header_vars};body_vars={body_vars};footer={has_footer}"


class TemplateRegistry:
    def __init__(
        self,
        access_token: str,
        waba_id: str,
        api_version: str = "v25.0",
        template_name: str = "aizen_invoice",
        language: str = "",
        timeout: float = 15.0,
        session: requests.Session | None = None,
        cache_path: str = DEFAULT_CACHE,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._token = access_token
        self._waba_id = waba_id
        self._api_version = api_version
        self._name = template_name
        self._lang = language
        self._timeout = timeout
        self._session = session or requests.Session()
        self._cache_path = cache_path
        self._ttl = ttl_seconds
        self._clock = clock

    def query(self) -> TemplateStatus:
        url = (
            f"https://graph.facebook.com/{self._api_version}/{self._waba_id}"
            "/message_templates"
        )
        session = self._session
        session.headers.update({"Authorization": f"Bearer {self._token}"})
        response = session.get(
            url,
            params={"name": self._name, "fields": "status,name,language,components"},
            timeout=self._timeout,
        )
        if not response.ok:
            # via to_api_error so the message keeps Meta's [code NNNNN] and
            # fbtrace_id: the template query is where a 132001-class "template
            # parameter mismatch" is diagnosed, so the bare response text that
            # used to be raised here threw the diagnostics away.
            raise to_api_error(response, "template query")
        body = json_or_none(response)
        if not isinstance(body, dict):
            raise ValueError("the template query returned a non-object body")
        items = body.get("data") or []
        entry = None
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("name") != self._name:
                continue
            if self._lang and str(item.get("language") or "") != self._lang:
                continue
            entry = item
            break
        if entry is None:
            raise ValueError(f"No template named {self._name!r} found on the account")
        components = entry.get("components") or []
        header = _header_of(components)
        status = str(entry.get("status") or "")
        header_vars = _placeholder_count((header or {}).get("text"))
        has_document = bool(header and str(header.get("format") or "").upper() == "DOCUMENT")
        return TemplateStatus(
            status=status,
            fingerprint=_fingerprint(entry),
            clean=status.upper() == "APPROVED" and not has_document and header_vars == 0,
            header_vars=header_vars,
            detected_at=self._clock(),
        )

    def current(self, force: bool = False) -> dict:
        if not force:
            cached = self._load_cache()
            if cached is not None and self._is_fresh(cached) and cached.get("status") != "PENDING":
                return cached
        try:
            return self.refresh()
        except (WhatsAppApiError, ValueError, requests.RequestException) as exc:
            cached = self._load_cache()
            if cached is not None:
                log.warning("template check failed (%s); reusing cached snapshot", exc)
                return cached
            log.warning(
                "template check failed (%s); falling back to the legacy builder", exc
            )
            return self._fallback_snapshot()

    def refresh(self) -> dict:
        previous = self._load_cache() or {}
        status = self.query()
        previous_structure = previous.get("approved_structure")
        if status.status.upper() == "APPROVED":
            structure = NEW if status.clean else LEGACY
        else:
            structure = previous_structure
        active = NEW if structure == NEW else LEGACY
        snapshot = {
            "template": self._name,
            "fetched_at": status.detected_at,
            "status": status.status,
            "fingerprint": status.fingerprint,
            "header_vars": status.header_vars,
            "clean": status.clean,
            "approved_structure": structure,
            "active_builder": active,
            "legacy_deprecated": False,
        }
        previous_noticed = previous.get("noticed_at")
        if structure == NEW and previous_structure != NEW and previous_noticed is None:
            snapshot["legacy_deprecated"] = True
            snapshot["noticed_at"] = self._clock()
            log.warning(
                "%s approved with the new structure; the legacy document-header "
                "builder is deprecated",
                self._name,
            )
        elif previous_noticed is not None:
            snapshot["legacy_deprecated"] = True
            snapshot["noticed_at"] = previous_noticed
        if not status.clean and status.status:
            log.warning(
                "%s is in status %r (clean=%s); keeping the last approved structure %r",
                self._name,
                status.status,
                status.clean,
                structure,
            )
        self._save_cache(snapshot)
        return snapshot

    def choose_builder(
        self,
        name: str,
        language: str,
        country_code: str,
        attachment: InvoiceAttachmentProvider | None = None,
    ) -> InvoiceTemplateBuilder:
        """The builder matching the template's current status.

        *attachment* is only relevant to the legacy builder (the clean template
        has no header document), but it is accepted for both so callers do not
        have to know which one they will get.
        """
        snapshot = self.current()
        if snapshot.get("active_builder") == NEW:
            return CleanTextTemplateBuilder(name, language, country_code)
        return LegacyInvoiceTemplateBuilder(
            name, language, country_code, attachment=attachment
        )

    def _fallback_snapshot(self) -> dict:
        return {
            "template": self._name,
            "fetched_at": self._clock(),
            "status": "",
            "fingerprint": "",
            "header_vars": 0,
            "clean": False,
            "approved_structure": None,
            "active_builder": LEGACY,
            "legacy_deprecated": False,
        }

    def _cache_file(self) -> Path:
        return Path(self._cache_path)

    def _load_cache(self) -> dict | None:
        try:
            raw = self._cache_file().read_text(encoding="utf-8")
        except OSError:
            return None
        except UnicodeDecodeError:
            # Binary garbage in the cache is a miss, not a crash: the registry
            # simply re-queries the Graph API and rewrites the file. OSError alone
            # does not cover this — UnicodeDecodeError is a ValueError.
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("template") != self._name:
            return None
        return data

    def _save_cache(self, data: dict) -> None:
        try:
            write_json_atomic(self._cache_file(), data)
        except OSError:
            log.warning("could not write the template cache at %s", self._cache_path)

    def _is_fresh(self, cached: dict) -> bool:
        fetched = cached.get("fetched_at")
        if not isinstance(fetched, (int, float)):
            return False
        return (self._clock() - fetched) < self._ttl