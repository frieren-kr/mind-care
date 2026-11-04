"""실제 PostgreSQL + 로그인 HTTP 통합 검증. 전용 스키마 생성 후 전체 롤백.

001·004·005·010·011을 직접 적용하여 인증 FK·UNIQUE를 포함한다.
public 사용자·비밀번호·세션에는 쓰지 않는다. DB 연결 실패는 실패로 보고한다.
"""
from contextlib import contextmanager, redirect_stdout
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from pydantic import SecretStr

from app.api import auth, users
from app.auth.config import auth_settings
from app.auth.rate_limit import LoginLimiter
from app.auth.security import PASSWORD_HASHER
from app.db import auth_functions, functions, user_functions
from app.db.connection import get_connection
from tests.test_auth_security import TEST_KEY

PASSWORD = "DB auth test password 2026!"
HASH = PASSWORD_HASHER.hash(PASSWORD)


class AuthDbTests(TestCase):
    def setUp(self):
        db = get_connection()
        self.conn = db.__enter__()
        self.addCleanup(db.__exit__, None, None, None)
        self.addCleanup(self.conn.rollback)
        schema = sql.Identifier(f"test_auth_{uuid4().hex}")
        self.conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        self.conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(schema))
        migrations = Path(__file__).resolve().parents[1] / "app/db/migrations"
        for name in ("001_init.sql", "004_patient_management.sql", "005_medical_visits_detail.sql",
                     "010_user_feed_refresh_days.sql", "011_authentication.sql"):
            self.conn.execute((migrations / name).read_text())
        self.migration = (migrations / "011_authentication.sql").read_text()
        self.uid = self.conn.execute(
            "INSERT INTO users(name,email) VALUES ('인증 테스트','db-auth@example.com') RETURNING id"
        ).fetchone()[0]
        self.other = self.conn.execute(
            "INSERT INTO users(name,email) VALUES ('다른 계정','db-other@example.com') RETURNING id"
        ).fetchone()[0]
        self.pid = self.conn.execute(
            "INSERT INTO patient_profiles(caregiver_id,name) VALUES (%s,'환자') RETURNING id", (self.uid,)
        ).fetchone()[0]
        self.conn.execute("INSERT INTO user_credentials(user_id,password_hash) VALUES (%s,%s)", (self.uid, HASH))
        self.now = datetime.now(timezone.utc)

        @contextmanager
        def connection():
            # 실제 함수의 SQL과 트랜잭션 동작을 실행하면서 전체 테스트는 롤백한다.
            with self.conn.transaction():
                yield self.conn

        for target, attribute, value in (
            (auth_functions, "get_connection", connection), (user_functions, "get_connection", connection),
            (auth_settings, "auth_jwt_secret", SecretStr(TEST_KEY)),
        ):
            patcher = patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        limiter = patch("app.auth.rate_limit.login_limiter", new=LoginLimiter(limit=100))
        limiter.start()
        self.addCleanup(limiter.stop)
        app = FastAPI()
        app.include_router(auth.router)
        app.include_router(users.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def create(self, **changes):
        sid = uuid4()
        args = dict(password_hash=HASH, created_at=self.now, expires_at=self.now + timedelta(minutes=15))
        args.update(changes)
        result = functions.create_auth_session(self.uid, sid, **args)
        return sid, result

    def user(self, sid, **changes):
        return functions.get_auth_session_user(changes.get("uid", self.uid), sid,
                                               reference_time=changes.get("at", self.now))

    def login(self):
        response = self.client.post("/auth/login", json={"email": "db-auth@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": "Bearer " + response.json()["access_token"]}

    def test_end_to_end_real_db_and_profiles_preserved(self):
        before = user_functions.get_user_profile_context(self.uid)
        headers = self.login()
        self.assertEqual(self.client.get("/auth/me", headers=headers).json()["id"], str(self.uid))
        profile = self.client.get(f"/feed/{self.uid}", headers=headers)
        self.assertEqual(profile.status_code, 200, profile.text)
        self.assertEqual(profile.json()["patients"][0]["patient"]["id"], str(self.pid))
        settings_url = f"/users/{self.uid}/feed-settings"
        self.assertEqual(self.client.patch(settings_url, json={"feed_refresh_days": 5}, headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/auth/me", headers=headers).json()["feed_refresh_days"], 5)
        self.assertEqual(self.client.get(f"/feed/{self.other}", headers=headers).status_code, 403)
        self.assertEqual(self.client.get(f"/feed/{self.uid}").status_code, 401)
        for _ in range(2):
            self.assertEqual(self.client.post("/auth/logout", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/auth/me", headers=headers).status_code, 401)
        self.assertEqual(self.client.get(f"/feed/{self.uid}", headers=headers).status_code, 401)
        self.assertEqual(user_functions.get_user_profile_context(self.uid).patients, before.patients)
        stored_hash = self.conn.execute("SELECT password_hash FROM user_credentials").fetchone()[0]
        self.assertEqual(stored_hash, HASH)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM auth_refresh_tokens").fetchone()[0], 0)

    def test_lookup_normalization_and_account_without_credentials(self):
        row = functions.get_auth_credentials("  DB-AUTH@EXAMPLE.COM  ")
        self.assertEqual(row.user.id, self.uid)
        self.assertNotIn(HASH, repr(row))
        self.assertIsNone(functions.get_auth_credentials("db-other@example.com"))
        self.assertIsNone(functions.get_auth_credentials("missing@example.com"))
        response = self.client.post("/auth/login", json={"email": "db-other@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 401)

    def test_session_ownership_expiry_and_exact_boundary(self):
        sid, ok = self.create()
        self.assertTrue(ok)
        self.assertEqual(self.user(sid).id, self.uid)
        self.assertIsNone(self.user(sid, uid=self.other))
        self.assertIsNone(self.user(sid, at=self.now - timedelta(seconds=1)))
        self.assertIsNotNone(self.user(sid, at=self.now + timedelta(minutes=15) - timedelta(microseconds=1)))
        self.assertIsNone(self.user(sid, at=self.now + timedelta(minutes=15)))

    def test_revoke_is_idempotent_and_does_not_revoke_other_sessions(self):
        sid, _ = self.create()
        other_sid, _ = self.create()
        functions.revoke_auth_session(self.other, sid, reference_time=self.now)
        self.assertIsNotNone(self.user(sid))
        for _ in range(2):
            functions.revoke_auth_session(self.uid, sid, reference_time=self.now)
        self.assertIsNone(self.user(sid))
        self.assertIsNotNone(self.user(other_sid))

    def test_disabling_account_or_password_change_invalidates_existing_session(self):
        sid, _ = self.create()
        self.conn.execute("UPDATE user_credentials SET is_active=false WHERE user_id=%s", (self.uid,))
        self.assertIsNone(self.user(sid))
        self.assertFalse(self.create()[1])
        self.conn.execute("UPDATE user_credentials SET is_active=true, password_changed_at=%s WHERE user_id=%s",
                          (self.now + timedelta(seconds=1), self.uid))
        self.assertIsNone(self.user(sid, at=self.now + timedelta(seconds=2)))
        self.assertFalse(self.create()[1])

    def test_changed_hash_between_verify_and_create_does_not_create_session(self):
        self.conn.execute("UPDATE user_credentials SET password_hash='changed-hash' WHERE user_id=%s", (self.uid,))
        self.assertFalse(self.create()[1])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM auth_sessions").fetchone()[0], 0)

    def test_deleting_account_cascades_auth_data(self):
        sid, _ = self.create()
        self.conn.execute("DELETE FROM users WHERE id=%s", (self.uid,))
        self.assertIsNone(self.user(sid))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM auth_sessions").fetchone()[0], 0)
        self.assertIsNone(functions.get_auth_credentials("db-auth@example.com"))

    def test_real_foreign_keys_normalized_uniqueness_and_migration_rerun(self):
        sid, _ = self.create()
        with self.assertRaises(ForeignKeyViolation), self.conn.transaction():
            self.conn.execute("INSERT INTO user_credentials(user_id,password_hash) VALUES (%s,%s)", (uuid4(), HASH))
        with self.assertRaises(UniqueViolation), self.conn.transaction():
            self.conn.execute("INSERT INTO users(name,email) VALUES ('중복',' DB-AUTH@EXAMPLE.COM ')")
        self.conn.execute(self.migration)
        self.assertIsNotNone(self.user(sid))
        self.assertEqual(functions.get_auth_credentials("db-auth@example.com").password_hash, HASH)

    def test_invalid_time_contract_is_rejected_before_write(self):
        for changes in ({"expires_at": self.now}, {"created_at": self.now.replace(tzinfo=None)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.create(**changes)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM auth_sessions").fetchone()[0], 0)

    def test_manual_check_script_against_real_db_hides_secrets_and_revokes_session(self):
        from scripts.check_auth import run_checks

        output = io.StringIO()
        with redirect_stdout(output):
            run_checks(self.client, "db-auth@example.com", PASSWORD)
        self.assertIn("로그아웃한 토큰 차단", output.getvalue())
        for secret in (PASSWORD, HASH, "access_token", "Bearer "):
            self.assertNotIn(secret, output.getvalue())
        self.assertEqual(self.conn.execute("SELECT count(*) FROM auth_sessions WHERE revoked_at IS NULL").fetchone()[0], 0)
