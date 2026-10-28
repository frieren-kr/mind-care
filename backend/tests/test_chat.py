"""/chat 테스트. DB·실제 LLM 없이 가짜 실행기와 Mock 사용자 조회로 돌린다.

실행 (backend/ 에서):  python -m unittest tests.test_chat
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.ai.agent.outputs import StructuredAgentResult
from app.api import chat
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

    def post(self, body, execution=None, user_id=USER_ID):
        app.dependency_overrides[chat.get_chat_execution] = lambda: execution
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

    def test_no_sources_and_no_patient(self):
        res = self.post({"message": "안녕하세요"}, fake_execution([], self.calls))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["sources"], [])
        self.assertIsNone(self.calls[0][0].patient_id)

    def test_unknown_user_404(self):
        res = self.post({"message": "질문"}, fake_execution([], self.calls), user_id=uuid4())
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

    def test_missing_key_gives_no_execution(self):
        chat.get_chat_execution.cache_clear()
        self.addCleanup(chat.get_chat_execution.cache_clear)
        fake_settings = SimpleNamespace(openai_api_key="", openai_agent_model="m", openai_summary_model="m")
        with patch.object(chat, "settings", fake_settings), patch.object(chat, "OpenAI") as sdk:
            self.assertIsNone(chat.get_chat_execution())
        sdk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
