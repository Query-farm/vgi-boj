"""Strict source-value conversion and JSON preservation for statistical data."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from typing import Any

from .transport import ApiError


def source_json(value: Any) -> str:
    """Preserve source fields with deterministic JSON and finite numbers."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError, OverflowError):
        raise ApiError("Response contains invalid JSON values") from None


def text(value: Any, *, required: bool = False) -> str | None:
    """Read source strings and integer identifiers without losing leading zeroes."""
    if value is None and not required:
        return None
    if type(value) not in (str, int) or (required and not str(value)):
        raise ApiError("Response has a missing or invalid text field")
    return str(value)


def integer(value: Any, *, optional: bool = False) -> int | None:
    """Read a signed integer without coercing booleans or fractional values."""
    if optional and value in (None, ""):
        return None
    if type(value) not in (str, int) or re.fullmatch(r"[+-]?\d{1,18}", str(value)) is None:
        raise ApiError("Response has an invalid integer field")
    return int(value)


def number(value: Any, *, missing: frozenset[str] = frozenset({"", "null", ".."})) -> tuple[float | None, str | None]:
    """Retain original numeric text and reject unknown or non-finite values."""
    if value is None:
        return None, None
    raw = text(value, required=False)
    assert raw is not None
    if raw in missing:
        return None, raw
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", raw) is None:
        raise ApiError("Response contains an unrecognized numeric value")
    result = float(raw)
    if not math.isfinite(result):
        raise ApiError("Response contains a non-finite numeric value")
    return result, raw


def iso_date(value: str) -> date:
    """Require an exact ISO calendar date."""
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise ApiError("Date must be a valid YYYY-MM-DD calendar date") from None


def timestamp(value: str) -> str:
    """Validate a timezone-qualified ISO timestamp without losing its source text."""
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or "T" not in value:
            raise ValueError
        return value
    except ValueError:
        raise ApiError("Timestamp must be ISO 8601 with a timezone, for example 2024-01-01T00:00:00Z") from None
