"""Bounded, credential-safe HTTP access to the public data API."""

from __future__ import annotations

import http.client
import json
import math
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

_RATE_LOCK = threading.Lock()
_LAST_REQUEST = 0.0


class ApiError(RuntimeError):
    """A safe error message suitable for returning through SQL."""


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    """Read and validate an operator-controlled numeric setting."""
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        raise ApiError(f"{name} must be numeric") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ApiError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True)
class Config:
    """Operator settings; neither credentials nor configuration enter stream state."""

    base_url: str = "https://www.stat-search.boj.or.jp/api/v1"
    timeout: float = 30.0
    attempts: int = 4
    retry_budget: float = 120.0
    min_interval: float = 1.0
    max_response_bytes: int = 16 * 1024 * 1024

    @classmethod
    def from_env(cls) -> Config:
        """Load settings, allowing plaintext HTTP only for explicitly enabled loopback tests."""
        url = os.environ.get("BOJ_API_BASE_URL", cls.base_url).rstrip("/")
        try:
            parts = urllib.parse.urlsplit(url)
            valid_port = parts.port is None or 0 < parts.port <= 65535
        except ValueError:
            raise ApiError("BOJ_API_BASE_URL is invalid") from None
        loopback_http = (
            parts.scheme == "http"
            and parts.hostname in {"localhost", "127.0.0.1", "::1"}
            and os.environ.get("BOJ_ALLOW_INSECURE_HTTP") == "1"
        )
        if (
            not parts.hostname
            or not valid_port
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or any(c.isspace() for c in url)
            or (parts.scheme != "https" and not loopback_http)
        ):
            raise ApiError("BOJ_API_BASE_URL must be HTTPS without credentials, query, or fragment")
        attempts = _number("BOJ_MAX_ATTEMPTS", 4, 1, 6)
        if not attempts.is_integer():
            raise ApiError("BOJ_MAX_ATTEMPTS must be an integer")
        size = _number("BOJ_MAX_RESPONSE_BYTES", 16 * 1024 * 1024, 1024, 64 * 1024 * 1024)
        if not size.is_integer():
            raise ApiError("BOJ_MAX_RESPONSE_BYTES must be an integer")
        return cls(
            base_url=url,
            timeout=_number("BOJ_TIMEOUT_SECONDS", 30, 0.1, 120),
            attempts=int(attempts),
            retry_budget=_number("BOJ_RETRY_BUDGET_SECONDS", 120, 1, 600),
            min_interval=_number("BOJ_MIN_INTERVAL_SECONDS", 1.0, 0, 60),
            max_response_bytes=int(size),
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects to an unvalidated destination."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> urllib.request.Request | None:
        return None


def _throttle(interval: float, deadline: float) -> None:
    """Space request starts across threads in this process, within the request budget."""
    global _LAST_REQUEST
    with _RATE_LOCK:
        now = time.monotonic()
        delay = max(0.0, _LAST_REQUEST + interval - now)
        if now + delay >= deadline:
            raise ApiError("BOJ request budget exhausted while waiting for the rate limiter")
        if delay:
            time.sleep(delay)
        _LAST_REQUEST = time.monotonic()


def retry_delay(header: str | None, attempt: int) -> float:
    """Honor delta-seconds or HTTP-date Retry-After; otherwise use jittered backoff."""
    if header:
        try:
            seconds = float(header)
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
        except ValueError:
            try:
                when = parsedate_to_datetime(header)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                return max(0.0, (when - datetime.now(UTC)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return float(min(16.0, 0.5 * 2**attempt) + random.uniform(0.0, 0.25))


@dataclass(frozen=True)
class HttpResponse:
    """Status and bounded body; HTTP headers and URLs are not retained."""

    body: bytes
    status: int


def request_bytes(
    endpoint: str,
    query: list[tuple[str, str]],
    config: Config,
    *,
    payload: dict[str, Any] | None = None,
    accept: str = "application/json",
    allow_not_modified: bool = False,
) -> HttpResponse:
    """Fetch a bounded response with safe errors; only trusted code chooses endpoints."""
    if (
        endpoint.startswith("/")
        or "://" in endpoint
        or any(c in endpoint for c in "?#\\")
        or ".." in endpoint.split("/")
    ):
        raise ApiError("Invalid API endpoint")
    url = config.base_url + ("/" + endpoint if endpoint else "")
    if query:
        url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, allow_nan=False).encode() if payload is not None else None,
        headers={
            "Accept": accept,
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "User-Agent": "vgi-boj/0.1.0",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    deadline = time.monotonic() + config.retry_budget
    for attempt in range(config.attempts):
        _throttle(config.min_interval, deadline)
        delay = 0.0
        try:
            with opener.open(request, timeout=min(config.timeout, max(0.001, deadline - time.monotonic()))) as response:
                # Read incrementally to enforce the total deadline even when a server dribbles bytes.
                chunks: list[bytes] = []
                size = 0
                while True:
                    if time.monotonic() >= deadline:
                        raise ApiError("BOJ request exceeded its time budget")
                    chunk = response.read1(min(65536, config.max_response_bytes + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > config.max_response_bytes:
                        raise ApiError("BOJ response exceeds BOJ_MAX_RESPONSE_BYTES; narrow the query")
                raw = b"".join(chunks)
                status = response.status
            return HttpResponse(body=raw, status=status)
        except urllib.error.HTTPError as err:
            status = err.code
            if status == 304 and allow_not_modified:
                err.close()
                return HttpResponse(body=b"", status=304)
            header = err.headers.get("Retry-After")
            err.close()
            # Never echo upstream bodies, URLs or exception chains.
            if not (status == 429 or 500 <= status <= 599) or attempt + 1 == config.attempts:
                hint = {
                    400: "check query parameters",
                    401: "upstream authorization rejected",
                    403: "access denied",
                    404: "resource not found",
                    423: "resource locked",
                    429: "rate limit exceeded; reduce concurrency",
                }.get(status, "upstream request failed")
                raise ApiError(f"BOJ {endpoint} returned HTTP {status}: {hint}") from None
            delay = retry_delay(header, attempt)
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            if attempt + 1 == config.attempts:
                raise ApiError(f"BOJ {endpoint} network request failed after {config.attempts} attempts") from None
            delay = retry_delay(None, attempt)
        if time.monotonic() + delay >= deadline:
            raise ApiError(f"BOJ {endpoint} retry exceeds time budget; retry the query later")
        time.sleep(delay)
    raise AssertionError("unreachable")


def _invalid_constant(value: str) -> None:
    """Reject non-standard NaN/Infinity tokens in source JSON."""
    raise ValueError("Non-finite JSON constant")


def request_json(
    endpoint: str, query: list[tuple[str, str]], config: Config, *, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Read a strict JSON object while preserving fractional numeric source text."""
    response = request_bytes(endpoint, query, config, payload=payload)
    try:
        body = json.loads(response.body, parse_float=str, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ApiError("API returned invalid JSON") from None
    if not isinstance(body, dict):
        raise ApiError("API returned an invalid response or API error")
    return body
