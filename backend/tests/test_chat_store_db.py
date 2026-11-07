"""012·013 마이그레이션 + app/db/chat_store.py PostgreSQL 통합 테스트.

실행 (backend/ 에서):  python -m unittest tests.test_chat_store_db
로컬 DB(docker compose up -d)가 필요하다. 연결이 안 되면 건너뛴다.
격리 스키마에 001 모양의 chat_messages를 만들고 012·013을 적용한다. public 데이터는 건드리지 않고 전부 롤백한다.
"""
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg.errors import CheckViolation, NotNullViolation, UniqueViolation

from app.db import chat_store
from app.db.chat_store import KST
from app.db.connection import get_connection

_MIGRATIONS = Path(__file__).parents[1] / "app/db/migrations"
MIGRATION = (_MIGRATIONS / "012_chat_history.sql").read_text(encoding="utf-8")
MIGRATION_013 = (_MIGRATIONS / "013_chat_turns.sql").read_text(encoding="utf-8")
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
        self.conn.execute(MIGRATION_013)
        self.conn.execute(MIGRATION_013)
        self.user = self.conn.execute("INSERT INTO users (name, email) VALUES ('u', %s) RETURNING id",
                                      (f"chat-{uuid4().hex}@example.com",)).fetchone()[0]
        self.patient = self.conn.execute("INSERT INTO patient_profiles (caregiver_id) VALUES (%s) RETURNING id",
                                         (self.user,)).fetchone()[0]

        self.connections = 0

        @contextmanager
        def connection():
            self.connections += 1
            yield self.conn

        patcher = patch.object(chat_store, "get_connection", connection)
        patcher.start()
        self.addCleanup(patcher.stop)

    def insert(self, role, content, at, patient=None, turn=None):
        self.conn.execute("INSERT INTO chat_messages (user_id, patient_id, role, content, created_at, turn_id)"
                          " VALUES (%s, %s, %s, %s, %s, %s)", (self.user, patient, role, content, at, turn))

    def turn(self, question, answer, asked, answered, patient=None, turn=None):
        turn = turn or uuid4()
        self.insert("user", question, asked, patient, turn)
        self.insert("assistant", answer, answered, patient, turn)
        return turn

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

    def test_patient_delete_removes_messages_and_summaries(self):
        # 013: SET NULL → CASCADE. 삭제된 환자 대화가 "환자 미선택" 대화로 섞이지 않는다.
        chat_store.save_chat_turn(self.user, self.patient, "q", "a", [])
        chat_store.save_chat_turn(self.user, None, "환자 없는 q", "a", [])
        chat_store.save_daily_summary(self.user, self.patient, DAY, "요약", "m")
        chat_store.save_daily_summary(self.user, None, DAY, "환자 없음 요약", "m")
        self.conn.execute("DELETE FROM patient_profiles WHERE id = %s", (self.patient,))
        self.assertEqual(self.conn.execute("SELECT content, patient_id FROM chat_messages WHERE role = 'user'")
                         .fetchall(), [("환자 없는 q", None)])
        self.assertEqual(self.conn.execute("SELECT summary FROM chat_daily_summaries").fetchall(), [("환자 없음 요약",)])

    def test_013_constraint_and_turn_unique(self):
        self.assertEqual(self.conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
            " WHERE conname = 'chat_messages_patient_id_fkey' AND conrelid = 'chat_messages'::regclass").fetchall(),
            [("FOREIGN KEY (patient_id) REFERENCES patient_profiles(id) ON DELETE CASCADE",)])
        now = datetime.now(KST)
        self.insert("user", "옛 행1", now)  # turn_id NULL끼리는 겹쳐도 된다
        self.insert("user", "옛 행2", now)
        turn = self.turn("q", "a", now, now)
        with self.assertRaises(UniqueViolation), self.conn.transaction():
            self.insert("user", "같은 턴 두 번째 질문", now, turn=turn)

    def test_save_turn_shares_turn_id(self):
        chat_store.save_chat_turn(self.user, None, "q", "a", [])
        rows = self.conn.execute("SELECT role, turn_id, created_at FROM chat_messages ORDER BY role DESC").fetchall()
        self.assertEqual([r[0] for r in rows], ["user", "assistant"])
        self.assertIsNotNone(rows[0][1])
        self.assertEqual(rows[0][1], rows[1][1])
        self.assertLessEqual(rows[0][2], rows[1][2])  # 질문 시각 <= 답변 시각

    def test_recent_turns_pairs_scope_reference_and_order(self):
        now = datetime.now(KST)

        def ago(minutes):
            return now - timedelta(minutes=minutes)

        self.turn("오래된 q", "오래된 a", ago(60), ago(59))
        tie_a = self.turn("동시1 q", "동시1 a", ago(31), ago(30))
        tie_b = self.turn("동시2 q", "동시2 a", ago(31), ago(30))
        self.turn("최신 q", "최신 a", ago(10), ago(9))
        self.turn("미래 q", "미래 a", ago(1), now + timedelta(minutes=1))  # 기준 시각 이후 완료
        self.turn("환자 q", "환자 a", ago(5), ago(4), patient=self.patient)
        self.insert("user", "옛 q", ago(3))  # 012 시절 turn_id 없는 행
        self.insert("assistant", "옛 a", ago(3))
        self.insert("user", "답 없는 q", ago(2), turn=uuid4())

        turns = chat_store.get_recent_chat_turns(self.user, None, now, 5)
        ties = [q for q, _ in sorted([("동시1 q", tie_a), ("동시2 q", tie_b)], key=lambda x: x[1])]
        self.assertEqual([t.question for t in turns], ["최신 q", *ties, "오래된 q"])
        self.assertEqual((turns[0].answer, turns[0].started_at, turns[0].completed_at), ("최신 a", ago(10), ago(9)))
        self.assertEqual([t.question for t in chat_store.get_recent_chat_turns(self.user, None, now, 2)],
                         ["최신 q", ties[0]])
        self.assertEqual([t.question for t in chat_store.get_recent_chat_turns(self.user, self.patient, now)],
                         ["환자 q"])
        self.assertEqual(chat_store.get_recent_chat_turns(self.user, self.patient, ago(5)), [])
        with self.assertRaises(ValueError):
            chat_store.get_recent_chat_turns(self.user, None, datetime(2026, 11, 7), 5)

    def test_list_chat_turns_for_screen(self):
        now = datetime.now(KST)

        def ago(minutes):
            return now - timedelta(minutes=minutes)

        turns = [self.turn(f"q{i}", f"a{i}", ago(50 - i * 10), ago(49 - i * 10)) for i in range(4)]  # q0이 가장 오래됨
        self.conn.execute("UPDATE chat_messages SET sources = %s WHERE turn_id = %s AND role = 'assistant'",
                          (Jsonb([SOURCE]), turns[3]))
        self.turn("환자 q", "환자 a", ago(5), ago(4), patient=self.patient)
        self.insert("user", "옛 q", ago(3))  # turn_id 없음
        self.insert("assistant", "옛 a", ago(3))
        self.insert("user", "답 없는 q", ago(2), turn=uuid4())

        page, more = chat_store.list_chat_turns(self.user, None, limit=2)
        self.assertEqual(([t.question for t in page], more), (["q2", "q3"], True))  # 최신 2개, 오래된 것부터
        self.assertEqual((page[1].turn_id, page[1].answer, page[1].sources), (turns[3], "a3", [SOURCE]))
        self.assertEqual((page[0].asked_at, page[0].answered_at), (ago(30), ago(29)))
        self.assertEqual(page[0].sources, [])
        page, more = chat_store.list_chat_turns(self.user, None, before=page[0].answered_at, limit=2)
        self.assertEqual(([t.question for t in page], more), (["q0", "q1"], False))
        page, more = chat_store.list_chat_turns(self.user, None, limit=4)
        self.assertEqual((len(page), more), (4, False))  # 딱 맞으면 has_more=false
        self.assertEqual([t.question for t in chat_store.list_chat_turns(self.user, self.patient)[0]], ["환자 q"])
        self.assertEqual(chat_store.list_chat_turns(uuid4(), None), ([], False))
        with self.assertRaises(ValueError):
            chat_store.list_chat_turns(self.user, None, before=datetime(2026, 11, 7))

    def test_daily_summaries_before_and_scope(self):
        for day, text in ((date(2026, 11, 4), "4일"), (DAY, "5일"), (date(2026, 11, 6), "6일")):
            chat_store.save_daily_summary(self.user, None, day, text, "m")
        chat_store.save_daily_summary(self.user, self.patient, DAY, "환자 5일", "m")
        result = chat_store.get_daily_summaries_before(self.user, None, date(2026, 11, 6))
        self.assertEqual([(s.summary_date, s.summary) for s in result],
                         [(DAY, "5일"), (date(2026, 11, 4), "4일")])
        self.assertEqual([s.summary for s in chat_store.get_daily_summaries_before(
            self.user, None, date(2026, 11, 7), 1)], ["6일"])
        self.assertEqual([s.summary for s in chat_store.get_daily_summaries_before(
            self.user, self.patient, date(2026, 11, 7))], ["환자 5일"])

    def test_read_chat_history_one_connection_and_kst_today(self):
        reference = datetime(2026, 11, 6, 0, 30, tzinfo=KST)  # UTC로는 11/5지만 한국 날짜는 11/6
        self.turn("q", "a", reference - timedelta(minutes=2), reference - timedelta(minutes=1))
        chat_store.save_daily_summary(self.user, None, DAY, "어제", "m")
        chat_store.save_daily_summary(self.user, None, date(2026, 11, 6), "오늘", "m")
        self.connections = 0
        turns, summaries = chat_store.read_chat_history(self.user, None, reference.astimezone(timezone.utc), 5)
        self.assertEqual(self.connections, 1)
        self.assertEqual([t.question for t in turns], ["q"])
        self.assertEqual([s.summary for s in summaries], ["어제"])

    def test_user_delete_cascades(self):
        chat_store.save_chat_turn(self.user, None, "q", "a", [])
        chat_store.save_daily_summary(self.user, None, DAY, "요약", "m")
        self.conn.execute("DELETE FROM users WHERE id = %s", (self.user,))
        self.assertEqual(self.conn.execute("SELECT (SELECT count(*) FROM chat_messages),"
                                           " (SELECT count(*) FROM chat_daily_summaries)").fetchone(), (0, 0))


if __name__ == "__main__":
    unittest.main()
