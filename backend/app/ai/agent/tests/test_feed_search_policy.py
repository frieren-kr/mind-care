"""Offline Feed search policy and real SDK wire contract."""
import json
import unittest

import httpx
from openai import OpenAI

from ..clients.base import GenerationRequest
from ..clients.openai_client import OpenAIClient
from ..schemas import Message, ModelTurn
from ..tools.search_evidence import SEARCH_EVIDENCE_DATA_POLICY
from ..workflows.feed_execution import FeedExecutionV1
from . import test_feed_candidates as candidates
from .test_feed_candidates import decisions, package, review_turn
from .test_workflows import CONTEXT
from .test_openai_client import response_fixture


class FeedSearchPolicyTests(unittest.TestCase):
    setUp = candidates.FeedCandidateRunnerTests.setUp
    run_feed = candidates.FeedCandidateRunnerTests.run_feed
    search = staticmethod(candidates.FeedCandidateRunnerTests.search)

    def test_no_search_or_wrong_tool_is_rejected_without_second_generation(self):
        from ..schemas import ToolCall
        for turn in (ModelTurn(text=json.dumps(decisions(0))),
                     ModelTurn(tool_calls=[ToolCall(id="bad", name="other", arguments={})])):
            result, client, _ = self.run_feed([turn], {})
            self.assertEqual(result.execution.status, "invalid_response")
            self.assertEqual(result.execution.errors[-1].code, "feed_search_required")
            self.assertEqual(result.execution.total_tool_calls, 0)
            self.assertEqual(len(client.requests), 1)
            self.assertIsNone(client.options[0].required_tool)
            self.assertEqual(client.options[0].tool_choice, "required")
            first = client.requests[0][0]
            self.assertFalse(any(m.kind == "evidence" for m in first))
            self.assertFalse(any("FeedDecisionDraft" in (m.content or "") for m in first))

    def test_empty_missing_pmid_and_duplicate_results_have_actual_progress(self):
        for pmids in ([], [None], ["A"]):
            for calls in (1, 2, 3):
                turns = [review_turn(self.search(str(i), str(i)), 1 if pmids == ["A"] else 0, (0,) if pmids == ["A"] else ()) if i else self.search(str(i), str(i)) for i in range(calls)]
                turns += [ModelTurn(text=json.dumps(decisions(1 if pmids == ["A"] else 0)))]
                result, client, _ = self.run_feed(turns, {str(i): package(pmids) for i in range(calls)},
                    max_tool_rounds=3, max_total_tool_calls=9)
                self.assertEqual(result.execution.status, "completed")
                self.assertEqual(len(client.requests), calls + 1)
                self.assertEqual([o.tool_choice for o in client.options[1:]],
                                 ["auto"] * (calls - 1) + ["none" if calls == 3 else "auto"])
                state = json.loads(client.requests[-1][0][-1].content)
                self.assertEqual(state["search_progress"], {
                    "phase": "exhausted" if calls == 3 else "evaluating",
                    "search_calls_used": calls, "search_calls_remaining": 9-calls,
                    "search_rounds_used": calls, "search_rounds_remaining": 3-calls, "searches_per_round_limit": 3,
                    "successful_searches": calls, "failed_searches": 0, "model_tool_calls_used": calls})
                self.assertEqual(state["last_search"]["returned_evidence_count"], len(pmids))
                self.assertEqual(state["last_search"]["new_eligible_candidate_count"],
                                 1 if pmids == ["A"] and calls == 1 else 0)
                self.assertEqual(state["candidate_status"], "available" if pmids == ["A"] else "empty")
                self.assertEqual(GenerationRequest(inputs=(), tools=[]).options.tool_choice, "auto")

    def test_sdk_forces_first_search_auto_then_none_at_budget(self):
        requests = []
        def transport(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if len(requests) <= 3:
                message = {'text': None, 'calls': [{'type': 'function_call', 'id': 'item_' + f'opaque_{len(requests)}', 'call_id': f'opaque_{len(requests)}', 'name': 'search_evidence', 'arguments': json.dumps({'query': f'research {len(requests)}', **({'review': {'decisions':[{'include':True, 'personal_reason':'Retained', 'category':'care'}]}} if len(requests) > 1 else {})}), 'status': 'completed'}]}
                return httpx.Response(200, json=response_fixture(message))
            return httpx.Response(200, json=response_fixture({'text': json.dumps(decisions(1, (0,)))}))
        sdk = OpenAI(api_key="offline-placeholder", http_client=httpx.Client(transport=httpx.MockTransport(transport)))
        self.addCleanup(sdk.close)
        from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
        execution = FeedExecutionV1(OpenAIClient(sdk, model="offline-model"), ToolRegistry((
            ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], lambda context, args: package(["A"])),)))
        result = execution(CONTEXT.model_copy(update={"patient_id":None}), '{"patients":[]}')
        self.assertEqual(result.execution.status, "completed")
        self.assertEqual(result.sources[0].source_ref.tool_call_id, "opaque_1")
        self.assertEqual([p["tool_choice"] for p in requests], [
            "required", "auto", "auto", "none"])
        self.assertEqual(len(requests), 4)
        first_wire = requests[0]["input"]
        self.assertFalse(any("FeedDecisionDraft" in (m.get("content") or "") for m in first_wire))
        self.assertFalse(any('"candidate_count"' in (m.get("content") or "") for m in first_wire))
        for payload in requests[1:]:
            self.assertEqual(sum('"title": "FeedDecisionDraft"' in (m.get("content") or "")
                                 for m in payload["input"]), 1)
            self.assertEqual(sum('"untrusted_feed_candidates"' in (m.get("content") or "")
                                 for m in payload["input"]), 1)
        for payload in requests:
            self.assertTrue(payload["parallel_tool_calls"])
            self.assertNotIn("reasoning", payload)
            self.assertEqual(payload["tools"][0]["name"], "search_evidence")
        OpenAIClient(sdk, model="offline-model").generate(
            GenerationRequest(inputs=(Message(role="user", kind="user_input", content="Synthetic Chat request"),), tools=requests[0]["tools"]))
        self.assertEqual(requests[-1]["tool_choice"], "auto")
        self.assertNotIn("parallel_tool_calls",requests[-1])
        self.assertEqual(GenerationRequest(inputs=(), tools=[]).options.tool_choice, "auto")

    def test_failed_search_state_and_option_cleanup_on_exception(self):
        from ..feed_candidates import FeedCandidates
        from ..clients.base import GenerationOptions, GenerationRequest
        table = FeedCandidates()
        table.record_search(False)
        state = json.loads(table.message(2).content)
        self.assertEqual(state["search_progress"]["failed_searches"], 1)
        self.assertEqual(state["search_progress"]["successful_searches"], 0)
        self.assertFalse(state["last_search"]["success"])
        class BrokenClient:
            def generate(self, request):
                messages, tools = request.inputs, request.tools
                self.options = request.options
                raise RuntimeError("synthetic failure")
        client = BrokenClient()
        with self.assertRaises(RuntimeError):
            client.generate(GenerationRequest(inputs=(), tools=[], options=GenerationOptions(required_tool="search_evidence")))
        self.assertEqual(client.options.required_tool, "search_evidence")
        self.assertEqual(GenerationRequest(inputs=(), tools=[]).options.tool_choice, "auto")

    def test_feed_messages_separate_search_planning_from_evaluation(self):
        from ..testing.fake_llm import FakeLLMClient
        from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
        prepared = '{"feed_scope":"caregiver_all","patients":[]}'
        for pmids in ([], [None], ["A"]):
            for calls in (1, 2, 3):
                with self.subTest(pmids=pmids, calls=calls):
                    turns = [review_turn(self.search(f"opaque_{i}", f"research {i}"), 1 if pmids == ["A"] else 0, (0,) if pmids == ["A"] else ()) if i else self.search(f"opaque_{i}", f"research {i}") for i in range(calls)]
                    turns.append(ModelTurn(text=json.dumps(decisions(1 if pmids == ["A"] else 0, (0,)))))
                    client = FakeLLMClient(turns)
                    registry = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE],
                        lambda context, args: package(pmids)),))
                    result = FeedExecutionV1(client, registry)(CONTEXT, prepared)
                    self.assertEqual(result.execution.status, "completed")
                    self.assertEqual(len(client.requests), calls + 1)
                    first, definitions = client.requests[0]
                    self.assertEqual([m.content for m in first if m.role == "user"], [prepared])
                    self.assertTrue(any("첫 검색을 반드시 실행" in (m.content or "") for m in first))
                    self.assertEqual(definitions[0]["name"], "search_evidence")
                    self.assertIsNone(client.options[0].required_tool)
                    self.assertEqual(client.options[0].tool_choice, "required")
                    self.assertFalse(any(m.kind == "evidence" for m in first))
                    for marker in ('"decisions"', 'decisions=[]', 'FeedDecisionDraft', '"candidate_count"', '"candidates"'):
                        self.assertFalse(any(marker in (m.content or "") for m in first), marker)
                    for used, (messages, _) in enumerate(client.requests[1:], 1):
                        policies = [m for m in messages if '"title": "FeedDecisionDraft"' in (m.content or "")]
                        self.assertEqual(len(policies), 1)
                        self.assertEqual(policies[0].role, "system")
                        projections = [m for m in messages if m.kind == "evidence"]
                        self.assertEqual(len(projections), 1)
                        self.assertEqual(projections[0].role, "user")
                        state = json.loads(projections[0].content)
                        self.assertEqual(state["search_progress"]["search_calls_used"], used)
                        self.assertEqual(state["search_progress"]["search_calls_remaining"], 9-used)
                        self.assertEqual(state["candidate_count"], int(pmids == ["A"]))
                        self.assertEqual([m.provider_output["call_id"] for m in messages if m.type == "provider_output" and m.provider_output["type"] == "function_call"],
                                         [m.call_id for m in messages if m.type == "function_call_output"])
                        results = [m for m in messages if m.type == "function_call_output"]
                        data_policies = [m for m in messages if m.content == SEARCH_EVIDENCE_DATA_POLICY]
                        self.assertEqual(len(data_policies), 1)
                        self.assertEqual(data_policies[0].role, "system")
                        self.assertGreater(messages.index(data_policies[0]), messages.index(results[0]))
                        self.assertLess(messages.index(data_policies[0]), messages.index(projections[0]))
                        self.assertTrue(all(messages.index(m) < messages.index(projections[0]) for m in results))
                        self.assertEqual(messages[-1], projections[0])
                    if pmids == ["A"]:
                        self.assertEqual(result.sources[0].source_ref.tool_call_id, "opaque_0")
                        self.assertEqual(result.sources[0].source_ref.evidence_index, 0)
