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
    if digits.startswith(default_country_code):
        return digits
    # A number that never carried its country code — a 10-digit local number
    # missing its leading 0 ("1012345678"), an 8-digit string — must not be sent
    # as-is: Meta rejects it per recipient and the poller would abandon the
    # invoice. A long number is treated as a genuine international number from a
    # country other than the default, which is how a non-local customer keeps
    # working. A national-length bare number is ambiguous, so it fails loudly.
    if len(digits) <= 11:
        raise ValueError(f"Invalid phone number (no usable country code): {raw!r}")
    return digits