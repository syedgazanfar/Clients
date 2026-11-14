"""Phone number normalisation to digits-only international format."""

from __future__ import annotations

import re


def normalize_phone(raw: str, default_country_code: str = "44") -> str | None:
    """Return digits-only international number (no plus sign), or None if invalid.

    "+44 7700 900123" -> "447700900123"
    "07700 900123"    -> "447700900123" (with default country code 44)
    "447700900123@c.us" -> "447700900123"
    """
    if not raw:
        return None
    text = raw.strip().split("@", 1)[0]
    international = text.startswith("+") or text.startswith("00")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return None
    if text.startswith("00"):
        digits = digits[2:]
    elif not international:
        if digits.startswith("0"):
            digits = default_country_code + digits.lstrip("0")
        elif len(digits) <= 10:
            digits = default_country_code + digits
    if not 8 <= len(digits) <= 15:
        return None
    return digits
