from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class TushareProbeScriptContractTest(unittest.TestCase):
    def test_missing_token_returns_a_safe_structured_diagnostic(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        script = repository / "scripts" / "tushare_probe.py"
        self.assertTrue(script.is_file(), "the standalone Tushare probe script must exist")
        environment = dict(os.environ)
        environment.pop("TUSHARE_TOKEN", None)

        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )

        self.assertEqual(2, result.returncode)
        self.assertEqual(
            {"status": "configuration_error", "error": "TUSHARE_TOKEN is not set"},
            json.loads(result.stdout),
        )
        self.assertEqual("", result.stderr)

    def test_daily_request_sends_token_in_tushare_json_body(self) -> None:
        received: dict[str, object] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers["Content-Length"])
                received["request"] = json.loads(self.rfile.read(length))
                response = {
                    "code": 0,
                    "msg": "",
                    "data": {"fields": ["ts_code"], "items": [["000001.SZ"], ["600000.SH"]]},
                }
                body = json.dumps(response).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        repository = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment.update(
            {
                "TUSHARE_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
                "TUSHARE_TOKEN": "token-for-probe-test",
                "TUSHARE_TRADE_DATE": "20260807",
            }
        )

        result = subprocess.run(
            [sys.executable, str(repository / "scripts" / "tushare_probe.py")],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("ok", json.loads(result.stdout)["status"])
        self.assertEqual(2, json.loads(result.stdout)["rows"])
        self.assertEqual(
            {
                "api_name": "daily",
                "fields": "ts_code",
                "params": {"trade_date": "20260807"},
                "token": "token-for-probe-test",
            },
            received["request"],
        )


if __name__ == "__main__":
    unittest.main()
