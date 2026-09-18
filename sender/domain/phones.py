from __future__ import annotations

import re


def normalize_phone(raw: str, default_country_code: str = "20") -> str:
    digits = re.sub(r"\D", "", str(raw))
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0") and default_country_code:
        digits = default_country_code + digits[1:]
    if not 8 <= len(digits) <= 15:
        raise ValueError(f"Invalid phone number: {raw!r}")
    return digits