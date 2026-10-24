"""설치된 SDK + MockTransport로 실제 wire format을 검증한다. 네트워크 없음."""
import json
import unittest
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI

from ..clients.openai_client import OpenAIClient
from ..agent_loop_runner import AgentLoopRunner
from ..composition import build_evidence_registry
from ..outputs import FeedAnswerV2
from ..registry import ToolName, production_registry
from ..testing.fake_tools import fake_registry
from ..tools.contracts import EvidenceItem, EvidencePackage
from ..workflows.feed import FeedWorkflow, FeedWorkflowError
from ..workflows.feed_execution import FeedExecutionV1
from .test_feed_execution import CONTEXT as FEED_CONTEXT, prepared_input
from .test_workflows import CONTEXT


def completion(message, finish_reason="stop"):
    return {"id": "test-completion", "object": "chat.completion", "created": 0, "model": "test-model",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}]}


class OpenAIClientTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", side_effect=AssertionError("Live network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

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
        for payload in requests:
            self.assertEqual(payload["reasoning_effort"], "none")
            self.assertEqual(payload["tool_choice"], "auto")
            self.assertEqual(payload["tools"], requests[0]["tools"])
            self.assertEqual(payload["model"], "configured-test-model")
            self.assertNotIn("extra_body", payload)  # SDK flattens it into the HTTP JSON body.
        followup = requests[1]["messages"]
        self.assertEqual(followup[-2]["tool_calls"][0]["function"]["arguments"], "{}")
        self.assertEqual(followup[-2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(followup[-1]["role"], "tool")
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
        self.assertNotIn("reasoning_effort", requests[0])
        self.assertNotIn("extra_body", requests[0])

    def test_tool_provider_rejection_has_no_retry_model_fallback_or_error_leak(self):
        requests = []
        def transport(request):
            requests.append(json.loads(request.content))
            return httpx.Response(400, json={"error": {
                "message": "PRIVATE unsupported reasoning option", "type": "invalid_request_error"}})
        result = AgentLoopRunner(self.sdk(transport), fake_registry()).run("Synthetic request", CONTEXT)
        self.assertEqual(result.status, "provider_error")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["reasoning_effort"], "none")
        self.assertEqual(requests[0]["model"], "configured-test-model")
        self.assertNotIn("PRIVATE", result.model_dump_json())

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


class OpenAIFeedIntegrationTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket.connect", "psycopg.connect", "app.ai.retrieval.embedding.get_model"):
            guard = patch(target, side_effect=AssertionError("Live I/O forbidden in offline tests"))
            guard.start()
            self.addCleanup(guard.stop)

    @staticmethod
    def final_payload():
        return {"decisions": [{
            "include": True, "personal_reason": "Synthetic care relevance", "category": "care",
        }]}

    def scenario(self, *, final_text=None, arguments=None):
        requests = []
        arguments = json.dumps({"query": "Dementia sleep research", "top_k": 5}) if arguments is None else arguments
        final_text = json.dumps(self.final_payload()) if final_text is None else final_text
        def transport(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_sdk_opaque_feed", "type": "function", "function": {
                        "name": "search_evidence", "arguments": arguments}}]}
                return httpx.Response(200, json=completion(message, "tool_calls"))
            self.assertEqual(len(requests), 2)
            return httpx.Response(200, json=completion({"role": "assistant", "content": final_text}))
        sdk = OpenAI(api_key="offline-placeholder", timeout=90, max_retries=2,
                     http_client=httpx.Client(transport=httpx.MockTransport(transport)))
        self.addCleanup(sdk.close)
        evidence = EvidencePackage(query="Dementia sleep research", evidence=[EvidenceItem(
            evidence_type="new_research", pmid="SYNTHETIC-PMID", title="Synthetic dementia sleep research",
            relevance_score=0.8)])
        retriever = Mock()
        retriever.search_evidence.return_value = evidence
        execution = FeedExecutionV1(OpenAIClient(sdk, model="configured-test-model"),
                                    build_evidence_registry(retriever=retriever))
        results = []
        def execute(context, provider_input):
            result = execution(context, provider_input)
            results.append(result)
            return result
        prepare = Mock(return_value=prepared_input())
        receipt = object()
        handoff = Mock(return_value=receipt)
        workflow = FeedWorkflow(context_prepare=prepare, feed_execution=execute, backend_handoff=handoff)
        return SimpleNamespace(requests=requests, sdk=sdk, evidence=evidence, retriever=retriever,
            execution=execution, results=results, prepare=prepare, handoff=handoff,
            workflow=workflow, receipt=receipt)

    def test_sdk_feed_tool_result_and_v2_sources_reach_workflow_handoff(self):
        case = self.scenario()
        self.assertIs(case.workflow.run(FEED_CONTEXT), case.receipt)
        case.prepare.assert_called_once_with(FEED_CONTEXT)
        case.retriever.search_evidence.assert_called_once_with(
            query="Dementia sleep research", top_k=5, context=FEED_CONTEXT)
        self.assertEqual(len(case.requests), 2)
        for index, payload in enumerate(case.requests):
            self.assertEqual(payload["reasoning_effort"], "none")
            self.assertEqual(payload["model"], "configured-test-model")
            self.assertEqual(payload["tool_choice"], "required" if index == 0 else "auto")
            self.assertTrue(payload["parallel_tool_calls"])
            from ..feed_search_policy import feed_tool_definitions
            self.assertEqual(payload["tools"], feed_tool_definitions(case.execution.runner.registry.definitions()))
        self.assertIn(case.prepare.return_value, [m["content"] for m in case.requests[0]["messages"] if m["role"] == "user"])
        followup = case.requests[1]["messages"]
        assistant = next(m for m in followup if m.get("tool_calls"))
        tool = next(m for m in followup if m["role"] == "tool")
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_sdk_opaque_feed")
        self.assertEqual(tool["tool_call_id"], "call_sdk_opaque_feed")
        envelope = json.loads(tool["content"])
        self.assertTrue(envelope["success"])
        projection = json.loads(followup[-1]["content"])
        self.assertEqual(projection["candidate_count"], 1)
        self.assertEqual(projection["candidates"][0]["title"], case.evidence.evidence[0].title)
        self.assertNotIn("evidence", envelope["data"])
        case.handoff.assert_called_once()
        context, result = case.handoff.call_args.args
        self.assertEqual(context, FEED_CONTEXT)
        self.assertEqual(result.execution.status, "completed")
        self.assertEqual(result.execution.total_tool_calls, 1)
        self.assertEqual(result.execution.errors, [])
        self.assertIsInstance(result.output, FeedAnswerV2)
        self.assertEqual(result.output.model_dump(mode="json"), {
            "schema_version": "2", "response_type": "feed", "items": [{
                "source_ref": {"tool_call_id": "call_sdk_opaque_feed", "evidence_index": 0},
                "personal_reason": "Synthetic care relevance", "category": "care"}]})
        self.assertIsNone(result.execution.final_answer)
        self.assertEqual(result.sources[0].source_ref, result.output.items[0].source_ref)
        self.assertEqual(result.sources[0].pmid, case.evidence.evidence[0].pmid)
        self.assertEqual(result.sources[0].title, case.evidence.evidence[0].title)
        runner = case.execution.runner
        self.assertEqual((runner.max_tool_rounds, runner.max_total_tool_calls), (3, 3))
        self.assertEqual((runner.executor.timeout_seconds, runner.request_timeout_seconds), (30, 120))
        self.assertEqual((case.sdk.timeout, case.sdk.max_retries), (90, 2))

    def test_sdk_feed_invalid_schema_or_reference_never_reaches_handoff(self):
        invalid = ["{invalid JSON"]
        for change in ("missing_call", "out_of_range", "v1", "headline", "summary_bullets"):
            payload = self.final_payload()
            if change == "missing_call":
                payload["decisions"][0]["tool_call_id"] = "missing"
            elif change == "out_of_range":
                payload["decisions"][0]["evidence_index"] = 1
            elif change == "v1":
                payload["schema_version"] = "1"
            else:
                payload["decisions"][0][change] = ["Retired summary"] if change == "summary_bullets" else "Retired title"
            invalid.append(json.dumps(payload))
        for final_text in invalid:
            with self.subTest(final_text=final_text):
                case = self.scenario(final_text=final_text)
                with self.assertRaises(FeedWorkflowError) as caught:
                    case.workflow.run(FEED_CONTEXT)
                self.assertEqual(caught.exception.stage, "backend_handoff")
                self.assertEqual(len(case.requests), 2)
                result = case.results[0]
                self.assertEqual(result.execution.status, "invalid_response")
                self.assertEqual(result.execution.errors[-1].code,
                                 "invalid_feed_json" if final_text == "{invalid JSON" else "invalid_feed_decision")
                self.assertIsNone(result.output)
                self.assertEqual(result.sources, [])
                case.retriever.search_evidence.assert_called_once()
                case.handoff.assert_not_called()

    def test_sdk_feed_invalid_tool_arguments_cannot_supply_a_citation(self):
        case = self.scenario(arguments="{invalid")
        with self.assertRaises(FeedWorkflowError) as caught:
            case.workflow.run(FEED_CONTEXT)
        self.assertEqual(caught.exception.stage, "backend_handoff")
        self.assertEqual(len(case.requests), 1)
        result = case.results[0]
        self.assertEqual(result.execution.status, "failed")
        self.assertEqual(result.execution.errors[-1].code, "invalid_arguments")
        self.assertIsNone(result.output)
        self.assertEqual(result.sources, [])
        case.retriever.search_evidence.assert_not_called()
        case.handoff.assert_not_called()
