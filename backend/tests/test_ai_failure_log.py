"""AI 실행 실패 로그 (app/api/ai_failure_log.py). DB·LLM 없이 돈다.

실행 (backend/ 에서):  python -m unittest tests.test_ai_failure_log
API에 붙인 동작은 tests.test_chat / tests.test_recommendations의 provider_error 테스트가 확인한다.
"""
from types import SimpleNamespace
import unittest

import httpx
import openai

from app.ai.agent.outputs import StructuredAgentResult
from app.api.ai_failure_log import (ErrorRecordingClient, clean_message, describe_provider_error,
                                    logged_execution)

LOGGER = "app.api.ai_failure_log"
CONTEXT = SimpleNamespace(request_id="req-1")


def result(status, errors=(), tools=()):
    return StructuredAgentResult.model_validate({
        "execution": {"status": status, "request_id": "req-1", "final_answer": None, "tool_rounds": 1,
                      "total_tool_calls": len(tools), "errors": [{"code": c, "message": "PRIVATE 검색어"} for c in errors],
                      "called_tools": [{"tool_name": n, "success": e is None, "duration_ms": 1.0, "error_type": e}
                                       for n, e in tools]},
        "output": None, "sources": [],
    })


class CleanMessageTests(unittest.TestCase):
    def test_masks_keys_joins_lines_and_truncates(self):
        self.assertEqual(clean_message("bad key: sk-proj-AB_c-12.\nretry"), "bad key: sk-*** retry")
        self.assertEqual(len(clean_message("가" * 1000)), 300)

    def test_describes_openai_status_code_and_nested_body_code(self):
        response = httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
        exc = openai.AuthenticationError("Key sk-live-SECRET expired", response=response,
                                         body={"error": {"code": "expired_secret_key"}})
        text = describe_provider_error(exc)
        self.assertEqual(text, "AuthenticationError status=401 code=expired_secret_key message=Key sk-*** expired")

    def test_plain_exception_has_type_and_message_only(self):
        self.assertEqual(describe_provider_error(TimeoutError("late")), "TimeoutError message=late")


class LoggedExecutionTests(unittest.TestCase):
    def test_failed_result_logs_status_codes_and_tool_errors_but_not_messages(self):
        failed = result("failed", errors=["tool_error", "invalid_output"],
                        tools=[("search_evidence", None), ("get_recent_care_logs", "unavailable")])
        with self.assertLogs(LOGGER, "WARNING") as logs:
            self.assertIs(logged_execution("챗봇", lambda ctx, text: failed)(CONTEXT, "PRIVATE 질문"), failed)
        text = "\n".join(logs.output)
        self.assertIn("request_id=req-1, status=failed errors=['tool_error', 'invalid_output']"
                      " tools=['get_recent_care_logs:unavailable'], provider=[]", text)
        self.assertNotIn("PRIVATE", text)

    def test_provider_exception_recorded_by_wrapped_client(self):
        class Failing:
            model = "m"

            def generate(self, request):
                raise RuntimeError("upstream sk-abc down")

        client = ErrorRecordingClient(Failing())
        self.assertEqual(client.model, "m")  # 다른 속성은 그대로 넘긴다

        def execution(ctx, text):
            try:
                client.generate(None)
            except RuntimeError:
                return result("provider_error", errors=["provider_error"])

        with self.assertLogs(LOGGER, "WARNING") as logs:
            logged_execution("추천", execution)(CONTEXT, "x")
        self.assertIn("provider=['RuntimeError message=upstream sk-*** down']", logs.output[0])

    def test_raised_exception_is_logged_by_type_and_reraised(self):
        def boom(ctx, text):
            raise ValueError("PRIVATE")
        with self.assertLogs(LOGGER, "WARNING") as logs, self.assertRaises(ValueError):
            logged_execution("챗봇", boom)(CONTEXT, "x")
        self.assertIn("챗봇 AI 실행 예외 (request_id=req-1, ValueError", logs.output[0])
        self.assertNotIn("PRIVATE", logs.output[0])

    def test_completed_result_and_unwrapped_calls_log_nothing(self):
        ok = SimpleNamespace(execution=SimpleNamespace(status="completed"))
        with self.assertNoLogs(LOGGER, "WARNING"):
            logged_execution("챗봇", lambda ctx, text: ok)(CONTEXT, "x")
            with self.assertRaises(RuntimeError):  # 실행 밖 호출은 기록할 곳이 없어도 그대로 던진다
                ErrorRecordingClient(SimpleNamespace(generate=lambda r: (_ for _ in ()).throw(RuntimeError())))\
                    .generate(None)


if __name__ == "__main__":
    unittest.main()
