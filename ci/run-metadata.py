"""Run metadata assurance against deterministic local API fixtures."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    """Isolate behavioral probes from public API quotas and operator credentials."""
    from test.mock_api.api_server import API_KEY, ApiServer

    prefix = json.loads((ROOT / "vgi-worker.json").read_text())["catalog"].upper()
    server = ApiServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = os.environ.copy()
    environment.update(
        {
            prefix + "_API_KEY": API_KEY,
            prefix + "_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            prefix + "_ALLOW_INSECURE_HTTP": "1",
            prefix + "_MIN_INTERVAL_SECONDS": "0",
        }
    )
    try:
        result = subprocess.run(sys.argv[1:], cwd=ROOT, env=environment, check=False)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
