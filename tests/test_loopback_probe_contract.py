"""Contract for the deployment-owned loopback HTTP readiness probe."""

from __future__ import annotations

import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class LoopbackProbeContractTest(unittest.TestCase):
    def test_probe_reads_and_closes_a_successful_loopback_readiness_response(self) -> None:
        try:
            from stock_mcp.loopback_probe import probe
        except ImportError as error:
            self.fail(f"loopback deployment probe is not implemented: {error}")

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - HTTPServer callback
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "18")
                self.end_headers()
                self.wfile.write(b'{"status":"ready"}')

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertTrue(probe(f"http://127.0.0.1:{server.server_port}/readyz"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
