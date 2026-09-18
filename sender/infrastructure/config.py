from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

DAFTRA_BASE_URL = "https://api.daftra.com/api2"


def _env_float(source: Mapping[str, str], key: str, default: float) -> float:
    raw = source.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid {key}: {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    daftra_api_key: str = field(repr=False)
    wa_access_token: str = field(repr=False)
    wa_phone_number_id: str
    daftra_base_url: str = DAFTRA_BASE_URL
    daftra_timeout: float = 15.0
    default_country_code: str = "20"
    wa_api_version: str = "v25.0"
    wa_template_name: str = "aizen_invoice"
    wa_template_lang: str = "en_EG"
    wa_timeout: float = 15.0
    dry_run: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        require_whatsapp: bool = False,
        require_daftra: bool = True,
    ) -> "Settings":
        source = os.environ if env is None else env
        required = ["DAFTRA_API_KEY"] if require_daftra else []
        if require_whatsapp:
            required += ["WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"]
        missing = [name for name in required if not source.get(name)]
        if missing:
            raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")
        return cls(
            daftra_api_key=source.get("DAFTRA_API_KEY", ""),
            wa_access_token=source.get("WHATSAPP_ACCESS_TOKEN", ""),
            wa_phone_number_id=source.get("WHATSAPP_PHONE_NUMBER_ID", ""),
            daftra_base_url=source.get("DAFTRA_BASE_URL", DAFTRA_BASE_URL).rstrip("/"),
            daftra_timeout=_env_float(source, "DAFTRA_TIMEOUT", 15.0),
            default_country_code=source.get("DEFAULT_COUNTRY_CODE", "20").strip(),
            wa_api_version=source.get("WHATSAPP_API_VERSION", "v25.0"),
            wa_template_name=source.get("WHATSAPP_TEMPLATE_NAME", "aizen_invoice"),
            wa_template_lang=source.get("WHATSAPP_TEMPLATE_LANG", "en_EG"),
            wa_timeout=_env_float(source, "WHATSAPP_TIMEOUT", 15.0),
            dry_run=(source.get("WHATSAPP_DRY_RUN") or "false").strip().lower() in ("1", "true", "yes"),
            log_level=(source.get("LOG_LEVEL") or "INFO").strip(),
        )