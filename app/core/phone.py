"""Indian mobile numbers, normalised exactly like the app's `IndianMobile.normalize`:
spaces, dashes, +91, 91, 0091 and a leading 0 are all accepted; the result is
`+91XXXXXXXXXX` with a 10-digit number starting 6-9."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator

_VALID = re.compile(r"^[6-9]\d{9}$")


def normalize_indian_mobile(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) > 10:
        if digits.startswith("0091"):
            digits = digits[4:]
        elif digits.startswith("91"):
            digits = digits[2:]
        elif digits.startswith("0"):
            digits = digits[1:]
    if not _VALID.match(digits):
        raise ValueError("not a valid Indian mobile number")
    return f"+91{digits}"


# Use as a Pydantic field type: `phone: IndianPhone`.
IndianPhone = Annotated[str, AfterValidator(normalize_indian_mobile)]
