"""Failure handling at the credential-bearing HTTP boundary."""

from __future__ import annotations

import http.client
import io
import urllib.error
from datetime import UTC, datetime, timedelta
from email.message import Message
from email.utils import format_datetime
from unittest.mock import patch

import pytest

from boj_worker.transport import ApiError, Config, request_json, retry_delay

SECRET = "fixture-credential-never-print-this"


class Response(io.BytesIO):
    status = 200


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "file:///etc/passwd",
        "https://user:password@host",
        "https://host?key=secret",
        "https://host#frag",
        "https://host:bad",
        "https://host:0",
        "https://host:65536",
    ],
)
def test_reject_unsafe_configuration(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setenv("BOJ_API_BASE_URL", url)
    with pytest.raises(ApiError):
        Config.from_env()


@pytest.mark.parametrize(
    "name,value",
    [
        ("MAX_ATTEMPTS", "1.5"),
        ("TIMEOUT_SECONDS", "nan"),
        ("TIMEOUT_SECONDS", "0"),
        ("MIN_INTERVAL_SECONDS", "-1"),
        ("RETRY_BUDGET_SECONDS", "inf"),
        ("MAX_RESPONSE_BYTES", "12"),
    ],
)
def test_invalid_limits(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv("BOJ_" + name, value)
    with pytest.raises(ApiError):
        Config.from_env()


def test_loopback_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOJ_API_BASE_URL", "http://127.0.0.1:12345")
    monkeypatch.delenv("BOJ_ALLOW_INSECURE_HTTP", raising=False)
    with pytest.raises(ApiError):
        Config.from_env()
    monkeypatch.setenv("BOJ_ALLOW_INSECURE_HTTP", "1")
    assert Config.from_env().base_url == "http://127.0.0.1:12345"


@pytest.mark.parametrize(
    "endpoint",
    ["//attacker.test", "https://attacker.test", "../secret", "data?key=other", "data#fragment", "data\\other"],
)
def test_invalid_endpoint_does_not_open_connection(endpoint: str) -> None:
    with patch("boj_worker.transport.urllib.request.build_opener") as factory, pytest.raises(ApiError):
        request_json(endpoint, [], Config())
    factory.assert_not_called()


@pytest.mark.parametrize(
    "error", [http.client.BadStatusLine(SECRET), TimeoutError(SECRET), urllib.error.URLError(SECRET)]
)
def test_network_errors_are_redacted(error: Exception) -> None:
    with patch("boj_worker.transport.urllib.request.build_opener") as factory:
        factory.return_value.open.side_effect = error
        with pytest.raises(ApiError, match="network request failed") as exc:
            request_json("data", [("api_key", SECRET)], Config(attempts=1, min_interval=0))
    assert SECRET not in str(exc.value)
    assert exc.value.__suppress_context__


@pytest.mark.parametrize(
    "status,attempts", [(400, 1), (401, 1), (403, 1), (404, 1), (302, 1), (429, 3), (500, 3), (503, 3)]
)
def test_http_retry_policy_and_redaction(status: int, attempts: int) -> None:
    headers = Message()
    headers["Retry-After"] = "0"
    with (
        patch("boj_worker.transport.urllib.request.build_opener") as factory,
        patch("boj_worker.transport.time.sleep"),
    ):
        factory.return_value.open.side_effect = [
            urllib.error.HTTPError("https://example.test/?key=" + SECRET, status, SECRET, headers, None)
            for _ in range(attempts)
        ]
        with pytest.raises(ApiError, match=f"HTTP {status}") as exc:
            request_json("data", [("api_key", SECRET)], Config(attempts=3, min_interval=0))
        assert factory.return_value.open.call_count == attempts
    assert SECRET not in str(exc.value)
    assert exc.value.__suppress_context__


def test_retry_after_larger_than_budget_is_not_shortened() -> None:
    headers = Message()
    headers["Retry-After"] = "600"
    with patch("boj_worker.transport.urllib.request.build_opener") as factory:
        factory.return_value.open.side_effect = urllib.error.HTTPError(
            "https://example.test", 429, "Rate limit", headers, None
        )
        with pytest.raises(ApiError, match="time budget"):
            request_json("data", [], Config(min_interval=0, retry_budget=1))
        assert factory.return_value.open.call_count == 1


def test_retry_after_seconds_date_and_malformed_values() -> None:
    when = datetime.now(UTC) + timedelta(seconds=10)
    assert 8 <= retry_delay(format_datetime(when), 0) <= 10
    assert retry_delay("60", 0) == 60
    for value in ("garbage", "NaN", "-1"):
        assert 0.5 <= retry_delay(value, 0) <= 0.75


@pytest.mark.parametrize("body", [b"[]", b"null", b"broken json", b"\xff"])
def test_invalid_json_is_safe(body: bytes) -> None:
    with patch("boj_worker.transport.urllib.request.build_opener") as factory:
        factory.return_value.open.return_value = Response(body)
        with pytest.raises(ApiError, match="invalid"):
            request_json("data", [], Config(min_interval=0))


def test_response_body_limit() -> None:
    with patch("boj_worker.transport.urllib.request.build_opener") as factory:
        factory.return_value.open.return_value = Response(b'{"data": "' + b"x" * 2048 + b'"}')
        with pytest.raises(ApiError, match="MAX_RESPONSE_BYTES"):
            request_json("data", [], Config(min_interval=0, max_response_bytes=1024))


def test_request_budget_interrupts_trickled_body() -> None:
    with (
        patch("boj_worker.transport.urllib.request.build_opener") as factory,
        patch("boj_worker.transport._throttle"),
        patch("boj_worker.transport.time.monotonic", side_effect=[0.0, 0.0, 0.0, 2.0]),
    ):
        factory.return_value.open.return_value = Response(b'{"data": []}')
        with pytest.raises(ApiError, match="time budget"):
            request_json("data", [], Config(retry_budget=1, min_interval=0))


def test_default_endpoint_is_official(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BOJ_API_BASE_URL", raising=False)
    assert Config.from_env().base_url == "https://www.stat-search.boj.or.jp/api/v1"
