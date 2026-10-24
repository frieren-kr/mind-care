"""개발용 CORS. development에서 허용 출처에만 Access-Control-Allow-Origin이 붙는지 확인한다."""
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main


def build_client(app_env: str) -> TestClient:
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    with patch.object(main.settings, "app_env", app_env):
        main._add_dev_cors(app)
    return TestClient(app)


class DevCorsTests(unittest.TestCase):
    def test_development_allows_localhost_8081(self):
        client = build_client("development")
        for origin in ("http://localhost:8081", "http://127.0.0.1:8081"):
            response = client.get("/ping", headers={"Origin": origin})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers.get("access-control-allow-origin"), origin)

    def test_development_rejects_other_origin(self):
        client = build_client("development")
        response = client.get("/ping", headers={"Origin": "http://evil.example"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_production_has_no_cors(self):
        client = build_client("production")
        response = client.get("/ping", headers={"Origin": "http://localhost:8081"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("access-control-allow-origin", response.headers)


if __name__ == "__main__":
    unittest.main()
