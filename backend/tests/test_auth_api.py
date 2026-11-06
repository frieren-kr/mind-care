"""실제 라우터·암호 검증·인증 의존성 실행. DB·AI만 대체한다."""
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from psycopg import OperationalError
from pydantic import SecretStr

from app.api import auth, recommendations, users
from app.auth.config import auth_settings
from app.auth.rate_limit import LoginLimiter
from app.auth.security import PASSWORD_HASHER, create_access_token
from app.auth_schemas import AuthUser
from app.db.auth_functions import AuthCredential
from app.db import functions
from app.user_schemas import FeedSettingsOut, UserProfileContext
from tests.test_auth_security import TEST_KEY

PASSWORD = "Auth test password 2026!"
PASSWORD_HASH = PASSWORD_HASHER.hash(PASSWORD)


class AuthApiTests(TestCase):
    def setUp(self):
        self.user = AuthUser(id=uuid4(), name="인증 테스트", email="auth-test@example.com", feed_refresh_days=3)
        self.sid = uuid4()
        self.credential = AuthCredential(self.user, PASSWORD_HASH, True)
        self.sessions = {self.sid: self.user.id}
        self.app = FastAPI()
        for router in (auth.router, users.router, recommendations.router):
            self.app.include_router(router)
        self.execution_init = Mock()

        def execution():
            self.execution_init()
            return None

        self.app.dependency_overrides[recommendations.get_feed_execution] = execution
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.patch(auth_settings, "auth_jwt_secret", SecretStr(TEST_KEY))
        self.patch(auth_settings, "auth_access_token_minutes", 15)
        self.patch("app.auth.rate_limit.login_limiter", new=LoginLimiter(limit=1000))
        self.patch("psycopg.connect", side_effect=AssertionError("DB access forbidden"))
        self.patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("External HTTP forbidden"))
        self.lookup = self.patch(functions, "get_auth_credentials", return_value=self.credential)
        self.create = self.patch(functions, "create_auth_session", side_effect=self.create_session)
        self.session = self.patch(functions, "get_auth_session_user", side_effect=self.session_user)
        self.revoke = self.patch(functions, "revoke_auth_session", side_effect=self.revoke_session)
        self.profile = self.patch(users.user_functions, "get_user_profile_context", return_value=UserProfileContext(
            user_id=self.user.id, name=self.user.name, generated_at=datetime.now(timezone.utc), patients=[]))
        self.settings_read = self.patch(users.user_functions, "get_user_feed_settings",
                                       return_value=FeedSettingsOut(user_id=self.user.id, feed_refresh_days=3))
        self.settings_write = self.patch(users.user_functions, "update_user_feed_settings",
                                        return_value=FeedSettingsOut(user_id=self.user.id, feed_refresh_days=5))
        self.caregiver = self.patch(users.user_functions, "get_caregiver_context")
        self.owns_patient = self.patch(users.user_functions, "is_caregiver_of", return_value=False)
        self.patient = self.patch(users.user_functions, "get_patient_context")
        self.search = self.patch(recommendations, "search_recommendations")
        self.cache = self.patch(recommendations, "load_cached_feed")

    def patch(self, target, attribute=None, *args, **kwargs):
        patcher = patch(target, **kwargs) if isinstance(target, str) else patch.object(target, attribute, *args, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def create_session(self, user_id, session_id, **kwargs):
        self.sessions[session_id] = user_id
        return True

    def session_user(self, user_id, session_id, **kwargs):
        return self.user if self.sessions.get(session_id) == user_id else None

    def revoke_session(self, user_id, session_id, **kwargs):
        if self.sessions.get(session_id) == user_id:
            del self.sessions[session_id]

    def headers(self, user_id=None, *, expired=False):
        now = datetime.now(timezone.utc)
        token = create_access_token(user_id or self.user.id, self.sid,
                                    issued_at=now - timedelta(minutes=20) if expired else now,
                                    expires_at=now - timedelta(minutes=5) if expired else now + timedelta(minutes=15))
        return {"Authorization": f"Bearer {token}"}

    def login(self, **changes):
        return self.client.post("/auth/login", json={"email": self.user.email, "password": PASSWORD, **changes})

    def test_login_me_logout_and_repeated_logout(self):
        response = self.login(email="  AUTH-TEST@EXAMPLE.COM  ")
        self.assertEqual(response.status_code, 200, response.text)
        self.lookup.assert_called_once_with(self.user.email)
        body = response.json()
        self.assertEqual(set(body), {"access_token", "token_type", "expires_in", "expires_at", "user"})
        self.assertEqual(body["user"], self.user.model_dump(mode="json"))
        self.assertTrue(0 < body["expires_in"] <= 900)
        self.assertNotIn(PASSWORD, response.text)
        self.assertNotIn(PASSWORD_HASH, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        headers = {"Authorization": "Bearer " + body["access_token"]}
        self.assertEqual(self.client.get("/auth/me", headers=headers).json(), body["user"])
        for _ in range(2):
            response = self.client.post("/auth/logout", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(self.client.get("/auth/me", headers=headers).status_code, 401)
        self.assertEqual(self.client.get("/auth/me", headers=self.headers()).status_code, 200)

    def test_login_failures_have_same_response_and_create_no_session(self):
        bodies = []
        for credential, password in ((None, PASSWORD), (self.credential, "wrong"),
                                     (AuthCredential(self.user, PASSWORD_HASH, False), PASSWORD),
                                     (AuthCredential(self.user, "broken-hash", True), PASSWORD)):
            with self.subTest(credential=credential):
                self.lookup.return_value = credential
                response = self.login(password=password)
                self.assertEqual(response.status_code, 401)
                bodies.append(response.json())
        self.assertTrue(all(body == bodies[0] for body in bodies))
        self.create.assert_not_called()

    def test_account_changed_during_login_is_rejected(self):
        self.create.side_effect = None
        self.create.return_value = False
        self.assertEqual(self.login().status_code, 401)

    def test_invalid_inputs_do_not_echo_password_or_extra_fields(self):
        secret = "sensitive-marker"
        bodies = [dict(email="invalid", password=secret), dict(email=self.user.email, password=secret * 20),
                  dict(email=self.user.email, password={"secret": secret}), dict(password=secret),
                  dict(email=self.user.email, password=secret, private=secret),
                  dict(email=self.user.email, password=""), [secret]]
        for body in bodies:
            response = self.client.post("/auth/login", json=body)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn(secret, response.text)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        malformed = self.client.post("/auth/login", content='{"password":"sensitive-marker",',
                                     headers={"Content-Type": "application/json"})
        self.assertEqual(malformed.status_code, 422)
        self.assertNotIn(secret, malformed.text)
        self.lookup.assert_not_called()

    def test_login_limit_and_recovery(self):
        self.patch("app.auth.rate_limit.login_limiter", new=LoginLimiter(limit=2, window=60))
        with patch("app.auth.rate_limit.monotonic", return_value=100):
            self.assertEqual(self.login(password="wrong").status_code, 401)
            self.assertEqual(self.login(password="wrong").status_code, 401)
            response = self.login()
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers["Retry-After"], "60")
            self.assertEqual(self.lookup.call_count, 2)
        with patch("app.auth.rate_limit.monotonic", return_value=160):
            self.assertEqual(self.login().status_code, 200)

    def test_limiter_bounds_memory_and_ignores_forwarded_header(self):
        limiter = LoginLimiter(limit=1, max_clients=1)
        limiter.check("a")
        with self.assertRaises(HTTPException) as caught:
            limiter.check("b")
        self.assertEqual(caught.exception.status_code, 429)
        self.patch("app.auth.rate_limit.login_limiter", new=LoginLimiter(limit=1))
        for index in range(2):
            response = self.client.post("/auth/login", json={"email": self.user.email, "password": "wrong"},
                                        headers={"X-Forwarded-For": f"192.0.2.{index}"})
            self.assertEqual(response.status_code, 401 if index == 0 else 429)

    def test_config_and_db_failures_are_503_not_bad_password(self):
        with patch.object(auth_settings, "auth_jwt_secret", SecretStr("")):
            self.assertEqual(self.login().status_code, 503)
            self.lookup.assert_not_called()
        self.lookup.side_effect = OperationalError("private-database-detail")
        response = self.login()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-database-detail", response.text)
        self.session.side_effect = OperationalError("private-database-detail")
        self.assertEqual(self.client.get("/auth/me", headers=self.headers()).status_code, 503)
        self.revoke.side_effect = OperationalError("private-database-detail")
        self.assertEqual(self.client.post("/auth/logout", headers=self.headers()).status_code, 503)

    def test_bad_bearer_and_expired_tokens_do_not_query_db(self):
        for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"},
                        {"Authorization": "Bearer invalid"}, self.headers(expired=True)):
            response = self.client.get("/auth/me", headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
        self.session.assert_not_called()

    def test_session_user_mismatch_and_inactive_session_are_rejected(self):
        self.assertEqual(self.client.get("/auth/me", headers=self.headers(uuid4())).status_code, 401)
        self.sessions.clear()
        self.assertEqual(self.client.get("/auth/me", headers=self.headers()).status_code, 401)

    def protected_requests(self, user_id):
        return [("GET", f"/feed/{user_id}", None), ("GET", f"/feed_user/{user_id}", None),
                ("GET", f"/feed_patient/{user_id}/{uuid4()}", None),
                ("GET", f"/users/{user_id}/feed-settings", None),
                ("PATCH", f"/users/{user_id}/feed-settings", {"feed_refresh_days": 5}),
                ("GET", f"/recommendations/{user_id}", None)]

    def test_missing_and_other_user_auth_block_all_business_work(self):
        for uid, headers, expected in ((self.user.id, {}, 401), (uuid4(), self.headers(), 403)):
            for method, url, body in self.protected_requests(uid):
                with self.subTest(method=method, url=url, expected=expected):
                    response = self.client.request(method, url, json=body, headers=headers)
                    self.assertEqual(response.status_code, expected, response.text)
        for mock in (self.profile, self.settings_read, self.settings_write, self.caregiver, self.owns_patient,
                     self.patient, self.search, self.cache, self.execution_init):
            mock.assert_not_called()

    def test_own_profile_and_settings_keep_existing_contract(self):
        headers = self.headers()
        response = self.client.get(f"/feed/{self.user.id}", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["patients"], [])
        url = f"/users/{self.user.id}/feed-settings"
        self.assertEqual(self.client.get(url, headers=headers).json()["feed_refresh_days"], 3)
        self.assertEqual(self.client.patch(url, headers=headers, json={"feed_refresh_days": 5}).status_code, 200)
        self.assertEqual(self.client.patch(url, headers=headers, json={"feed_refresh_days": 8}).status_code, 422)
        self.assertEqual(self.settings_write.call_count, 1)

    def test_authenticated_recommendation_works_with_zero_patients(self):
        from app.schemas import RecommendationResponse

        self.search.return_value = RecommendationResponse(user_id=self.user.id, mode="search", items=[])
        response = self.client.get(f"/recommendations/{self.user.id}", headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["mode"], "search")
        self.execution_init.assert_called_once()
        self.profile.assert_called_once_with(self.user.id)
        self.search.assert_called_once_with(self.user.id, None, [], 5, caregiver_interests=[])

    def test_foreign_patient_is_404_before_detail_or_recommendation(self):
        foreign = uuid4()
        headers = self.headers()
        self.assertEqual(self.client.get(f"/feed_patient/{self.user.id}/{foreign}", headers=headers).status_code, 404)
        response = self.client.get(f"/recommendations/{self.user.id}?patient_id={foreign}", headers=headers)
        self.assertEqual(response.status_code, 404)
        self.patient.assert_not_called()
        self.search.assert_not_called()
        self.cache.assert_not_called()

    def test_openapi_declares_bearer_for_protected_paths(self):
        paths = self.app.openapi()["paths"]
        self.assertNotIn("security", paths["/auth/login"]["post"])
        for path, method in (("/auth/me", "get"), ("/auth/logout", "post"),
                             ("/feed/{user_id}", "get"), ("/recommendations/{user_id}", "get"),
                             ("/users/{user_id}/feed-settings", "patch")):
            self.assertEqual(paths[path][method]["security"], [{"HTTPBearer": []}])

    def test_main_registers_auth_and_preserves_public_endpoints(self):
        from main import app
        from app.schemas import PaperListResponse

        self.patch(functions, "list_papers", return_value=PaperListResponse(total=0, limit=20, offset=0, items=[]))
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/papers").status_code, 200)
            self.assertEqual(client.get("/auth/me").status_code, 401)
            self.assertEqual(client.post("/auth/login", json={"email": self.user.email, "password": PASSWORD}).status_code, 200)
