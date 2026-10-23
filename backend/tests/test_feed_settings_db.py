"""010 마이그레이션·설정 API의 PostgreSQL 통합 테스트.

실행: python -m unittest tests.test_feed_settings_db
기존 DB 연결에 격리된 스키마를 만들고 테이블·트리거 함수를 함께 검증한다.
모든 테스트 변경은 rollback하며 기존 사용자 설정과 public 스키마는 변경하지 않는다.
"""
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.errors import CheckViolation, NotNullViolation

from app.api.users import router
from app.api.recommendations import get_feed_cache_days
from app.db import user_functions
from app.db.connection import get_connection


class MigrationDbCase(unittest.TestCase):
    def setUp(self):
        db = get_connection()
        self.conn = db.__enter__()
        self.addCleanup(db.__exit__, None, None, None)
        self.addCleanup(self.conn.rollback)
        with self.conn.cursor() as cur:
            schema = sql.Identifier(f"test_migration_010_{uuid4().hex}")
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
            cur.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(schema))
            for table in ("users", "patient_profiles", "caregiver_profiles", "medical_visits", "medications"):
                cur.execute(sql.SQL("CREATE TABLE {} (LIKE public.{} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)")
                            .format(sql.Identifier(table), sql.Identifier(table)))
            cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS feed_refresh_days")
            cur.execute("INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id",
                        ("기존 테스트 사용자", f"feed-settings-{uuid4().hex}@example.com"))
            self.user_id = cur.fetchone()[0]
            migration = Path(__file__).parents[1] / "app/db/migrations/010_user_feed_refresh_days.sql"
            self.migration_sql = migration.read_text()
            cur.execute(self.migration_sql)

        @contextmanager
        def connection():
            yield self.conn

        patcher = patch.object(user_functions, "get_connection", connection)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.url = f"/users/{self.user_id}/feed-settings"


class FeedSettingsDbTests(MigrationDbCase):
    def test_migration_defaults_for_existing_and_new_users(self):
        self.assertEqual(user_functions.get_user(self.user_id).feed_refresh_days, 3)
        new = user_functions.create_user({"name": "새 테스트 사용자", "email": f"new-{uuid4().hex}@example.com"})
        self.assertEqual(new.feed_refresh_days, 3)
        self.assertEqual(get_feed_cache_days(new.id), 3)

    def test_http_update_persists_and_all_reads_use_saved_value(self):
        before = user_functions.get_user(self.user_id)
        for days in (1, 5, 7):
            response = self.client.patch(self.url, json={"feed_refresh_days": days})
            self.assertEqual(response.status_code, 200, response.text)
            for url in (self.url, f"/feed/{self.user_id}", f"/feed_user/{self.user_id}"):
                read = self.client.get(url)
                self.assertEqual(read.status_code, 200, read.text)
                self.assertEqual(read.json()["feed_refresh_days"], days)
            self.assertEqual(get_feed_cache_days(self.user_id), days)
            self.assertEqual(user_functions.list_users()[0].feed_refresh_days, days)
        after = user_functions.get_user(self.user_id)
        self.assertEqual((after.name, after.email, after.created_at),
                         (before.name, before.email, before.created_at))

    def test_database_constraints_reject_invalid_days(self):
        for value, exception in ((0, CheckViolation), (8, CheckViolation), (None, NotNullViolation)):
            with self.subTest(value=value), self.assertRaises(exception):
                with self.conn.transaction():
                    self.conn.execute("UPDATE users SET feed_refresh_days = %s WHERE id = %s",
                                      (value, self.user_id))
        self.assertEqual(user_functions.get_user_feed_settings(self.user_id).feed_refresh_days, 3)

    def test_missing_user_and_invalid_request_do_not_change_settings(self):
        missing = f"/users/{uuid4()}/feed-settings"
        self.assertEqual(self.client.get(missing).status_code, 404)
        self.assertEqual(self.client.patch(missing, json={"feed_refresh_days": 5}).status_code, 404)
        self.assertEqual(self.client.patch(self.url, json={"feed_refresh_days": 8}).status_code, 422)
        self.assertEqual(self.client.get(self.url).json()["feed_refresh_days"], 3)


