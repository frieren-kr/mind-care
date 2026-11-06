"""기존 간병인과 인증 정보를 연결하는 PostgreSQL 통합 테스트.

실행: python -m unittest tests.test_seed_dev_auth -v
기존 DB의 테이블 구조를 복사한 격리 스키마에 예시 간병인·환자를 만들고 011을 적용한다.
public 데이터에는 쓰지 않으며 테스트마다 스키마와 데이터를 모두 롤백한다.
"""
from contextlib import contextmanager, redirect_stdout
import io
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from psycopg import sql

from app.db.connection import get_connection
from scripts import seed_dev_auth as auth


PASSWORD = "Test-only password 2026!"
OTHER_PASSWORD = "Another test password 2026!"
EMAIL_A = "dev-seed-auth-a@example.com"
EMAIL_B = "dev-seed-auth-b@example.com"


class SeedDevAuthDbTests(unittest.TestCase):
    def setUp(self):
        db = get_connection()
        self.conn = db.__enter__()
        self.addCleanup(db.__exit__, None, None, None)
        self.addCleanup(self.conn.rollback)
        schema = sql.Identifier(f"test_seed_auth_{uuid4().hex}")
        self.conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        self.conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(schema))
        for table in ("users", "patient_profiles", "caregiver_profiles"):
            self.conn.execute(sql.SQL("CREATE TABLE {} (LIKE public.{} INCLUDING ALL)").format(
                sql.Identifier(table), sql.Identifier(table),
            ))
        self.ids = {}
        for email in (EMAIL_A, EMAIL_B, "other@example.com"):
            user_id = self.conn.execute(
                "INSERT INTO users(name,email) VALUES ('테스트 간병인',%s) RETURNING id", (email,),
            ).fetchone()[0]
            self.ids[email] = user_id
            self.conn.execute(
                "INSERT INTO patient_profiles(caregiver_id,name) VALUES (%s,'테스트 환자')", (user_id,),
            )
            self.conn.execute(
                "INSERT INTO caregiver_profiles(user_id,relationship) VALUES (%s,'자녀')", (user_id,),
            )
        migration = Path(__file__).resolve().parents[1] / "app/db/migrations/011_authentication.sql"
        self.conn.execute(migration.read_text(encoding="utf-8"))

    def snapshot(self, table):
        return self.conn.execute(sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(table))).fetchall()

    def seed(self, emails=None, password=PASSWORD, **kwargs):
        return auth.seed_credentials(self.conn, emails or [EMAIL_A], password, **kwargs)

    def test_link_existing_caregiver_and_preserve_profiles(self):
        tables = ("users", "patient_profiles", "caregiver_profiles")
        before = {table: self.snapshot(table) for table in tables}
        result = self.seed()[0]
        self.assertEqual(result.user_id, self.ids[EMAIL_A])
        self.assertEqual(result.patient_count, 1)
        self.assertTrue(result.created)
        self.assertEqual(auth.verify_test_login(self.conn, EMAIL_A, PASSWORD), self.ids[EMAIL_A])
        self.assertEqual(auth.verify_test_login(self.conn, f" {EMAIL_A.upper()} ", PASSWORD), self.ids[EMAIL_A])
        self.assertIsNone(auth.verify_test_login(self.conn, EMAIL_A, OTHER_PASSWORD))
        self.assertIsNone(auth.verify_test_login(self.conn, EMAIL_B, PASSWORD))
        self.assertIsNone(auth.verify_test_login(self.conn, "missing@example.com", PASSWORD))
        stored_hash = self.conn.execute("SELECT password_hash FROM user_credentials").fetchone()[0]
        self.assertTrue(stored_hash.startswith("$argon2id$"))
        self.assertNotIn(PASSWORD, stored_hash)
        for table in tables:
            self.assertEqual(self.snapshot(table), before[table])
        self.assertEqual(self.snapshot("auth_sessions"), [])
        self.assertEqual(self.snapshot("auth_refresh_tokens"), [])

    def test_rerun_preserves_hash_timestamps_and_deduplicates_email(self):
        self.seed()
        before = self.snapshot("user_credentials")
        results = self.seed([EMAIL_A, f" {EMAIL_A.upper()} "])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].created)
        self.assertEqual(self.snapshot("user_credentials"), before)

    def test_same_password_has_different_salts_per_user(self):
        self.seed([EMAIL_A, EMAIL_B])
        hashes = self.conn.execute("SELECT password_hash FROM user_credentials").fetchall()
        self.assertEqual(len(hashes), 2)
        self.assertNotEqual(hashes[0], hashes[1])
        for email in (EMAIL_A, EMAIL_B):
            self.assertEqual(auth.verify_test_login(self.conn, email, PASSWORD), self.ids[email])

    def test_existing_password_mismatch_rolls_back_entire_batch(self):
        self.seed([EMAIL_B], password=OTHER_PASSWORD)
        before = self.snapshot("user_credentials")
        with self.assertRaisesRegex(ValueError, "기존 인증 정보"):
            self.seed([EMAIL_A, EMAIL_B])
        self.assertEqual(self.snapshot("user_credentials"), before)
        self.assertEqual(auth.verify_test_login(self.conn, EMAIL_B, OTHER_PASSWORD), self.ids[EMAIL_B])

    def test_inactive_account_stays_inactive(self):
        self.seed()
        self.conn.execute("UPDATE user_credentials SET is_active=false")
        before = self.snapshot("user_credentials")
        self.assertIsNone(auth.verify_test_login(self.conn, EMAIL_A, PASSWORD))
        with self.assertRaisesRegex(ValueError, "기존 인증 정보"):
            self.seed()
        self.assertEqual(self.snapshot("user_credentials"), before)

    def test_malformed_existing_hash_is_rejected_and_preserved(self):
        self.seed()
        self.conn.execute("UPDATE user_credentials SET password_hash='invalid-hash'")
        before = self.snapshot("user_credentials")
        self.assertIsNone(auth.verify_test_login(self.conn, EMAIL_A, PASSWORD))
        with self.assertRaises(ValueError):
            self.seed()
        self.assertEqual(self.snapshot("user_credentials"), before)

    def test_missing_user_rolls_back_and_does_not_create_user(self):
        users = self.snapshot("users")
        with self.assertRaisesRegex(ValueError, "기존 간병인"):
            self.seed([EMAIL_A, "z-missing@example.com"])
        self.assertEqual(self.snapshot("user_credentials"), [])
        self.assertEqual(self.snapshot("users"), users)

    def test_dry_run_verifies_then_rolls_back(self):
        self.seed([EMAIL_B])
        before = self.snapshot("user_credentials")
        results = self.seed([EMAIL_A, EMAIL_B], dry_run=True)
        self.assertEqual([r.created for r in results], [True, False])
        self.assertEqual(self.snapshot("user_credentials"), before)

    def test_password_whitespace_is_preserved(self):
        password = " " + PASSWORD + " "
        self.seed(password=password)
        self.assertEqual(auth.verify_test_login(self.conn, EMAIL_A, password), self.ids[EMAIL_A])
        self.assertIsNone(auth.verify_test_login(self.conn, EMAIL_A, password.strip()))

    def test_invalid_password_or_empty_selection_does_not_write(self):
        for password in ("", "a" * 11, "a" * 129):
            with self.subTest(length=len(password)), self.assertRaises(ValueError):
                self.seed(password=password)
        with self.assertRaisesRegex(ValueError, "대상 간병인"):
            auth.seed_credentials(self.conn, [], PASSWORD)
        self.assertEqual(self.snapshot("user_credentials"), [])

    def test_missing_migration_has_actionable_error(self):
        self.conn.execute("DROP TABLE user_credentials")
        # public에 인증 테이블이 이미 있는 환경에서도 그 테이블을 참조하지 않도록 격리한다.
        schema_name = self.conn.execute("SELECT current_schema()").fetchone()[0]
        self.conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema_name)))
        with self.assertRaisesRegex(ValueError, "011_authentication.sql"):
            self.seed()

    def test_cli_all_dev_profiles_ignores_other_users_and_hides_secrets(self):
        @contextmanager
        def connection():
            yield self.conn

        output = io.StringIO()
        with patch.object(auth, "get_connection", connection), \
             patch.object(auth.settings, "app_env", "test"), \
             patch.dict(os.environ, {"MINDCARE_TEST_PASSWORD": PASSWORD}), \
             redirect_stdout(output):
            auth.main(["--all-dev-profiles", "--dry-run"])
            self.assertEqual(self.snapshot("user_credentials"), [])
            auth.main(["--all-dev-profiles"])
        self.assertEqual(len(self.snapshot("user_credentials")), 2)
        self.assertIsNone(auth.verify_test_login(self.conn, "other@example.com", PASSWORD))
        self.assertNotIn(PASSWORD, output.getvalue())
        self.assertNotIn("$argon2", output.getvalue())
        self.assertIn("롤백", output.getvalue())
        self.assertIn("비밀번호 검증 성공", output.getvalue())


class SeedDevAuthCliTests(unittest.TestCase):
    def test_production_environment_is_rejected_before_prompt_or_db(self):
        with patch.object(auth.settings, "app_env", "production"), \
             patch.object(auth, "get_connection") as connection, \
             patch.object(auth, "getpass") as prompt, \
             patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                auth.main(["--all-dev-profiles"])
            connection.assert_not_called()
            prompt.assert_not_called()

    def test_password_confirmation_mismatch_does_not_connect(self):
        with patch.object(auth.settings, "app_env", "test"), \
             patch.dict(os.environ, {}, clear=True), \
             patch.object(auth, "getpass", side_effect=[PASSWORD, OTHER_PASSWORD]), \
             patch.object(auth, "get_connection") as connection:
            with self.assertRaisesRegex(SystemExit, "일치하지"):
                auth.main(["--email", EMAIL_A])
            connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
