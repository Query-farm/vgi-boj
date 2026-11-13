"""Central bank values, pagination and authentication through real worker processes."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from vgi.arguments import Arguments
from vgi.client import Client
from vgi.invocation import BindResponse

from boj_worker.auth import validate_http_auth
from boj_worker.functions import State
from boj_worker.transport import ApiError
from test.mock_api.api_server import ApiServer


@pytest.fixture()
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[ApiServer]:
    server = ApiServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("BOJ_API_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("BOJ_ALLOW_INSECURE_HTTP", "1")
    monkeypatch.setenv("BOJ_MIN_INTERVAL_SECONDS", "0")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def boj_client(api: ApiServer) -> Iterator[Client]:
    with Client([sys.executable, "-m", "boj_worker.worker"], pool=None) as client:
        yield client


def table(client: Client, name: str, *positional: Any, **named: Any) -> pa.Table:
    bound: list[BindResponse] = []
    batches = list(
        client.table_function(
            function_name=name,
            schema_path=["main"],
            bind_result_callback=bound.append,
            arguments=Arguments(
                positional=tuple(pa.scalar(v) for v in positional), named={k: pa.scalar(v) for k, v in named.items()}
            ),
        )
    )
    return pa.Table.from_batches(batches, schema=bound[-1].output_schema)


WORKER_TOKEN = "fixture-only-worker-token-with-32-characters"


@pytest.fixture()
def boj_http(api: ApiServer, tmp_path: Path) -> Iterator[str]:
    port_file = tmp_path / "port"
    environment = os.environ.copy()
    environment.update(VGI_BEARER_TOKENS=f"{WORKER_TOKEN}=analyst", VGI_PROXY_PROOF_MODE="off")
    environment.pop("VGI_JWT_ISSUER", None)
    with (tmp_path / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "boj_worker.worker", "--http", "--port", "0", "--port-file", str(port_file)],
            stdout=log,
            stderr=log,
            env=environment,
        )
        try:
            deadline = time.monotonic() + 20
            while not port_file.exists():
                assert process.poll() is None, "HTTP worker exited before publishing its port"
                if time.monotonic() >= deadline:
                    pytest.fail("HTTP worker did not start within 20 seconds")
                time.sleep(0.05)
            location = "http://127.0.0.1:" + port_file.read_text().strip()
            while True:
                try:
                    with urllib.request.urlopen(location + "/health", timeout=0.5) as response:
                        if response.status == 200:
                            break
                except (urllib.error.URLError, TimeoutError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        pytest.fail("HTTP worker did not become ready")
                    time.sleep(0.05)
            yield location
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log.seek(0)
            messages = log.read()
            assert WORKER_TOKEN not in messages


@pytest.mark.parametrize("token", [None, "incorrect-token"])
def test_http_rpc_rejects_missing_or_wrong_bearer(boj_http: str, api: ApiServer, token: str | None) -> None:
    headers = {"Authorization": "Bearer " + token} if token else {}
    request = urllib.request.Request(boj_http + "/__describe__", headers=headers)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=5)
    assert exc.value.code == 401
    exc.value.close()
    with (
        pytest.raises(Exception, match="authentication rejected"),
        Client(transport="http", base_url=boj_http, bearer_token=token, pool=None) as client,
    ):
        table(client, "datasets")
    assert api.requests == []


def test_http_health_remains_available_without_bearer(boj_http: str) -> None:
    with urllib.request.urlopen(boj_http + "/health", timeout=5) as response:
        assert response.status == 200


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        ",",
        WORKER_TOKEN,
        "=analyst",
        f"{WORKER_TOKEN}=",
        "short=analyst",
        f"{WORKER_TOKEN}=analyst,",
        f"{WORKER_TOKEN}=analyst,{WORKER_TOKEN}=other",
    ],
)
def test_bad_bearer_configuration_fails_without_exposing_token(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("VGI_BEARER_TOKENS", value)
    with pytest.raises(ValueError, match="Invalid VGI_BEARER_TOKENS") as exc:
        validate_http_auth()
    assert WORKER_TOKEN not in str(exc.value)


def test_bearer_configuration_accepts_multiple_principals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VGI_BEARER_TOKENS", f"{WORKER_TOKEN}=analyst,{WORKER_TOKEN}-second=service")
    validate_http_auth()
    monkeypatch.delenv("VGI_BEARER_TOKENS")
    validate_http_auth()


def test_bad_bearer_configuration_stops_http_startup(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["VGI_BEARER_TOKENS"] = WORKER_TOKEN
    port_file = tmp_path / "port"
    result = subprocess.run(
        [sys.executable, "-m", "boj_worker.worker", "--http", "--port-file", str(port_file)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )
    assert result.returncode != 0
    assert "Invalid VGI_BEARER_TOKENS" in result.stderr
    assert WORKER_TOKEN not in result.stderr + result.stdout
    assert not port_file.exists()


def test_http_authenticated_data(boj_http: str, api: ApiServer) -> None:
    with Client(transport="http", base_url=boj_http, bearer_token=WORKER_TOKEN, pool=None) as client:
        result = table(client, "observations", "IR01", ["MADR1Z@D", "OTHERD"])
    assert result.num_rows == 6
    assert len(api.requests) == 2
    assert all(WORKER_TOKEN not in str(record) for record in api.requests)


def test_state_roundtrip_has_no_values_or_credentials() -> None:
    state = State()
    assert State.deserialize_from_bytes(state.serialize_to_bytes()) == state
    assert not any("token" in name or "key" in name or "value" in name for name in state.__dataclass_fields__)


def test_discovery_and_localized_metadata(boj_client: Client, api: ApiServer) -> None:
    assert table(boj_client, "datasets").num_rows == 10
    assert not api.requests
    result = table(boj_client, "series", "IR01", language="jp").to_pylist()
    assert len(result) == 2
    assert result[0]["name_ja"] == "金利" and result[0]["last_update"] == date(2024, 2, 1)
    assert table(boj_client, "series", "IR01", include_headings=True).num_rows == 3
    assert table(boj_client, "series", "IR01", query="MADR1M").num_rows == 1
    assert table(boj_client, "layers", "IR01")["series_code"].to_pylist() == [None]


def test_pagination_precision_nulls_and_native_periods(boj_client: Client, api: ApiServer) -> None:
    result = table(
        boj_client, "observations", "IR01", ["MADR1Z@D", "OTHERD"], start_period="202401", end_period="202401"
    )
    assert result.num_rows == 6
    assert result["value_text"].to_pylist() == ["0.30", None, "0.35"] * 2
    assert result["period"].to_pylist() == ["20240101", "20240102", "20240103"] * 2
    assert [r["query"]["startPosition"] for r in api.requests] == [["1"], ["2"]]
    assert all(r["query"]["startDate"] == ["202401"] for r in api.requests)
    assert all(r["query"]["code"] == ["MADR1Z@D,OTHERD"] for r in api.requests)


def test_caps_and_layer_selection(boj_client: Client, api: ApiServer) -> None:
    assert table(boj_client, "observations", "IR01", ["MADR1Z@D", "OTHERD"], max_rows=4).num_rows == 4
    assert len(api.requests) == 2
    api.requests.clear()
    assert table(boj_client, "layer_observations", "IR01", "D", "1,1", max_rows=2).num_rows == 2
    assert len(api.requests) == 1 and api.requests[0]["query"]["layer"] == ["1,1"]
    assert table(boj_client, "observations", "IR01", ["MADR1Z@D"], start_period="210001").num_rows == 0


@pytest.mark.parametrize(
    "mode,match",
    [
        ("STALLED", "stalled"),
        ("REPEAT", "repeated"),
        ("WRONGPOS", "different pagination"),
        ("BADARRAY", "lengths"),
        ("BADVALUE", "numeric"),
        ("DUPLICATE", "duplicate"),
        ("APIERROR", "M181013E"),
        ("HTML", "invalid JSON"),
        ("REDIRECT", "HTTP 302"),
        ("ERROR", "HTTP 400"),
    ],
)
def test_invalid_responses(boj_client: Client, api: ApiServer, mode: str, match: str) -> None:
    api.mode = mode
    with pytest.raises(Exception, match=match) as exc:
        table(boj_client, "observations", "IR01", ["MADR1Z@D", "OTHERD"])
    assert "secret-sentinel" not in str(exc.value)


def test_retry(boj_client: Client, api: ApiServer) -> None:
    api.mode = "RETRY"
    assert table(boj_client, "observations", "IR01", ["MADR1Z@D"]).num_rows == 3
    assert api.attempts == 2


@pytest.mark.parametrize("codes", [["MADR1Z@D", "MADR1Z@D"], ["IR01'MADR1Z@D"], ["../x"], [None]])
def test_invalid_codes_before_network(boj_client: Client, api: ApiServer, codes: list[Any]) -> None:
    with pytest.raises(
        Exception, match=r"(?i)(series_codes|invalid|expected|must|none|null|period|date|timestamp|layer|argument)"
    ):
        table(boj_client, "observations", "IR01", codes)
    assert not api.requests


@pytest.mark.parametrize(
    "frequency,layer,start",
    [("Q", "1", "202405"), ("D", "1", "20240101"), ("CY", "1", "202401"), ("D", "1,,2", "202401")],
)
def test_invalid_layer_arguments(boj_client: Client, api: ApiServer, frequency: str, layer: str, start: str) -> None:
    with pytest.raises(
        Exception, match=r"(?i)(series_codes|invalid|expected|must|none|null|period|date|timestamp|layer|argument)"
    ):
        table(boj_client, "layer_observations", "IR01", frequency, layer, start_period=start)
    assert not api.requests


def test_source_numeric_lexemes_and_nonfinite_json(monkeypatch: pytest.MonkeyPatch) -> None:
    from boj_worker import transport
    from boj_worker.transport import Config, HttpResponse, request_json

    monkeypatch.setattr(
        transport, "request_bytes", lambda *a, **k: HttpResponse(body=b'{"x":0.30000000000000001}', status=200)
    )
    assert request_json("getMetadata.JSON", [], Config.from_env())["x"] == "0.30000000000000001"
    monkeypatch.setattr(transport, "request_bytes", lambda *a, **k: HttpResponse(body=b'{"x":NaN}', status=200))
    with pytest.raises(ApiError):
        request_json("getMetadata.JSON", [], Config.from_env())


def test_case_insensitive_series_codes(boj_client: Client, api: ApiServer) -> None:
    assert table(boj_client, "observations", "IR01", ["madr1z@d"]).num_rows == 3
    assert api.requests[-1]["query"]["code"] == ["MADR1Z@D"]
    with pytest.raises(Exception, match="must not repeat"):
        table(boj_client, "observations", "IR01", ["madr1z@d", "MADR1Z@D"])
