"""Deterministic identity helpers shared by implementation and unit tests."""

from __future__ import annotations

import hashlib
from typing import Any


IDENTITY_SEPARATOR = "\x1f"


def stable_sha256(*values: Any) -> str:
    """Hash ordered identity components using the same separator as Spark logic."""
    payload = IDENTITY_SEPARATOR.join("" if value is None else str(value) for value in values)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
