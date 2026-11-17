"""Field types shared by the API models."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import StringConstraints

Gender = Literal["female", "male", "other"]
Relationship = Literal["self", "son", "daughter", "wife", "husband", "mother", "father", "other"]
Proximity = Literal["strong", "moderate", "low"]
Availability = Literal["available", "limited", "unavailable"]
Signal = Literal["good", "fair", "poor"]
InvitationStatus = Literal["pending", "accepted", "declined"]
Cohort = Literal[1, 2]

Text50 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Text100 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
OptionalText100 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]
BandId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z0-9-]{6,32}$"),
]
