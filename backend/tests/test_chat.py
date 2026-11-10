"""/chat 테스트. DB·실제 LLM 없이 가짜 실행기와 Mock 사용자 조회로 돌린다.

로그인은 get_current_user만 바꿔 끼운다 (토큰·세션 검증은 test_auth_*). 본인 확인(require_user_access)은
실제 의존성 그대로 돌아서 403을 여기서 확인한다.

실행 (backend/ 에서):  python -m unittest tests.test_chat
"""
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.ai.agent.outputs import StructuredAgentResult
from app.ai.agent.registry import ToolName
from app.ai.agent.schemas import ModelTurn
from app.ai.agent.testing.chat_workflow_fakes import chat_call, chat_final
from app.ai.agent.testing.fake_llm import FakeLLMClient
from app.ai.agent.workflows.chat_execution import ChatExecutionV1
from app.api import chat
from app.api.chat_history import ChatStoreHistoryReader
from app.db.chat_store import ChatDailySummary, ChatTurn, ChatTurnRecord
from app.auth.dependencies import get_current_user
from app.auth_schemas import AuthUser
from app.feed_schemas import FeedReadCareLog, FeedReadCollection
from main import app

USER_ID = uuid4()
PATIENT_ID = uuid4()
PROFILE = SimpleNamespace(patients=[SimpleNamespace(patient=SimpleNamespace(id=PATIENT_ID))])

EVIDENCE = [
    {"evidence_type": "new_research", "title": "Sleep paper", "pmid": "111", "doi": None, "journal": "J1",
     "publication_year": 2025, "study_type": "rct", "organization": None, "source_url": None,
     "full_text_available": False},
    {"evidence_type": "guideline", "title": "Care guideline", "pmid": None, "doi": None, "journal": None,
     "publication_year": 2024, "study_type": None, "organization": "WHO", "source_url": "https://who.int/x",
     "full_text_available": True},
]


def fake_execution(refs, calls):
    """refs = 인용할 EVIDENCE 위치 목록. 받은 (context, 입력)을 calls에 남긴다."""
    def run(context, prepared):
        calls.append((context, prepared))
        citation_refs = [{"tool_call_id": "call_1", "evidence_index": i} for i in refs]
        return StructuredAgentResult.model_validate({
            "execution": {"status": "completed", "request_id": context.request_id, "final_answer": "답변입니다.",
                          "tool_rounds": 1, "total_tool_calls": 1, "called_tools": [], "errors": []},
            "output": {"schema_version": "1", "response_type": "chat", "answer": "답변입니다.",
                       "citation_refs": citation_refs},
            "sources": [{"source_ref": ref, **EVIDENCE[ref["evidence_index"]]} for ref in citation_refs],
        })
    return run


def failing_execution(context, prepared):
    raise RuntimeError("PRIVATE provider failure")


class ChatApiTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)
        for target, options in {
            "app.api.chat.user_functions.get_user_profile_context":
                {"side_effect": lambda user_id: PROFILE if user_id == USER_ID else None},
            "psycopg.connect": {"side_effect": AssertionError("Live DB forbidden")},
        }.items():
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(chat.chat_store, "save_chat_turn")
        self.save = patcher.start()
        self.addCleanup(patcher.stop)
        # 이전 대화 조회는 기본으로 빈 결과. 모델 ""이면 tiktoken 대신 UTF-8 상한으로 센다 (내려받기 없음).
        for name, target, value in (("history", chat.chat_store, "read_chat_history"),
                                    ("model", chat, "chat_model")):
            patcher = patch.object(target, value, return_value=([], []) if name == "history" else "")
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def post(self, body, execution=None, user_id=USER_ID, login_as=USER_ID):
        """login_as = 로그인한 사용자 (None이면 토큰 없이 보낸다)."""
        app.dependency_overrides[chat.get_chat_execution] = lambda: execution
        if login_as is not None:
            user = AuthUser(id=login_as, name="간병인", email="a@example.com", feed_refresh_days=3)
            app.dependency_overrides[get_current_user] = lambda: user
        return self.client.post(f"/chat/{user_id}", json=body)

    def test_answer_and_sources_in_citation_order(self):
        res = self.post({"message": "잠을 못 자요", "patient_id": str(PATIENT_ID)}, fake_execution([1, 0], self.calls))
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["answer"], "답변입니다.")
        self.assertEqual([s["title"] for s in body["sources"]], ["Care guideline", "Sleep paper"])
        self.assertEqual(body["sources"][0], EVIDENCE[1])  # source_ref는 내보내지 않는다
        context, prepared = self.calls[0]
        self.assertEqual((context.user_id, context.patient_id), (USER_ID, PATIENT_ID))
        self.assertIn("잠을 못 자요", prepared)
        self.save.assert_called_once_with(USER_ID, PATIENT_ID, "잠을 못 자요", "답변입니다.", [EVIDENCE[1], EVIDENCE[0]])

    def test_no_sources_and_no_patient(self):
        res = self.post({"message": "안녕하세요"}, fake_execution([], self.calls))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["sources"], [])
        self.assertIsNone(self.calls[0][0].patient_id)

    def test_save_failure_still_returns_answer(self):
        self.save.side_effect = RuntimeError("PRIVATE db failure")
        with self.assertLogs(chat.logger, "WARNING") as logs:
            res = self.post({"message": "PRIVATE 질문"}, fake_execution([0], self.calls))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["answer"], "답변입니다.")
        self.assertNotIn("PRIVATE", "\n".join(logs.output))  # 질문·오류 원문은 로그에 남기지 않는다

    def test_not_saved_when_no_answer(self):
        self.post({"message": "질문"}, None)                                    # 503
        self.post({"message": "질문"}, failing_execution)                       # 503
        self.post({"message": "질문", "patient_id": str(uuid4())}, fake_execution([], self.calls))  # 404
        self.post({"message": ""}, fake_execution([], self.calls))              # 422
        self.save.assert_not_called()

    def test_no_token_401(self):
        with patch("app.api.chat.user_functions.get_user_profile_context") as lookup:
            res = self.post({"message": "질문"}, fake_execution([], self.calls), login_as=None)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.headers.get("WWW-Authenticate"), "Bearer")
        lookup.assert_not_called()
        self.assertEqual(self.calls, [])
        self.save.assert_not_called()

    def test_other_users_path_403(self):
        other = uuid4()
        with patch("app.api.chat.user_functions.get_user_profile_context") as lookup:
            res = self.post({"message": "질문"}, fake_execution([], self.calls), user_id=other)
        self.assertEqual(res.status_code, 403)
        lookup.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_unknown_user_404(self):
        # 로그인은 됐지만 프로필이 없는 경우 (확인과 조회 사이에 삭제 등)
        missing = uuid4()
        res = self.post({"message": "질문"}, fake_execution([], self.calls), user_id=missing, login_as=missing)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self.calls, [])

    def test_other_users_patient_404(self):
        res = self.post({"message": "질문", "patient_id": str(uuid4())}, fake_execution([], self.calls))
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self.calls, [])

    def test_invalid_input_422(self):
        for body in ({"message": ""}, {"message": "   "}, {"message": "가" * 4001}, {},
                     {"message": "질문", "patient_id": "not-a-uuid"}, {"message": "​"}):
            with self.subTest(body=body):
                self.assertEqual(self.post(body, fake_execution([], self.calls)).status_code, 422)
        self.assertEqual(self.calls, [])

    def test_no_execution_503(self):
        res = self.post({"message": "질문"}, None)
        self.assertEqual(res.status_code, 503)
        self.assertIn("답변을 만들 수 없어요", res.json()["detail"])

    def test_execution_failure_503_without_leaking(self):
        with self.assertLogs(chat.logger, "WARNING") as logs:
            res = self.post({"message": "질문"}, failing_execution)
        self.assertEqual(res.status_code, 503)
        self.assertNotIn("PRIVATE", res.text + "\n".join(logs.output))

    def test_previous_chat_is_read_in_scope_and_passed_to_llm_input(self):
        now = datetime.now(timezone.utc)
        self.history.return_value = (
            [ChatTurn("어제 밤에 잠을 못 잤어요", "낮잠을 줄여 보세요.", now - timedelta(hours=2), now - timedelta(hours=1))],
            [ChatDailySummary(date(2026, 1, 2), "수면 상담")])
        res = self.post({"message": "오늘은요?", "patient_id": str(PATIENT_ID)}, fake_execution([], self.calls))
        self.assertEqual(res.status_code, 200)
        user_id, patient_id, reference, max_turns = self.history.call_args.args
        self.assertEqual((user_id, patient_id, max_turns), (USER_ID, PATIENT_ID, 5))
        self.assertIsNotNone(reference.tzinfo)
        prepared = json.loads(self.calls[0][1])
        self.assertEqual(prepared["user_message"], "오늘은요?")
        history = prepared["previous_chat_context"]
        self.assertEqual(history["recent_turns"][0]["messages"][0]["content"], "어제 밤에 잠을 못 잤어요")
        # 원문 턴과 다른 날짜의 요약이라 함께 들어간다 (같은 날짜면 Agent가 요약을 뺀다)
        self.assertEqual([s["summary"] for s in history["daily_summaries"]], ["수면 상담"])
        self.assertNotIn(str(USER_ID), self.calls[0][1])  # 서버 전용 id는 LLM 입력에 넣지 않는다

    def test_history_without_patient_reads_none_scope(self):
        self.post({"message": "안녕하세요"}, fake_execution([], self.calls))
        self.assertIsNone(self.history.call_args.args[1])

    def test_history_failure_still_answers_without_history(self):
        self.history.side_effect = RuntimeError("PRIVATE db failure")
        with self.assertLogs("app.ai.agent.workflows.chat_context_prepare", "WARNING") as logs:
            res = self.post({"message": "질문"}, fake_execution([], self.calls))
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("previous_chat_context", json.loads(self.calls[0][1]))
        self.assertNotIn("PRIVATE", "\n".join(logs.output))
        self.save.assert_called_once()

    def test_reader_builds_agent_snapshot(self):
        now = datetime.now(timezone.utc)
        self.history.return_value = ([ChatTurn("q", "a", now - timedelta(minutes=2), now - timedelta(minutes=1))],
                                     [ChatDailySummary(date(2026, 1, 2), "요약")])
        snap = ChatStoreHistoryReader().read_history(user_id=USER_ID, patient_id=None, reference_time=now,
                                                     max_recent_turns=5)
        self.history.assert_called_once_with(USER_ID, None, now, 5)
        self.assertEqual((snap.user_id, snap.patient_id), (USER_ID, None))
        self.assertEqual((snap.recent_turns[0].user_message, snap.recent_turns[0].assistant_message), ("q", "a"))
        self.assertEqual(snap.daily_summaries[0].summary, "요약")

    def real_execution(self, turns):
        """실제 ChatExecutionV1 + 운영 API의 DB 연결 구성. LLM만 가짜."""
        self.llm = FakeLLMClient(turns)
        return ChatExecutionV1(self.llm, chat.build_chat_tool_registry)

    def tool_names(self, request_index=0):
        return sorted(tool["name"] for tool in self.llm.requests[request_index][1])

    def test_registry_is_per_request_and_patient_tools_need_patient(self):
        execution = self.real_execution([chat_final("일반 답"), chat_final("환자 답")])
        self.assertEqual(self.post({"message": "안녕"}, execution).status_code, 200)
        self.assertEqual(self.tool_names(0), sorted(["search_evidence", "search_guidelines", "get_caregiver_profile"]))
        self.assertEqual(self.post({"message": "안녕", "patient_id": str(PATIENT_ID)}, execution).status_code, 200)
        self.assertEqual(self.tool_names(1), sorted(name.value for name in ToolName))

    def test_db_failure_and_unbound_visits_fail_alone_and_answer_still_returns(self):
        # 돌봄 조회 장애·미연결 예약 도구 실패를 정상 0건으로 바꾸지 않는다.
        # 그 Tool만 오류 결과가 되고, LLM은 다음 라운드에서 답을 만든다.
        patient = SimpleNamespace(id=PATIENT_ID, caregiver_id=USER_ID)
        execution = self.real_execution([
            ModelTurn(tool_calls=[chat_call(ToolName.RECENT_CARE_LOGS, identifier="logs_1"),
                                  chat_call(ToolName.PATIENT_MEDICAL_VISITS, identifier="visits_1",
                                            args={"status": "upcoming"})]),
            chat_final("기록을 확인하지 못했지만 일반적인 방법을 알려드릴게요."),
        ])
        with patch("app.db.user_functions.get_patient", return_value=patient), patch.object(
            chat.functions, "get_recent_care_logs", side_effect=RuntimeError("PRIVATE DB failure")
        ):
            res = self.post({"message": "최근 상태는요?", "patient_id": str(PATIENT_ID)}, execution)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["answer"], "기록을 확인하지 못했지만 일반적인 방법을 알려드릴게요.")
        outputs = [item for item in self.llm.requests[1][0] if item.type == "function_call_output"]
        self.assertEqual(sorted(o.call_id for o in outputs), ["logs_1", "visits_1"])
        for output in outputs:
            self.assertEqual(json.loads(output.content)["error"]["code"], "tool_exception")
        self.save.assert_called_once()

    def test_recent_care_logs_reach_llm_through_production_db_binding(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                execution = self.real_execution([
                    ModelTurn(tool_calls=[chat_call(ToolName.RECENT_CARE_LOGS, args={"days": 7, "limit": 2})]),
                    chat_final("등록한 돌봄 기록을 확인했어요."),
                ])
                def query(**kwargs):
                    self.assertEqual((kwargs["user_id"], kwargs["patient_id"], kwargs["limit"]),
                                     (USER_ID, PATIENT_ID, 2))
                    self.assertEqual(kwargs["end_at"] - kwargs["start_at"], timedelta(days=7))
                    items = [] if empty else [FeedReadCareLog(
                        user_id=USER_ID, patient_id=PATIENT_ID, log_type="patient_care",
                        logged_at=kwargs["start_at"], content="  산책 후 편안해하심\n", mood_tag=None)]
                    return FeedReadCollection[FeedReadCareLog](items=items, total_count=0 if empty else 3)
                with patch.object(chat.functions, "get_recent_care_logs", side_effect=query) as lookup:
                    response = self.post({"message": "최근 돌봄 기록 알려줘", "patient_id": str(PATIENT_ID)}, execution)
                self.assertEqual(response.status_code, 200, response.text)
                lookup.assert_called_once()
                output = next(item for item in self.llm.requests[1][0] if item.type == "function_call_output")
                payload = json.loads(output.content)
                self.assertTrue(payload["success"], payload)
                self.assertEqual(payload["data"]["total_count"], 0 if empty else 3)
                if empty:
                    self.assertEqual(payload["data"]["logs"], [])
                else:
                    self.assertEqual(payload["data"]["logs"][0]["content"], "  산책 후 편안해하심\n")

    def test_execution_uses_bound_registry_factory(self):
        chat.get_chat_execution.cache_clear()
        self.addCleanup(chat.get_chat_execution.cache_clear)
        self.model.return_value = "m"
        fake_settings = SimpleNamespace(openai_api_key="test-key", openai_agent_model="m", openai_summary_model="m")
        with patch.object(chat, "settings", fake_settings), patch.object(chat, "OpenAI"), \
             patch.object(chat, "chat_history_token_counter"), patch.object(chat, "OpenAIClient"), \
             patch.object(chat, "ChatExecutionV1") as execution:
            self.assertIs(chat.get_chat_execution(), execution.return_value)
        self.assertIs(execution.call_args.args[1], chat.build_chat_tool_registry)

    def test_missing_key_gives_no_execution(self):
        chat.get_chat_execution.cache_clear()
        self.addCleanup(chat.get_chat_execution.cache_clear)
        fake_settings = SimpleNamespace(openai_api_key="", openai_agent_model="m", openai_summary_model="m")
        with patch.object(chat, "settings", fake_settings), patch.object(chat, "OpenAI") as sdk:
            self.assertIsNone(chat.get_chat_execution())
        sdk.assert_not_called()



class ChatHistoryApiTests(unittest.TestCase):
    """GET /chat/{user_id}/history. DB 없이 chat_store.list_chat_turns·is_caregiver_of를 가짜로."""

    def setUp(self):
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)
        user = AuthUser(id=USER_ID, name="간병인", email="a@example.com", feed_refresh_days=3)
        app.dependency_overrides[get_current_user] = lambda: user
        now = datetime(2026, 11, 7, 3, tzinfo=timezone.utc)
        self.rows = [ChatTurnRecord(uuid4(), f"q{i}", f"a{i}", [EVIDENCE[1]] if i else [],
                                    now + timedelta(minutes=i), now + timedelta(minutes=i, seconds=5))
                     for i in range(2)]
        for name, target, kw in (
            ("turns", "app.api.chat.chat_store.list_chat_turns", {"return_value": (self.rows, True)}),
            ("owner", "app.api.chat.user_functions.is_caregiver_of",
             {"side_effect": lambda user_id, patient_id: (user_id, patient_id) == (USER_ID, PATIENT_ID)}),
            ("db", "psycopg.connect", {"side_effect": AssertionError("Live DB forbidden")}),
        ):
            patcher = patch(target, **kw)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def get(self, user_id=USER_ID, **params):
        return self.client.get(f"/chat/{user_id}/history", params=params)

    def test_turns_oldest_first_with_sources_and_has_more(self):
        res = self.get()
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["has_more"])
        self.assertEqual([t["question"] for t in body["turns"]], ["q0", "q1"])  # chat_store 순서 그대로
        self.assertEqual(body["turns"][1]["sources"], [EVIDENCE[1]])
        self.assertEqual(body["turns"][0]["sources"], [])
        self.assertEqual(set(body["turns"][0]), {"turn_id", "question", "answer", "sources", "asked_at", "answered_at"})
        self.assertEqual(body["turns"][0]["turn_id"], str(self.rows[0].turn_id))
        self.turns.assert_called_once_with(USER_ID, None, before=None, limit=20)
        self.owner.assert_not_called()  # 환자 미선택이면 환자 확인 쿼리도 없다

    def test_patient_scope_limit_and_before(self):
        before = "2026-11-07T12:00:00+09:00"
        res = self.get(patient_id=str(PATIENT_ID), limit=50, before=before)
        self.assertEqual(res.status_code, 200)
        self.turns.assert_called_once_with(USER_ID, PATIENT_ID, before=datetime.fromisoformat(before), limit=50)

    def test_other_users_or_unknown_patient_404(self):
        res = self.get(patient_id=str(uuid4()))
        self.assertEqual(res.status_code, 404)
        self.turns.assert_not_called()

    def test_no_token_401_and_other_user_403(self):
        self.assertEqual(self.get(user_id=uuid4()).status_code, 403)
        app.dependency_overrides.pop(get_current_user)
        res = self.get()
        self.assertEqual((res.status_code, res.headers.get("WWW-Authenticate")), (401, "Bearer"))
        self.turns.assert_not_called()

    def test_invalid_query_422(self):
        for params in ({"limit": 0}, {"limit": 51}, {"before": "2026-11-07T12:00:00"}, {"before": "어제"},
                       {"patient_id": "not-a-uuid"}):
            with self.subTest(params=params):
                self.assertEqual(self.get(**params).status_code, 422)
        self.turns.assert_not_called()

    def test_db_failure_503_without_leaking(self):
        self.turns.side_effect = RuntimeError("PRIVATE db failure")
        with self.assertLogs(chat.logger, "WARNING") as logs:
            res = self.get()
        self.assertEqual(res.status_code, 503)
        self.assertNotIn("PRIVATE", res.text + "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
