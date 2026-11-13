"""Bank of Japan API validation and lossless source-value decoding."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .transport import ApiError, Config, request_json
from .values import integer, iso_date, number, source_json, text

DATABASE = r"^[A-Z][A-Z0-9]{0,7}$"
CODE = r"^[A-Za-z0-9_@.+-]{1,128}$"
FREQUENCIES = ("CY", "FY", "CH", "FH", "Q", "M", "W", "D")


def codes(values: list[str]) -> list[str]:
    """Validate source codes without accepting database-prefixed display identifiers."""
    if not 1 <= len(values) <= 1250 or any(not isinstance(v, str) or re.fullmatch(CODE, v) is None for v in values):
        raise ApiError("series_codes must contain 1-1250 valid BoJ codes without a database prefix")
    result = [value.upper() for value in values]
    if len(set(result)) != len(result):
        raise ApiError("series_codes must not repeat")
    return result


def layer(value: str) -> str:
    """Validate one to five hierarchy levels with source wildcard syntax."""
    if re.fullmatch(r"(?:\*|[0-9]{1,8})(?:,(?:\*|[0-9]{1,8})){0,4}", value) is None:
        raise ApiError("layer must contain 1-5 comma-separated integers or '*' wildcards")
    return value


def period(value: str, frequency: str = "") -> str:
    """Validate native request bounds, including month bounds for daily and weekly data."""
    if not value:
        return value
    if re.fullmatch(r"[1-9][0-9]{3}(?:0[1-9]|1[0-2])?", value) is None:
        raise ApiError("BoJ bounds must use YYYY or YYYYMM/HH/QQ; daily and weekly bounds use YYYYMM")
    if frequency:
        if frequency in {"CY", "FY"}:
            valid = len(value) == 4
        else:
            maximum = 2 if frequency in {"CH", "FH"} else 4 if frequency == "Q" else 12
            valid = len(value) == 6 and 1 <= int(value[-2:]) <= maximum
        if not valid:
            raise ApiError("Period bound does not match the selected BoJ frequency")
    return value


def request(endpoint: str, query: list[tuple[str, str]]) -> dict[str, Any]:
    """Read successful JSON envelopes; never expose upstream message text in errors."""
    result = request_json(endpoint, [("format", "json"), *query], Config.from_env())
    if integer(result.get("STATUS")) != 200:
        message_id = result.get("MESSAGEID")
        safe_id = (
            message_id if isinstance(message_id, str) and re.fullmatch(r"M[0-9]{6}[EIS]", message_id) else "unknown"
        )
        raise ApiError(f"BoJ API rejected the request ({safe_id})")
    if result.get("MESSAGEID") not in {"M181000I", "M181030I"}:
        raise ApiError("BoJ returned an unknown success status")
    records = result.get("RESULTSET")
    if records is None and result["MESSAGEID"] == "M181030I":
        result["RESULTSET"] = []
    elif not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ApiError("BoJ response has an invalid RESULTSET")
    return result


def updated(value: Any) -> date | None:
    """Decode a source update calendar date without treating it as a release vintage."""
    raw = text(value)
    if not raw:
        return None
    if re.fullmatch(r"[0-9]{8}", raw) is None:
        raise ApiError("BoJ returned an invalid LAST_UPDATE date")
    return iso_date(raw[:4] + "-" + raw[4:6] + "-" + raw[6:])


def metadata(record: dict[str, Any], database: str, generated: Any) -> dict[str, Any]:
    """Preserve English and Japanese labels, original units and source hierarchy."""
    code = text(record.get("SERIES_CODE"), required=False)
    return {
        "database": database,
        "series_code": code or None,
        "name": text(record.get("NAME_OF_TIME_SERIES")),
        "name_ja": text(record.get("NAME_OF_TIME_SERIES_J")),
        "unit": text(record.get("UNIT")),
        "unit_ja": text(record.get("UNIT_J")),
        "frequency": text(record.get("FREQUENCY")),
        "category": text(record.get("CATEGORY")),
        "category_ja": text(record.get("CATEGORY_J")),
        "last_update": updated(record.get("LAST_UPDATE")),
        "start_period": text(record.get("START_OF_THE_TIME_SERIES")),
        "end_period": text(record.get("END_OF_THE_TIME_SERIES")),
        "notes": text(record.get("NOTES")),
        **{f"layer{i}": integer(record.get(f"LAYER{i}"), optional=True) for i in range(1, 6)},
        "source_timestamp": text(generated),
        "metadata_json": source_json({k: v for k, v in record.items() if k != "VALUES"}),
    }


def observations(records: list[dict[str, Any]], database: str, generated: Any) -> list[dict[str, Any]]:
    """Align period/value arrays strictly and preserve every source period code."""
    result = []
    for record in records:
        shared = metadata(record, database, generated)
        if not shared["series_code"] or not shared["frequency"]:
            raise ApiError("BoJ observation series is missing its code or frequency")
        values = record.get("VALUES")
        if not isinstance(values, dict):
            raise ApiError("BoJ response is missing its VALUES arrays")
        periods, numbers = values.get("SURVEY_DATES"), values.get("VALUES")
        if not isinstance(periods, list) or not isinstance(numbers, list) or len(periods) != len(numbers):
            raise ApiError("BoJ period and value arrays have different lengths or invalid types")
        seen: set[str] = set()
        for native, raw in zip(periods, numbers, strict=True):
            native_period = text(native, required=True)
            assert native_period is not None
            if re.fullmatch(r"[0-9]{4}(?:[0-9]{2}){0,2}", native_period) is None or native_period in seen:
                raise ApiError("BoJ response has an invalid or duplicate observation period")
            seen.add(native_period)
            numeric, original = number(raw)
            result.append({**shared, "period": native_period, "value": numeric, "value_text": original})
    return result
