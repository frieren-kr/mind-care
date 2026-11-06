"""012 마이그레이션 + app/db/chat_store.py PostgreSQL 통합 테스트.

실행 (backend/ 에서):  python -m unittest tests.test_chat_store_db
로컬 DB(docker compose up -d)가 필요하다. 연결이 안 되면 건너뛴다.
격리 스키마에 001 모양의 chat_messages를 만들고 012를 적용한다. public 데이터는 건드리지 않고 전부 롤백한다.
"""
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from psycopg import sql
from psycopg.errors import CheckViolation, NotNullViolation

from app.db import chat_store
from app.db.chat_store import KST
from app.db.connection import get_connection

MIGRATION = (Path(__file__).parents[1] / "app/db/migrations/012_chat_history.sql").read_text(encoding="utf-8")
# 001_init.sql의 chat_messages 정의 그대로 (012가 이 위에 칸을 더한다)
CHAT_MESSAGES_001 = """
CREATE TABLE chat_messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    cited_paper_ids UUID[] DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_chat_messages_user_time ON chat_messages (user_id, created_at DESC);
"""
SOURCE = {"evidence_type": "guideline", "title": "지침", "pmid": None, "full_text_available": True}
DAY = date(2026, 11, 5)


class ChatStoreDbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            with get_connection():
                pass
        except Exception as exc:
            raise unittest.SkipTest(f"로컬 DB 없음 ({type(exc).__name__}) — backend/에서 docker compose up -d")

    def setUp(self):
        db = get_connection()
        self.conn = db.__enter__()
        self.addCleanup(db.__exit__, None, None, None)
        self.addCleanup(self.conn.rollback)
        schema = sql.Identifier(f"test_chat_012_{uuid4().hex}")
        self.conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        self.conn.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(schema))
        for table in ("users", "patient_profiles"):
            self.conn.execute(sql.SQL("CREATE TABLE {} (LIKE public.{} INCLUDING ALL)")
                              .format(sql.Identifier(table), sql.Identifier(table)))
        self.conn.execute(CHAT_MESSAGES_001)
        self.conn.execute(MIGRATION)
        self.conn.execute(MIGRATION)  # 두 번 돌려도 안전해야 한다
        self.user = self.conn.execute("INSERT INTO users (name, email) VALUES ('u', %s) RETURNING id",
                                      (f"chat-{uuid4().hex}@example.com",)).fetchone()[0]
        self.patient = self.conn.execute("INSERT INTO patient_profiles (caregiver_id) VALUES (%s) RETURNING id",
                                         (self.user,)).fetchone()[0]

        @contextmanager
        def connection():
            yield self.conn

        patcher = patch.object(chat_store, "get_connection", connection)
        patcher.start()
        self.addCleanup(patcher.stop)

    def insert(self, role, content, at, patient=None):
        self.conn.execute("INSERT INTO chat_messages (user_id, patient_id, role, content, created_at)"
                          " VALUES (%s, %s, %s, %s, %s)", (self.user, patient, role, content, at))

    def test_save_turn_and_recent_in_order(self):
        chat_store.save_chat_turn(self.user, self.patient, "질문1", "답1", [SOURCE])
        chat_store.save_chat_turn(self.user, self.patient, "질문2", "답2", [])
        chat_store.save_chat_turn(self.user, None, "환자 없는 질문", "답", [])
        recent = chat_store.get_recent_chat_messages(self.user, self.patient, limit=3)
        self.assertEqual([(m.role, m.content) for m in recent],
                         [("assistant", "답1"), ("user", "질문2"), ("assistant", "답2")])
        self.assertEqual(recent[0].sources, [SOURCE])
        self.assertEqual(recent[1].sources, [])
        self.assertEqual([m.content for m in chat_store.get_recent_chat_messages(self.user, None)],
                         ["환자 없는 질문", "답"])

    def test_save_turn_is_atomic(self):
        # 답변이 NULL이라 실패하면 질문도 저장되지 않는다 (savepoint로 이 부분만 되돌린다)
        with self.assertRaises(NotNullViolation), self.conn.transaction():
            chat_store.save_chat_turn(self.user, None, "질문", None, [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM chat_messages").fetchone(), (0,))

    def test_role_check(self):
        with self.assertRaises(CheckViolation):
            self.insert("system", "x", datetime.now(KST))

    def test_day_is_kst(self):
        start = datetime(2026, 11, 5, 0, 0, tzinfo=KST)
        self.insert("user", "전날 밤", start - timedelta(seconds=1))
        self.insert("user", "자정", start)
        self.insert("user", "환자 대화", start + timedelta(hours=1), self.patient)
        self.insert("user", "밤 11시", start + timedelta(hours=23, minutes=59))
        self.insert("user", "다음날", start + timedelta(days=1))
        self.assertEqual([m.content for m in chat_store.get_messages_for_day(self.user, None, DAY)], ["자정", "밤 11시"])
        self.assertEqual(sorted(chat_store.list_chat_threads_for_day(DAY), key=str),
                         sorted([(self.user, None), (self.user, self.patient)], key=str))

    def test_daily_summary_overwrites_even_without_patient(self):
        for patient in (None, self.patient):
            chat_store.save_daily_summary(self.user, patient, DAY, "처음", "m1")
            chat_store.save_daily_summary(self.user, patient, DAY, "다시", "m2")
        rows = self.conn.execute("SELECT patient_id, summary, model FROM chat_daily_summaries"
                                 " ORDER BY patient_id NULLS FIRST").fetchall()
        self.assertEqual(rows, [(None, "다시", "m2"), (self.patient, "다시", "m2")])

    def test_delete_old_keeps_recent_and_summaries(self):
        now = datetime.now(KST)
        self.insert("user", "8일 전", now - timedelta(days=8))
        self.insert("user", "6일 전", now - timedelta(days=6))
        chat_store.save_daily_summary(self.user, None, DAY, "요약", "m")
        self.assertEqual(chat_store.delete_old_chat_messages(7), 1)
        self.assertEqual(self.conn.execute("SELECT content FROM chat_messages").fetchall(), [("6일 전",)])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM chat_daily_summaries").fetchone(), (1,))

    def test_patient_delete_nulls_messages_and_removes_summaries(self):
        chat_store.save_chat_turn(self.user, self.patient, "q", "a", [])
        chat_store.save_daily_summary(self.user, self.patient, DAY, "요약", "m")
        chat_store.save_daily_summary(self.user, None, DAY, "환자 없음 요약", "m")
        self.conn.execute("DELETE FROM patient_profiles WHERE id = %s", (self.patient,))
        self.assertEqual(self.conn.execute("SELECT DISTINCT patient_id FROM chat_messages").fetchall(), [(None,)])
        self.assertEqual(self.conn.execute("SELECT summary FROM chat_daily_summaries").fetchall(), [("환자 없음 요약",)])

    def test_user_delete_cascades(self):
        chat_store.save_chat_turn(self.user, None, "q", "a", [])
        chat_store.save_daily_summary(self.user, None, DAY, "요약", "m")
        self.conn.execute("DELETE FROM users WHERE id = %s", (self.user,))
        self.assertEqual(self.conn.execute("SELECT (SELECT count(*) FROM chat_messages),"
                                           " (SELECT count(*) FROM chat_daily_summaries)").fetchone(), (0, 0))


if __name__ == "__main__":
    unittest.main()
