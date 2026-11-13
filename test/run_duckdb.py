"""Verify a packaged or local worker using stock DuckDB and a signed VGI extension."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
WORKER_TOKEN = "fixture-only-worker-token-with-32-characters"


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main() -> None:
    manifest = json.loads((ROOT / "vgi-worker.json").read_text())
    catalog = manifest["catalog"]
    prefix = catalog.upper()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, default=ROOT / f".venv/bin/vgi-{catalog}-worker")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--http-auth", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.live and args.http_auth:
        parser.error("--http-auth uses offline fixtures; do not combine with --live")
    if args.live and manifest.get("key_required") and not os.environ.get(prefix + "_API_KEY"):
        parser.error(f"Live checks require {prefix}_API_KEY in the worker environment")
    if args.live and os.environ.get(prefix + "_API_BASE_URL", manifest["api_base_url"]) != manifest["api_base_url"]:
        parser.error("Live smoke tests require the official API base URL")
    server: Any = None
    thread: threading.Thread | None = None
    worker: subprocess.Popen[bytes] | None = None
    key = os.environ.get(prefix + "_API_KEY", "")
    if not args.live:
        from test.mock_api.api_server import API_KEY, ApiServer

        server = ApiServer()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        key = API_KEY
        os.environ.update(
            {
                prefix + "_API_KEY": key,
                prefix + "_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                prefix + "_ALLOW_INSECURE_HTTP": "1",
                prefix + "_MIN_INTERVAL_SECONDS": "0",
            }
        )
    report: dict[str, Any] = {
        "status": "failed",
        "catalog": catalog,
        "mode": "live" if args.live else "fixture",
        "checks": [],
    }
    try:
        duckdb = importlib.import_module("duckdb")
        report.update(engine="stock DuckDB", engine_version=duckdb.__version__)
        if duckdb.__version__ != "2.0.0":
            raise RuntimeError("This contract requires duckdb==2.0.0")
        with tempfile.TemporaryDirectory(prefix=f"vgi-{catalog}-verification-") as directory:
            con = duckdb.connect(":memory:", config={"extension_repository_directory": directory})
            try:
                con.execute("SET allow_extension_repositories='allowed'")
                con.execute("CREATE EXTENSION REPOSITORY query_farm WITH PREFIX 'https://duckdb-extensions.query.farm'")
                con.execute("INSTALL vgi FROM query_farm; LOAD vgi FROM query_farm")
                report["extension_version"] = con.execute(
                    "SELECT extension_version FROM duckdb_extensions() WHERE extension_name='vgi'"
                ).fetchone()[0]
                report["platform"] = con.execute("PRAGMA platform").fetchone()[0]
                argv = [str(args.worker.resolve())]
                command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
                attach = f"ATTACH {literal(catalog)} (TYPE vgi, LOCATION {literal(command)})"
                if args.http_auth:
                    report["mode"] = "fixture-http-auth"
                    port_file = Path(directory) / "port"
                    environment = os.environ.copy()
                    environment.update(VGI_BEARER_TOKENS=f"{WORKER_TOKEN}=sql-test", VGI_PROXY_PROOF_MODE="off")
                    environment.pop(prefix + "_API_KEY", None)
                    environment.pop("VGI_JWT_ISSUER", None)
                    worker = subprocess.Popen(
                        [*argv, "--http", "--port", "0", "--port-file", str(port_file)],
                        env=environment,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    deadline = time.monotonic() + 20
                    while not port_file.exists():
                        if worker.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("Authenticated HTTP worker failed to start")
                        time.sleep(0.05)
                    location = "http://127.0.0.1:" + port_file.read_text().strip()
                    while True:
                        try:
                            with urllib.request.urlopen(location + "/health", timeout=0.5) as response:
                                if response.status == 200:
                                    break
                        except (urllib.error.URLError, TimeoutError):
                            if worker.poll() is not None or time.monotonic() >= deadline:
                                raise RuntimeError("HTTP worker did not become ready") from None
                            time.sleep(0.05)
                    con.execute("INSTALL httpfs FROM core; LOAD httpfs")
                    for label, option in (("missing bearer", ""), ("wrong bearer", ", bearer_token 'wrong-token'")):
                        try:
                            con.execute(f"ATTACH {literal(catalog)} (TYPE vgi, LOCATION {literal(location)}{option})")
                        except Exception as exc:
                            if "401" not in str(exc) and "authentication" not in str(exc).lower():
                                raise
                            report["checks"].append({"name": label + " rejected", "status": "passed"})
                        else:
                            raise AssertionError(label + " was accepted")
                    attach = (
                        f"ATTACH {literal(catalog)} (TYPE vgi, LOCATION {literal(location)}, "
                        f"bearer_token {literal(WORKER_TOKEN)})"
                    )
                report["attach_sql"] = attach.replace(WORKER_TOKEN, "<redacted>")
                con.execute(attach)
                if args.http_auth and manifest.get("key_required"):
                    try:
                        con.execute(manifest["credential_check_sql"])
                    except Exception as exc:
                        if prefix + "_API_KEY" not in str(exc):
                            raise
                        report["checks"].append(
                            {"name": "worker bearer does not replace upstream credential", "status": "passed"}
                        )
                    else:
                        raise AssertionError("Query accepted without its required upstream credential")
                if args.http_auth and manifest.get("secret_type"):
                    con.execute(f"CREATE SECRET fixture (TYPE {manifest['secret_type']}, API_KEY {literal(key)})")
                for check in manifest["live_checks" if args.live else "checks"]:
                    actual = json.loads(json.dumps(con.execute(check["sql"]).fetchall(), default=str))
                    if actual != check["rows"]:
                        raise AssertionError(f"{check['name']}: expected {check['rows']!r}, got {actual!r}")
                    report["checks"].append({"name": check["name"], "status": "passed"})
                for check in manifest.get("error_checks", []) if not args.live else []:
                    try:
                        con.execute(check["sql"]).fetchall()
                    except Exception as exc:
                        message = str(exc)
                        if check["message"] not in message:
                            raise AssertionError(check["name"] + ": unexpected error") from None
                        if key and key in message:
                            raise AssertionError(check["name"] + ": credential leaked") from None
                        report["checks"].append({"name": check["name"], "status": "passed"})
                    else:
                        raise AssertionError(check["name"] + ": expected failure")
                con.execute(f'DETACH "{catalog}"')
                report["status"] = "passed"
            finally:
                con.close()
    except Exception as exc:
        message = str(exc)
        report["error"] = (message.replace(key, "<redacted>") if key else message).replace(WORKER_TOKEN, "<redacted>")
    finally:
        if worker:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)
        if server:
            server.shutdown()
            server.server_close()
        if thread:
            thread.join(timeout=5)
    output = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
    print(output, end="")
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
