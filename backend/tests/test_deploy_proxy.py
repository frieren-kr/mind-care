"""배포 프록시 설정 테스트. DB·nginx 없이 돌린다.

- uvicorn --proxy-headers 뒤에서 로그인 제한이 X-Forwarded-For(실제 사용자 IP) 기준으로 세는지
- deploy/ 설정끼리 값이 맞는지 (web 고정 IP = backend --forwarded-allow-ips, nginx가 XFF를 덮어쓰는지)

실행 (backend/ 에서):  python -m unittest tests.test_deploy_proxy
"""
import ipaddress
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api import auth
from app.auth.config import auth_settings
from app.auth.rate_limit import LoginLimiter
from app.db import functions
from tests.test_auth_security import TEST_KEY

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


class LoginLimitBehindProxyTests(unittest.TestCase):
    def setUp(self):
        for p in (
            patch("app.auth.rate_limit.login_limiter", new=LoginLimiter(limit=1)),
            patch.object(auth_settings, "auth_jwt_secret", SecretStr(TEST_KEY)),
            patch.object(functions, "get_auth_credentials", return_value=None),
            patch("psycopg.connect", side_effect=AssertionError("DB access forbidden")),
        ):
            p.start()
            self.addCleanup(p.stop)

    def login(self, trusted, ip):
        app = FastAPI()
        app.include_router(auth.router)
        # TestClient의 접속 주소는 "testclient" — 그걸 nginx 자리로 믿을지 말지만 바꾼다.
        client = TestClient(ProxyHeadersMiddleware(app, trusted_hosts=trusted))
        return client.post("/auth/login", json={"email": "x@example.com", "password": "wrong password!"},
                           headers={"X-Forwarded-For": ip}).status_code

    def test_trusted_proxy_limits_per_forwarded_ip(self):
        self.assertEqual(self.login("testclient", "192.0.2.1"), 401)
        self.assertEqual(self.login("testclient", "192.0.2.1"), 429)
        self.assertEqual(self.login("testclient", "192.0.2.2"), 401)  # 다른 사용자는 막히지 않는다

    def test_untrusted_sender_cannot_dodge_limit_with_header(self):
        self.assertEqual(self.login("10.231.0.10", "192.0.2.1"), 401)
        self.assertEqual(self.login("10.231.0.10", "192.0.2.2"), 429)  # 헤더 무시 → 같은 접속 IP로 센다


class DeployConfigTests(unittest.TestCase):
    def test_forwarded_allow_ip_matches_web_static_ip(self):
        compose = (DEPLOY / "docker-compose.prod.yml").read_text(encoding="utf-8")
        allowed = re.search(r'"--forwarded-allow-ips",\s*"([^"]+)"', compose).group(1)
        web_ip = re.search(r"ipv4_address:\s*(\S+)", compose).group(1)
        subnet = re.search(r"subnet:\s*(\S+)", compose).group(1)
        self.assertEqual(allowed, web_ip)
        self.assertIn("--proxy-headers", compose)
        self.assertIn(ipaddress.ip_address(web_ip), ipaddress.ip_network(subnet))

    def test_nginx_api_overwrites_xff_and_passes_bearer(self):
        conf = (DEPLOY / "nginx.conf").read_text(encoding="utf-8")
        api = re.search(r"location /api/ \{(.*?)\n    \}", conf, re.S).group(1)
        self.assertIn("auth_basic off;", api)
        self.assertRegex(api, r"proxy_set_header X-Forwarded-For \$remote_addr;")
        self.assertNotIn("Authorization", re.sub(r"#.*", "", api))  # 지우면 Bearer 토큰이 백엔드에 안 간다


if __name__ == "__main__":
    unittest.main()
