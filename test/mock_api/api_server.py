"""Synthetic official API envelopes for offline SQL and authentication contracts."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

API_KEY = "public-api-no-upstream-key"


class ApiServer(ThreadingHTTPServer):
    """Local service with controllable transport and response failures."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests: list[dict[str, Any]] = []
        self.mode = ""
        self.attempts = 0


class Handler(BaseHTTPRequestHandler):
    """Serve synthetic metadata and data without writing request logs."""

    server: ApiServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        self.server.requests.append({"path": url.path, "query": query})
        self.server.attempts += 1
        database = query.get("db", ["IR01"])[0]
        mode = self.server.mode or (database if database != "IR01" else "")
        position = int(query.get("startPosition", ["1"])[0])
        result: dict[str, Any] = {"STATUS": 200, "MESSAGEID": "M181000I", "DATE": "2024-02-01T9:00:00+09:00"}

        def record(code: str) -> dict[str, Any]:
            row = {
                "SERIES_CODE": code,
                "NAME_OF_TIME_SERIES": "Fixture rate" if code else "Fixture heading",
                "UNIT": "Percent per annum" if code else "",
                "FREQUENCY": "DAILY" if code else "",
                "CATEGORY": "Fixture rates",
                "LAST_UPDATE": "20240201" if code else "",
                "START_OF_THE_TIME_SERIES": "20240101",
                "END_OF_THE_TIME_SERIES": "20240103",
                "NOTES": "Fixture note",
                # Like the real hierarchy: the heading is layer 1, its series layer 1,1.
                **{f"LAYER{i}": 1 if i < (3 if code else 2) else 0 for i in range(1, 6)},
            }
            if query.get("lang") == ["jp"]:
                row.update(NAME_OF_TIME_SERIES_J="金利", UNIT_J="年率\uff05", CATEGORY_J="金利データ")
            return row

        if "getMetadata" in url.path:
            result.update(DB=database, RESULTSET=[record(""), record("MADR1Z@D"), record("MADR1M")])
        else:
            selected = query.get("code", ["MADR1Z@D,OTHERD"])[0].split(",")
            current = selected[position - 1 : position]
            if mode == "REPEAT" and position > 1:
                current = selected[:1]
            rows = []
            for code in current:
                row = record(code)
                # Daily dates fall in the requested start month (default January 2024).
                start = query.get("startDate", [""])[0]
                month = int(start) if len(start) == 6 else 202401
                dates = [month * 100 + day for day in (1, 2, 3)]
                row["VALUES"] = {"SURVEY_DATES": dates, "VALUES": ["0.30", None, "0.35"]}
                if mode == "BADARRAY":
                    row["VALUES"]["VALUES"].pop()
                if mode == "BADVALUE":
                    row["VALUES"]["VALUES"][0] = "Infinity"
                if mode == "DUPLICATE":
                    row["VALUES"]["SURVEY_DATES"][1] = 20240101
                rows.append(row)
            next_position = position + 1 if position < len(selected) else None
            if mode == "STALLED":
                next_position = position
            if mode == "EMPTY" or query.get("startDate", [""])[0] > "202412":
                rows, next_position = [], None
                result["MESSAGEID"] = "M181030I"
            if mode == "WRONGPOS":
                position += 1
            result.update(
                RESULTSET=rows, NEXTPOSITION=next_position, PARAMETER={"DB": database, "STARTPOSITION": str(position)}
            )
        if mode == "APIERROR":
            result.update(STATUS=400, MESSAGEID="M181013E", MESSAGE="secret-sentinel-never-echo")
        body = json.dumps(result, ensure_ascii=False).encode()
        status = 200 if url.path in {"/getMetadata", "/getDataCode", "/getDataLayer"} else 404
        if mode == "HTML":
            body = b"<html>Not JSON</html>"
        if mode == "RETRY" and self.server.attempts == 1:
            status, body = 429, b""
        if mode == "REDIRECT":
            status, body = 302, b""
        if mode == "ERROR":
            status, body = 400, b"secret-sentinel-never-echo"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if status == 429:
            self.send_header("Retry-After", "0")
        if status == 302:
            self.send_header("Location", "https://example.invalid/")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ApiServer()
    print(f"PORT:{server.server_port}", flush=True)
    server.serve_forever()
