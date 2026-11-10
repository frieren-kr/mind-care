"""최근 돌봄 기록의 실제 PostgreSQL 권한·경계·스냅샷 및 챗봇 연결 검증.

실행: python -m unittest tests.test_recent_care_logs_db
테스트 전용 스키마에만 저장하고 종료 시 삭제한다. public 데이터는 변경하지 않는다.
스냅샷 검증은 별도 연결에서 커밋하므로 단일 연결/롤백 가짜로 대체하지 않는다.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch
from uuid import UUID, uuid4

from psycopg import Cursor, sql
from psycopg.errors import ReadOnlySqlTransaction

from app.api.chat import build_chat_tool_registry
from app.ai.agent.executor import ToolExecutor
from app.ai.agent.registry import ToolName
from app.ai.agent.schemas import AgentContext, ToolCall
from app.db import functions, user_functions
from app.db.connection import get_connection


START = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
END = START + timedelta(days=14)


class RecentCareLogsInputTests(TestCase):
    def test_invalid_inputs_fail_before_opening_connection(self):
        valid = dict(user_id=uuid4(), patient_id=uuid4(), start_at=START, end_at=END, limit=10)
        with patch.object(user_functions, "get_connection") as connection:
            for field in ("user_id", "patient_id"):
                for value in (None, "bad-uuid"):
                    with self.subTest(field=field, value=value), self.assertRaises(LookupError):
                        functions.get_recent_care_logs(**{**valid, field: value})
            for changes in (
                {"start_at": START.replace(tzinfo=None)}, {"end_at": "2026-10-15"},
                {"end_at": START}, {"start_at": END + timedelta(seconds=1)},
                *({"limit": value} for value in (0, 31, True, 1.0, "1", None)),
            ):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    functions.get_recent_care_logs(**{**valid, **changes})
            connection.assert_not_called()


class RecentCareLogsDbTests(TestCase):
    def setUp(self):
        connection = get_connection()
        self.writer = connection.__enter__()
        self.addCleanup(connection.__exit__, None, None, None)
        self.writer.autocommit = True
        self.schema = sql.Identifier(f"test_recent_care_logs_{uuid4().hex}")
        self.writer.execute(sql.SQL("CREATE SCHEMA {}").format(self.schema))
        self.addCleanup(self.drop_schema)
        self.search_path = sql.SQL("SET search_path TO {}, public").format(self.schema)
        self.writer.execute(self.search_path)
        for table in ("users", "patient_profiles", "care_logs"):
            self.writer.execute(sql.SQL("CREATE TABLE {} (LIKE public.{} INCLUDING ALL)")
                                .format(sql.Identifier(table), sql.Identifier(table)))
        self.user = self.writer.execute(
            "INSERT INTO users(name, email) VALUES ('test', 'care@example.com') RETURNING id"
        ).fetchone()[0]
        self.other = self.writer.execute(
            "INSERT INTO users(name, email) VALUES ('other', 'other@example.com') RETURNING id"
        ).fetchone()[0]
        self.patient = self.writer.execute(
            "INSERT INTO patient_profiles(caregiver_id) VALUES (%s) RETURNING id", (self.user,)
        ).fetchone()[0]
        self.other_patient = self.writer.execute(
            "INSERT INTO patient_profiles(caregiver_id) VALUES (%s) RETURNING id", (self.other,)
        ).fetchone()[0]
        self.on_count = None
        case = self

        class ObservingCursor(Cursor):
            def execute(self, query, params=None, **kwargs):
                result = super().execute(query, params, **kwargs)
                if isinstance(query, str) and query.startswith("SELECT count(*) AS total FROM care_logs "):
                    hook, case.on_count = case.on_count, None
                    if hook is not None:
                        hook(self.connection)
                return result

        @contextmanager
        def read_connection():
            with get_connection() as conn:
                conn.autocommit = True
                conn.execute(self.search_path)
                conn.autocommit = False
                conn.cursor_factory = ObservingCursor
                yield conn

        patcher = patch.object(user_functions, "get_connection", read_connection)
        patcher.start()
        self.addCleanup(patcher.stop)

    def drop_schema(self):
        self.writer.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(self.schema))

    def add_log(self, **changes):
        row = dict(id=uuid4(), user_id=self.user, patient_id=self.patient, log_type="patient_care",
                   content="  관찰 원문\n", mood_tag=None, logged_at=START)
        row.update(changes)
        self.writer.execute("""
            INSERT INTO care_logs(id, user_id, patient_id, log_type, content, mood_tag, logged_at)
            VALUES (%(id)s, %(user_id)s, %(patient_id)s, %(log_type)s, %(content)s, %(mood_tag)s, %(logged_at)s)
        """, row)

    def read(self, **changes):
        return functions.get_recent_care_logs(**{
            "user_id": self.user, "patient_id": self.patient,
            "start_at": START, "end_at": END, "limit": 10, **changes,
        })

    def test_filters_boundaries_stable_order_count_and_original_content(self):
        self.add_log(id=UUID(int=1), content=None)
        self.add_log(id=UUID(int=2), logged_at=END - timedelta(microseconds=1), content="  최근 원문\n")
        self.add_log(id=UUID(int=3), logged_at=END - timedelta(microseconds=1), content="동시각 큰 id")
        for changes in (
            {"logged_at": START - timedelta(microseconds=1)}, {"logged_at": END},
            {"patient_id": self.other_patient}, {"user_id": self.other},
            {"patient_id": None}, {"log_type": "caregiver_selfcare"},
        ):
            self.add_log(**changes)
        result = self.read(limit=2)
        self.assertEqual(result.total_count, 3)
        self.assertEqual([item.content for item in result.items], ["동시각 큰 id", "  최근 원문\n"])
        full = self.read(limit=30)
        self.assertEqual(len(full.items), 3)
        self.assertIsNone(full.items[-1].content)
        self.assertIsNone(full.items[-1].mood_tag)
        self.assertEqual(full.items[-1].logged_at, START)
        self.assertEqual(self.read(limit=1).total_count, 3)
        self.assertEqual(len(self.read(limit=1).items), 1)

    def test_empty_authorized_scope_succeeds_but_missing_or_foreign_patient_fails(self):
        self.assertEqual(self.read().model_dump(), {"items": [], "total_count": 0})
        for changes in ({"patient_id": self.other_patient}, {"patient_id": uuid4()},
                        {"user_id": self.other}, {"user_id": uuid4()}):
            with self.subTest(changes=changes), self.assertRaises(LookupError):
                self.read(**changes)

    def test_uuid_strings_and_different_timezone_offsets_read_same_instants(self):
        self.add_log()
        result = self.read(
            user_id=str(self.user), patient_id=str(self.patient),
            start_at=START.astimezone(timezone(timedelta(hours=9))),
            end_at=END.astimezone(timezone(timedelta(hours=-4))),
        )
        self.assertEqual(result, self.read())

    def test_authorization_count_and_rows_keep_one_snapshot_during_concurrent_commits(self):
        self.add_log(content="snapshot row")

        def concurrent_change(conn):
            self.assertEqual(conn.execute("SHOW transaction_isolation").fetchone()[0], "repeatable read")
            self.assertEqual(conn.execute("SHOW transaction_read_only").fetchone()[0], "on")
            self.add_log(logged_at=END - timedelta(seconds=1), content="new committed row")
            self.writer.execute("UPDATE care_logs SET content='changed' WHERE logged_at=%s", (START,))
            self.writer.execute("UPDATE patient_profiles SET caregiver_id=%s WHERE id=%s",
                                (self.other, self.patient))

        self.on_count = concurrent_change
        result = self.read()
        self.assertEqual(result.total_count, 1)
        self.assertEqual([item.content for item in result.items], ["snapshot row"])
        with self.assertRaises(LookupError):
            self.read()

    def test_database_rejects_writes_in_query_transaction(self):
        def try_write(conn):
            with self.assertRaises(ReadOnlySqlTransaction):
                with conn.transaction():
                    conn.execute("UPDATE care_logs SET content='forbidden'")

        self.on_count = try_write
        self.assertEqual(self.read().total_count, 0)

    def test_production_chat_registry_executes_real_database_query(self):
        self.add_log(logged_at=datetime.now(timezone.utc) - timedelta(hours=1))
        context = AgentContext(request_id="care-log-db-test", user_id=self.user, patient_id=self.patient)
        outcome, _ = ToolExecutor(build_chat_tool_registry(context)).execute(
            ToolCall(id="logs-1", name=ToolName.RECENT_CARE_LOGS.value, arguments={}), context)
        self.assertTrue(outcome.success, outcome)
        self.assertEqual(outcome.data["total_count"], 1)
        self.assertEqual(outcome.data["logs"][0]["content"], "  관찰 원문\n")
        self.assertEqual(set(outcome.data["logs"][0]), {"logged_at", "content", "mood_tag"})