class ClinicalDateConstraintDbTests(MigrationDbCase):
    def assert_rejected(self, statement, params, constraint):
        with self.assertRaises(CheckViolation) as caught:
            with self.conn.transaction():
                self.conn.execute(statement, params)
        self.assertEqual(caught.exception.diag.constraint_name, constraint)

    def test_direct_sql_insert_rejects_both_contradictions(self):
        self.assert_rejected(
            "INSERT INTO medical_visits(patient_id, visit_date, is_visited) "
            "VALUES (%s, (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date + 1, true)",
            (uuid4(),), "medical_visits_completed_date_check")
        self.assert_rejected(
            "INSERT INTO medications(patient_id, drug_name, is_taking, end_date) "
            "VALUES (%s, 'test', false, (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date + 1)",
            (uuid4(),), "medications_stopped_end_date_check")
        for table in ("medical_visits", "medications"):
            self.assertEqual(self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_partial_visit_updates_check_final_row(self):
        future = self.conn.execute(
            "INSERT INTO medical_visits(patient_id, visit_date, is_visited) "
            "VALUES (%s, (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date + 1, false) RETURNING id",
            (uuid4(),)).fetchone()[0]
        done = self.conn.execute(
            "INSERT INTO medical_visits(patient_id, visit_date, is_visited) "
            "VALUES (%s, (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date, true) RETURNING id",
            (uuid4(),)).fetchone()[0]
        self.assert_rejected("UPDATE medical_visits SET is_visited=true WHERE id=%s", (future,),
                             "medical_visits_completed_date_check")
        self.assert_rejected("UPDATE medical_visits SET visit_date=visit_date+1 WHERE id=%s", (done,),
                             "medical_visits_completed_date_check")
        self.assertFalse(self.conn.execute("SELECT is_visited FROM medical_visits WHERE id=%s", (future,)).fetchone()[0])

    def test_partial_medication_updates_check_final_row(self):
        future = self.conn.execute(
            "INSERT INTO medications(patient_id, drug_name, is_taking, end_date) "
            "VALUES (%s, 'test', true, (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date + 1) RETURNING id",
            (uuid4(),)).fetchone()[0]
        stopped = self.conn.execute(
            "INSERT INTO medications(patient_id, drug_name, is_taking, end_date) "
            "VALUES (%s, 'test', false, (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date) RETURNING id",
            (uuid4(),)).fetchone()[0]
        self.assert_rejected("UPDATE medications SET is_taking=false WHERE id=%s", (future,),
                             "medications_stopped_end_date_check")
        self.assert_rejected("UPDATE medications SET end_date=end_date+1 WHERE id=%s", (stopped,),
                             "medications_stopped_end_date_check")
        self.assertTrue(self.conn.execute("SELECT is_taking FROM medications WHERE id=%s", (future,)).fetchone()[0])

    def test_today_past_future_plans_and_unknown_end_are_allowed(self):
        for days, visited in ((-1, True), (0, True), (1, False)):
            self.conn.execute("INSERT INTO medical_visits(patient_id, visit_date, is_visited) "
                              "VALUES (%s, (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date + %s, %s)",
                              (uuid4(), days, visited))
        for days, taking in ((-1, False), (0, False), (1, True), (None, False)):
            self.conn.execute("INSERT INTO medications(patient_id, drug_name, is_taking, end_date) "
                              "VALUES (%s, 'test', %s, (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date + %s::integer)",
                              (uuid4(), taking, days))

    def test_session_timezone_does_not_change_korean_today(self):
        for timezone in ("Pacific/Honolulu", "Pacific/Kiritimati"):
            self.conn.execute("SELECT set_config('TimeZone', %s, true)", (timezone,))
            self.conn.execute("INSERT INTO medical_visits(patient_id, visit_date, is_visited) "
                              "VALUES (%s, (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date, true)", (uuid4(),))
            self.assert_rejected(
                "INSERT INTO medical_visits(patient_id, visit_date, is_visited) "
                "VALUES (%s, (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date + 1, true)",
                (uuid4(),), "medical_visits_completed_date_check")
            self.conn.execute("INSERT INTO medications(patient_id, drug_name, is_taking, end_date) "
                              "VALUES (%s, 'test', false, (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date)",
                              (uuid4(),))
            self.assert_rejected(
                "INSERT INTO medications(patient_id, drug_name, is_taking, end_date) "
                "VALUES (%s, 'test', false, (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date + 1)",
                (uuid4(),), "medications_stopped_end_date_check")

    def test_rerun_preserves_settings_and_does_not_duplicate_triggers(self):
        user_functions.update_user_feed_settings(self.user_id, {"feed_refresh_days": 7})
        self.conn.execute(self.migration_sql)
        self.assertEqual(get_feed_cache_days(self.user_id), 7)
        for table in ("medical_visits", "medications"):
            count = self.conn.execute("SELECT count(*) FROM pg_trigger WHERE tgrelid=%s::regclass AND NOT tgisinternal",
                                      (table,)).fetchone()[0]
            self.assertEqual(count, 1)

    def test_existing_contradictions_abort_migration_without_rewriting_data(self):
        for table, trigger, constraint, insert in (
            ("medical_visits", "medical_visits_date_state_guard", "medical_visits_completed_date_check",
             "INSERT INTO medical_visits(patient_id, visit_date, is_visited) "
             "VALUES (%s, (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date + 1, true)"),
            ("medications", "medications_date_state_guard", "medications_stopped_end_date_check",
             "INSERT INTO medications(patient_id, drug_name, is_taking, end_date) "
             "VALUES (%s, 'test', false, (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date + 1)"),
        ):
            with self.subTest(table=table), self.conn.transaction(force_rollback=True):
                self.conn.execute(sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}")
                                  .format(sql.Identifier(table), sql.Identifier(trigger)))
                self.conn.execute(insert, (uuid4(),))
                self.assert_rejected(self.migration_sql, None, constraint)
                self.assertEqual(self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
