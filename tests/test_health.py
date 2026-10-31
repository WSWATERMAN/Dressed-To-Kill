"""HTTP tests for the lightweight application readiness endpoint."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.server as application_server
from app.portfolio import PaperPortfolio
from app.provider import KalshiDemoProvider, MockProvider


class HealthEndpointTests(unittest.TestCase):
    def request(self, store, provider, path="/healthz"):
        result = {}
        handler = SimpleNamespace(
            path=path,
            _json=lambda status, payload: result.update(status=status, payload=payload),
        )
        with patch.object(application_server, "STORE", store), patch.object(application_server, "PROVIDER", provider):
            application_server.Handler.do_GET(handler)
        return result["status"], json.dumps(result["payload"]).encode()

    def test_healthz_reports_database_and_mock_provider_without_sensitive_details(self):
        with tempfile.TemporaryDirectory() as folder:
            store = PaperPortfolio(Path(folder) / "private-path.sqlite3")
            status, body = self.request(store, MockProvider())

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok", "database": "ok", "provider": "mock"})
        self.assertNotIn(b"private-path", body)

    def test_healthz_reports_database_failure_without_leaking_exception(self):
        class BrokenStore:
            def health_check(self):
                raise sqlite3.OperationalError("database unavailable at /private/secret.sqlite3")

        status, body = self.request(BrokenStore(), KalshiDemoProvider())
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body), {"status": "error", "database": "error", "provider": "kalshi_demo"})
        self.assertNotIn(b"secret", body)
        self.assertNotIn(b"/private", body)

    def test_existing_api_health_route_remains_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            store = PaperPortfolio(Path(folder) / "portfolio.sqlite3")
            status, body = self.request(store, MockProvider(), "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
