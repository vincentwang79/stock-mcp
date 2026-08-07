from __future__ import annotations

import unittest


class McpHttpRuntimeContractTest(unittest.TestCase):
    def test_real_sdk_mounts_health_readiness_and_rejects_untrusted_origin(self) -> None:
        try:
            from starlette.testclient import TestClient

            from stock_mcp.mcp_server import create_server
            from tests.mcp.test_tool_catalog_contract import FakeApplicationService
        except ImportError as error:
            self.skipTest(f"MCP HTTP runtime unavailable: {error}")

        server = create_server(
            FakeApplicationService(),
            health_provider=lambda: {"healthz": "healthy", "readyz": "ready"},
        )
        app = server.streamable_http_app(
            streamable_http_path="/mcp", stateless_http=True, json_response=True
        )
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "contract", "version": "1"},
            },
        }
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            self.assertEqual(200, client.get("/healthz").status_code)
            self.assertEqual(200, client.get("/readyz").status_code)
            rejected = client.post(
                "/mcp",
                json=initialize,
                headers={
                    "Origin": "https://attacker.invalid",
                    "Accept": "application/json, text/event-stream",
                },
            )

        self.assertEqual(403, rejected.status_code)


if __name__ == "__main__":
    unittest.main()
