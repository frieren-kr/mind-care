"""설치된 SDK + MockTransport로 실제 wire format을 검증한다. 네트워크 없음."""
import json
import unittest
from threading import Event
from unittest.mock import patch

import httpx
from openai import OpenAI

from ..clients.openai_client import OpenAIClient
from ..agent_loop_runner import AgentLoopRunner
from ..registry import ToolName, production_registry
from ..testing.fake_tools import fake_registry
from .test_workflows import CONTEXT


def completion(message, finish_reason="stop"):
    return {"id": "test-completion", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}]}


class OpenAIClientTests(unittest.TestCase):
    def sdk(self, handler):
        http = httpx.Client(transport=httpx.MockTransport(handler))
        sdk = OpenAI(api_key="test-placeholder", http_client=http, max_retries=0)
        self.addCleanup(sdk.close)
        return OpenAIClient(sdk, model="configured-test-model")

    def test_explicit_network_timeout_and_disabled_sdk_retries(self):
        requests = []
        def transport(request):
            requests.append(request)
            return httpx.Response(500, json={"error": {"message": "synthetic", "type": "server_error"}})
        sdk = OpenAI(api_key="test-placeholder", http_client=httpx.Client(transport=httpx.MockTransport(transport)),
                     timeout=90, max_retries=2)
        self.addCleanup(sdk.close)
        client = OpenAIClient(sdk, model="configured-test-model", timeout_seconds=0.25)
        with patch.object(OpenAI, "_calculate_retry_timeout", return_value=0):
            result = AgentLoopRunner(client, production_registry()).run("synthetic", CONTEXT)
        self.assertEqual(result.status, "provider_error")
        self.assertEqual(len(requests), 1)
        self.assertEqual(set(requests[0].extensions["timeout"].values()), {0.25})
        self.assertEqual(sdk.max_retries, 2)  # injected SDK configuration is not mutated
        self.assertEqual(sdk.timeout, 90)

    def test_network_timeout_is_capped_to_remaining_agent_deadline(self):
        requests = []
        def transport(request):
            requests.append(request)
            return httpx.Response(200, json=completion({"role": "assistant", "content": "done"}))
        result = AgentLoopRunner(self.sdk(transport), production_registry(), request_timeout_seconds=1).run("synthetic", CONTEXT)
        self.assertEqual(result.status, "completed")
        for timeout in requests[0].extensions["timeout"].values():
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 1)

    def test_sdk_does_not_retry_after_agent_deadline(self):
        release, finished = Event(), Event()
        requests = []
        def transport(request):
            requests.append(request)
            release.wait(2)
            return httpx.Response(500, json={"error": {"message": "synthetic", "type": "server_error"}})
        sdk = OpenAI(api_key="test-placeholder", http_client=httpx.Client(transport=httpx.MockTransport(transport)),
                     max_retries=1)
        self.addCleanup(sdk.close)
        class ObservedClient(OpenAIClient):
            def generate(self, messages, tools):
                try:
                    return super().generate(messages, tools)
                finally:
                    finished.set()
        client = ObservedClient(sdk, model="configured-test-model")
        with patch.object(OpenAI, "_calculate_retry_timeout", return_value=0):
            try:
                result = AgentLoopRunner(client, production_registry(), request_timeout_seconds=0.1).run("synthetic", CONTEXT)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.errors[-1].code, "request_timeout")
                self.assertEqual(len(requests), 1)
            finally:
                release.set()
                self.assertTrue(finished.wait(1))
            self.assertEqual(len(requests), 1)

    def test_invalid_network_timeout_configuration_rejected(self):
        client = self.sdk(lambda request: httpx.Response(200))
        for value in (True, 0, -1, float("nan"), float("inf"), None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OpenAIClient(client.sdk, model="configured-test-model", timeout_seconds=value)

    def test_sdk_tool_roundtrip(self):
        requests = []
        def transport(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if len(requests) == 1:
                return httpx.Response(200, json=completion({"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function", "function": {
                        "name": ToolName.PATIENT_PROFILE.value, "arguments": "{}"}}]}, "tool_calls"))
            return httpx.Response(200, json=completion({"role": "assistant", "content": "완료"}))
        result = AgentLoopRunner(self.sdk(transport), fake_registry()).run("  원문\n", CONTEXT)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["model"], "configured-test-model")
        self.assertEqual(requests[0]["messages"][2]["content"], "  원문\n")
        self.assertEqual({tool["function"]["name"] for tool in requests[0]["tools"]},
                         {"get_patient_profile", "get_recent_care_logs", "search_evidence"})
        self.assertEqual(len(requests[0]["tools"]), 3)
        followup = requests[1]["messages"]
        self.assertEqual(followup[-2]["tool_calls"][0]["function"]["arguments"], "{}")
        self.assertEqual(followup[-1]["tool_call_id"], "call_1")
        self.assertTrue(json.loads(followup[-1]["content"])["success"])
        self.assertTrue(all("kind" not in m for m in followup))

    def test_empty_registry_omits_tools(self):
        requests = []
        def transport(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=completion({"role": "assistant", "content": "안녕"}))
        result = AgentLoopRunner(self.sdk(transport), production_registry()).run("안녕", CONTEXT)
        self.assertEqual(result.status, "completed")
        self.assertNotIn("tools", requests[0])
        self.assertNotIn("tool_choice", requests[0])

    def test_provider_error_not_exposed(self):
        def transport(request):
            return httpx.Response(401, json={"error": {"message": "PRIVATE secret", "type": "auth_error"}})
        result = AgentLoopRunner(self.sdk(transport), production_registry()).run("질문", CONTEXT)
        self.assertEqual(result.status, "provider_error")
        self.assertNotIn("PRIVATE", result.model_dump_json())

    def test_truncated_provider_response_is_not_final_answer(self):
        def transport(request):
            return httpx.Response(200, json=completion({"role": "assistant", "content": "잘린 답변"}, "length"))
        result = AgentLoopRunner(self.sdk(transport), production_registry()).run("질문", CONTEXT)
        self.assertEqual(result.status, "provider_error")
        self.assertIsNone(result.final_answer)

    def test_invalid_json_arguments_become_tool_error(self):
        requests = []
        def transport(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(200, json=completion({"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function", "function": {
                        "name": ToolName.PATIENT_PROFILE.value, "arguments": "{invalid"}}]}, "tool_calls"))
            return httpx.Response(200, json=completion({"role": "assistant", "content": "복구"}))
        result = AgentLoopRunner(self.sdk(transport), fake_registry()).run("질문", CONTEXT)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.errors[0].code, "invalid_arguments")
        self.assertEqual(requests[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"], "{invalid")
