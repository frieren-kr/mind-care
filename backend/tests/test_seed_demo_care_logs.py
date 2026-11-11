"""시연용 돌봄 기록 스크립트 테스트.

실행: python -m unittest tests.test_seed_demo_care_logs -v
데이터·날짜·실행 조건·dry-run은 DB 없이, 재실행 삭제 범위는 격리 스키마(롤백)에서 확인한다.
"""
from contextlib import redirect_stdout
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from psycopg import sql

from app.db.connection import get_connection
from app.user_schemas import APP_TIMEZONE
from scripts import seed_demo_care_logs as seed

PROFILES = Path(__file__).resolve().parents[1] / "app" / "db" / "seed" / "dev_profiles.json"
NOW = datetime(2026, 11, 11, 0, 30, tzinfo=APP_TIMEZONE)  # 자정 직후도 14일 창 안이어야 한다


class SeedDataTests(unittest.TestCase):
    def test_logs_match_dev_profiles_and_rules(self):
        profiles = json.loads(PROFILES.read_text(encoding="utf-8"))["profiles"]
        patients = {(p["user"]["email"], pt["dementia_stage"]) for p in profiles for pt in p["patients"]}
        self.assertEqual(set(seed.SEED_LOGS), {f"dev-seed-{i}@example.com" for i in range(1, 5)})
        for email, by_patient in seed.SEED_LOGS.items():
            for stage, logs in by_patient.items():
                self.assertIn((email, stage), patients)
                self.assertTrue(8 <= len(logs) <= 12, (email, stage, len(logs)))
                for days_ago, hour, mood_tag, content in logs:
                    self.assertTrue(1 <= days_ago <= 13 and 0 <= hour <= 23)
                    self.assertTrue(mood_tag and content)
                    self.assertIsNone(re.search(r"\d{2,3}-\d{3,4}-\d{4}|@", content))  # 연락처 없음

    def test_logged_at_is_inside_recent_14_days_and_marked(self):
        for days_ago, hour in ((1, 23), (13, 0), (13, 23)):
            at = seed.logged_at(NOW, days_ago, hour)
            self.assertTrue(NOW - timedelta(days=14) <= at < NOW, at)
            self.assertEqual((at.second, at.microsecond), (0, seed.MARK_MICROSECONDS))


class SeedMainTests(unittest.TestCase):
    def test_refuses_outside_development_and_test(self):
        with patch.object(seed.settings, "app_env", "production"), \
             patch.object(seed, "get_connection") as conn, redirect_stdout(io.StringIO()), \
             patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            seed.main([])
        conn.assert_not_called()

    def test_dry_run_counts_without_writing_or_printing_content(self):
        users = {email: uuid4() for email in seed.SEED_LOGS}
        patient = lambda stage: SimpleNamespace(id=uuid4(), dementia_stage=stage)
        out = io.StringIO()
        with patch.object(seed.settings, "app_env", "development"), \
             patch.object(seed, "get_connection", MagicMock()), \
             patch.object(seed, "seed_user_ids", return_value=users), \
             patch.object(seed.uf, "list_patients",
                          side_effect=lambda uid: [patient(n) for e, ps in seed.SEED_LOGS.items()
                                                   if users[e] == uid for n in ps]), \
             patch.object(seed, "delete_seeded") as delete, \
             patch.object(seed, "save_care_log") as save, redirect_stdout(out):
            seed.main(["--dry-run"])
        delete.assert_not_called()
        save.assert_not_called()
        total = sum(len(logs) for ps in seed.SEED_LOGS.values() for logs in ps.values())
        self.assertIn(f"총 {total}건", out.getvalue())
        for ps in seed.SEED_LOGS.values():
            for logs in ps.values():
                for *_, content in logs:
                    self.assertNotIn(content, out.getvalue())


class DeleteSeededDbTests(unittest.TestCase):
    def test_deletes_only_marked_patient_care_of_given_users(self):
        with get_connection() as conn:  # 실패하면 get_connection이 롤백, 성공해도 끝에서 롤백
            schema = sql.Identifier(f"test_demo_logs_{uuid4().hex}")
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
            conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(schema))
            conn.execute("CREATE TABLE care_logs (LIKE public.care_logs INCLUDING DEFAULTS)")
            seed_user, other_user = uuid4(), uuid4()
            marked = seed.logged_at(NOW, 1, 9)
            rows = [
                (seed_user, "patient_care", marked),                         # 지움
                (seed_user, "patient_care", marked.replace(microsecond=1)),  # 다른 기록
                (seed_user, "caregiver_selfcare", marked),                   # 다른 종류
                (other_user, "patient_care", marked),                        # 다른 사용자
            ]
            for row in rows:
                conn.execute("INSERT INTO care_logs(user_id, log_type, logged_at) VALUES (%s,%s,%s)", row)
            self.assertEqual(seed.delete_seeded(conn, [seed_user]), 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM care_logs").fetchone()[0], 3)
            self.assertEqual(seed.delete_seeded(conn, []), 0)
            conn.rollback()


if __name__ == "__main__":
    unittest.main()
