"""Production Stage 1 through real execution/Runner and structured handoff."""
import ast
import inspect
import json
import unittest
from unittest.mock import patch

from .. import agent_loop_runner
from ..agent_loop_runner import AgentLoopRunner
from ..outputs import ChatAnswerV1, FeedAnswerV2, StructuredAgentResult
from ..registry import ToolName, production_registry
from ..schemas import AgentResult, ModelTurn, ToolCall
from ..testing.fake_llm import FakeLLMClient
from ..testing.feed_workflow_fakes import FakeFeedDependencies, STAGES, build_feed_test_registry
from ..tools.contracts import EvidencePackage, EvidenceSearchInput
from ..workflows import feed
from ..workflows.feed import FeedWorkflow, FeedWorkflowError
from ..testing import feed_workflow_fakes as fake_module
from ..workflows.feed_context_prepare import prepare_production_feed_input
from ..workflows.feed_context_loader import DefaultFeedContextLoader
from .test_feed_context_loader import (
    FakeFeedContextSource, empty_snapshot, full_snapshot, NOW as SOURCE_NOW,
    SERVICE_TZ, PRIVATE as SOURCE_PRIVATE,
)
from .test_feed_execution import search_call, final_turn
from .test_feed_context_production_source import CONTEXT, NOW, P2, patient, snapshot


class FeedWorkflowTests(unittest.TestCase):
    def test_real_prepare_execution_runner_and_handoff_preserve_input_and_sources(self):
        second = patient(P2)
        second["symptoms"] = ["second patient signal"]
        raw = snapshot([second, patient()])
        before = raw.model_dump()
        context = CONTEXT.model_copy(update={"patient_id": P2})
        deps = FakeFeedDependencies(context, snapshot=raw, reference_time=NOW,
            turns=[ModelTurn(tool_calls=[search_call()]), final_turn()])
        workflow = deps.workflow()
        with patch.object(fake_module, "prepare_production_feed_input", wraps=prepare_production_feed_input) as prepare, \
             patch.object(deps.runner, "run_structured", wraps=deps.runner.run_structured) as run:
            self.assertIs(workflow.run(context), deps.receipt)
        prepare.assert_called_once_with(context, deps.loader)
        self.assertEqual(len(deps.read_calls), 1)
        self.assertEqual(deps.read_calls[0]["user_id"], context.user_id)
        run.assert_called_once()
        prepared = deps.received["feed_execution"][1]
        self.assertEqual(run.call_args.args[0], prepared)
        self.assertIs(type(run.call_args.args[0]), str)
        self.assertEqual(run.call_args.kwargs["previously_recommended_pmids"], frozenset())
        user = next(m for m in deps.client.requests[0][0] if m.kind == "user_input")
        self.assertEqual(user.content, prepared)
        patients = json.loads(prepared)["patients"]
        self.assertEqual([p["patient_ref"] for p in patients], ["patient_1", "patient_2"])
        self.assertEqual([p["symptoms"] for p in patients], [["signal 1"], ["second patient signal"]])
        self.assertEqual(json.loads(prepared)["feed_scope"], "caregiver_all")
        self.assertEqual(raw.model_dump(), before)
        self.assertEqual(deps.events, list(STAGES))
        self.assertEqual(deps.saved_owners, [CONTEXT.user_id])
        handed = deps.saved[0]
        self.assertEqual(handed, deps.agent_result)
        self.assertIsInstance(handed.output, FeedAnswerV2)
        self.assertEqual(handed.sources[0].pmid, "FAKE-PMID")
        self.assertEqual(handed.sources[0].title, "[FAKE] Dementia sleep orchestration fixture")
        self.assertEqual(handed.sources[0].source_ref, handed.output.items[0].source_ref)
        for messages, _ in deps.client.requests:
            text = json.dumps([m.model_dump(mode="json") for m in messages])
            for forbidden in (CONTEXT.request_id, str(CONTEXT.user_id), str(CONTEXT.patient_id),
                              "PRIVATE", "stored stopped drug", "stored current drug", "stored department",
                              "reference_time", "updated_at", "diagnosis_date", "assessed_at", "result_detail",
                              "burden_score", "mood_score", "2026-10", "coverage", "long_term_summary"):
                self.assertNotIn(forbidden, text)

    def test_workflow_observes_first_evidence_then_llm_chooses_followup(self):
        test = self
        class EvidenceDrivenFake(FakeLLMClient):
            def generate(self, request):
                messages, tools = request.inputs, request.tools
                if len(self.requests) == 1:
                    candidates = json.loads(next(m.content for m in messages if m.kind == "evidence"))
                    test.assertEqual(candidates["candidate_count"], 1)
                    test.assertEqual(candidates["candidates"][0]["title"], "[FAKE] Dementia sleep orchestration fixture")
                    self.turns.append(ModelTurn(tool_calls=[search_call("search_2", "Caregiver support research", revision=1, retain=(0,))]))
                elif len(self.requests) == 2:
                    test.assertEqual([m.call_id for m in messages if m.type == "function_call_output"],
                                     ["search_1", "search_2"])
                    self.turns.append(final_turn())
                return super().generate(request)
        client = EvidenceDrivenFake([ModelTurn(tool_calls=[search_call()])])
        deps = FakeFeedDependencies(CONTEXT, client=client)
        self.assertIs(deps.workflow().run(CONTEXT), deps.receipt)
        self.assertEqual(deps.search_queries, ["Dementia sleep research", "Caregiver support research"])
        self.assertEqual(deps.saved[0].sources[0].source_ref.tool_call_id, "search_1")  # First duplicate PMID wins.

    def test_each_stage_exception_stops_later_dependencies_and_hides_raw_details(self):
        for position, stage in enumerate(STAGES):
            with self.subTest(stage=stage):
                deps = FakeFeedDependencies(CONTEXT, fail_at=stage)
                with self.assertRaises(FeedWorkflowError) as caught:
                    deps.workflow().run(CONTEXT)
                self.assertEqual(caught.exception.stage, stage)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertEqual(deps.events, list(STAGES[:position + 1]))
                self.assertEqual(deps.saved, [])
                if stage != "backend_handoff":
                    self.assertEqual(deps.client.requests, [])

    def test_invalid_backend_ownership_stops_before_provider(self):
        deps = FakeFeedDependencies(CONTEXT)
        deps.snapshot.managed_patients.items[0].caregiver_id = CONTEXT.patient_id
        with self.assertRaises(FeedWorkflowError) as caught:
            deps.workflow().run(CONTEXT)
        self.assertEqual(caught.exception.stage, "context_prepare")
        self.assertEqual(deps.client.requests, [])
        self.assertEqual(deps.saved, [])

    def test_failed_or_invalid_agent_result_never_reaches_backend(self):
        cases = (
            [RuntimeError("PRIVATE provider")],
            [ModelTurn(text="PRIVATE invalid JSON")],
            [ModelTurn(tool_calls=[search_call()]), ModelTurn(text='{"decisions":[{"include":false,"source_ref":{"tool_call_id":"nonexistent","evidence_index":0}}]}')],
            [ModelTurn(tool_calls=[search_call()]), final_turn(count=2)],
            [ModelTurn(tool_calls=[search_call(f"search_{i}")]) for i in range(4)],
            [ModelTurn(tool_calls=[search_call(f"search_{i}") for i in range(4)])],
        )
        for turns in cases:
            with self.subTest(turns=len(turns)):
                deps = FakeFeedDependencies(CONTEXT, turns=turns)
                with self.assertRaises(FeedWorkflowError) as caught:
                    deps.workflow().run(CONTEXT)
                self.assertEqual(caught.exception.stage, "backend_handoff")
                self.assertEqual(deps.events, list(STAGES[:2]))
                self.assertIsNone(deps.agent_result.output)
                self.assertEqual(deps.agent_result.sources, [])
                self.assertEqual(deps.saved, [])
                self.assertLessEqual(len(deps.search_queries), 3)

    def test_three_calls_allow_final_after_third_result(self):
        for batches in ((1, 1, 1), (3,)):
            turns, identifier = [], 0
            for revision, batch in enumerate(batches):
                calls = []
                for _ in range(batch):
                    identifier += 1
                    calls.append(search_call(f"search_{identifier}", f"Research perspective {identifier}",
                                             revision=revision, retain=(0,)))
                turns.append(ModelTurn(tool_calls=calls))
            deps = FakeFeedDependencies(CONTEXT, turns=turns + [final_turn()])
            self.assertIs(deps.workflow().run(CONTEXT), deps.receipt)
            self.assertEqual(deps.saved[0].execution.total_tool_calls, 3)
            self.assertEqual(deps.saved[0].execution.tool_rounds, len(batches))

    def test_empty_evidence_and_empty_feed_are_successful_not_failed_reads(self):
        deps = FakeFeedDependencies(CONTEXT.model_copy(update={"patient_id": None}),
            snapshot=snapshot([], full=False), reference_time=NOW,
            turns=[ModelTurn(tool_calls=[search_call()]), final_turn(count=0)],
            handler=lambda context, args: EvidencePackage(query=args.query, evidence=[]))
        self.assertIs(deps.workflow().run(CONTEXT.model_copy(update={"patient_id": None})), deps.receipt)
        self.assertEqual(deps.saved[0].output.items, [])
        self.assertEqual(deps.saved[0].sources, [])
        self.assertEqual(deps.saved[0].execution.errors, [])
        tool = next(m for m in deps.client.requests[1][0] if m.type == "function_call_output")
        self.assertTrue(json.loads(tool.content)["success"])

    def test_timeout_and_search_exception_stop_without_retry(self):
        for error in (TimeoutError("PRIVATE"), RuntimeError("PRIVATE")):
            def handler(context, args):
                raise error
            deps = FakeFeedDependencies(CONTEXT, handler=handler,
                turns=[ModelTurn(tool_calls=[search_call()]), final_turn(count=0)])
            with self.assertRaises(FeedWorkflowError):
                deps.workflow().run(CONTEXT)
            self.assertEqual(len(deps.client.requests), 1)
            self.assertEqual(deps.saved, [])
            self.assertEqual(deps.agent_result.execution.status, "failed")
            self.assertEqual(deps.agent_result.execution.total_tool_calls, 1)

    def test_disallowed_profile_tool_never_executes_or_discloses_profile(self):
        deps = FakeFeedDependencies(CONTEXT, turns=[ModelTurn(tool_calls=[
            ToolCall(id="profile", name="get_patient_profile", arguments={})]), final_turn(count=0)])
        with self.assertRaises(FeedWorkflowError):
            deps.workflow().run(CONTEXT)
        self.assertEqual(deps.agent_result.execution.errors[0].code, "feed_search_required")
        self.assertEqual(deps.agent_result.execution.total_tool_calls, 0)
        self.assertEqual(len(deps.client.requests), 1)
        self.assertIsNone(deps.runner.executor.registry.get("get_patient_profile"))

    def test_mutated_execution_configuration_stops_before_provider_and_handoff(self):
        deps = FakeFeedDependencies(CONTEXT)
        workflow = deps.workflow()
        deps.runner.max_total_tool_calls = 4
        with self.assertRaises(FeedWorkflowError) as caught:
            workflow.run(CONTEXT)
        self.assertEqual(caught.exception.stage, "feed_execution")
        self.assertEqual(deps.client.requests, [])
        self.assertEqual(deps.saved, [])

    def test_handoff_revalidates_nested_contract_and_requires_feed_for_same_request(self):
        for mutate in (lambda r: setattr(r.output, "response_type", "chat"),
                       lambda r: r.sources.clear(),
                       lambda r: setattr(r.execution, "request_id", "another request"),
                       lambda r: setattr(r.output.items[0], "personal_reason", "")):
            deps = FakeFeedDependencies(CONTEXT, turns=[ModelTurn(tool_calls=[search_call()]), final_turn()])
            workflow = deps.workflow()
            def execute(context, provider_input):
                result = deps.run_feed(context, provider_input)
                mutate(result)
                return result
            workflow.feed_execution = execute
            with self.assertRaises(FeedWorkflowError) as caught:
                workflow.run(CONTEXT)
            self.assertEqual(caught.exception.stage, "backend_handoff")
            self.assertEqual(deps.saved, [])

    def test_completed_chat_output_is_rejected_before_feed_handoff(self):
        result = StructuredAgentResult(
            execution=AgentResult(status="completed", request_id=CONTEXT.request_id,
                final_answer="Synthetic answer", tool_rounds=0, total_tool_calls=0,
                called_tools=[], errors=[]),
            output=ChatAnswerV1(schema_version="1", response_type="chat",
                answer="Synthetic answer", citation_refs=[]), sources=[])
        deps = FakeFeedDependencies(CONTEXT)
        workflow = deps.workflow()
        workflow.feed_execution = lambda context, provider_input: result
        with self.assertRaises(FeedWorkflowError) as caught:
            workflow.run(CONTEXT)
        self.assertEqual(caught.exception.stage, "backend_handoff")
        self.assertNotIn("backend_handoff", deps.events)
        self.assertEqual(deps.saved, [])
        self.assertEqual(deps.saved_owners, [])

    def test_handoff_mutation_does_not_modify_runner_result(self):
        deps = FakeFeedDependencies(CONTEXT,
            turns=[ModelTurn(tool_calls=[search_call()]), final_turn()])
        original_payload = []
        def mutate_handoff(context, result):
            original = deps.agent_result
            original_payload.append(original.model_dump())
            self.assertEqual(result, original)
            self.assertIsNot(result, original)
            self.assertIsNot(result.output.items[0], original.output.items[0])
            self.assertIsNot(result.sources[0], original.sources[0])
            self.assertIsNot(result.output.items[0].source_ref,
                             original.output.items[0].source_ref)
            self.assertIsNot(result.sources[0].source_ref, original.sources[0].source_ref)
            result.output.items[0].personal_reason = "Synthetic handoff mutation"
            result.output.items[0].category = "prevention"
            result.sources[0].title = "Synthetic handoff source"
            result.output.items[0].source_ref.evidence_index = 1
            result.sources[0].source_ref.evidence_index = 1
            self.assertEqual(result.sources[0].source_ref, result.output.items[0].source_ref)
            self.assertEqual(original.model_dump(), original_payload[0])
            return deps.handoff(context, result)
        workflow = deps.workflow()
        workflow.backend_handoff = mutate_handoff
        self.assertIs(workflow.run(CONTEXT), deps.receipt)
        self.assertEqual(deps.agent_result.model_dump(), original_payload[0])
        self.assertEqual(deps.saved[0].output.items[0].personal_reason, "Synthetic handoff mutation")
        self.assertEqual(deps.saved[0].output.items[0].category, "prevention")
        self.assertEqual(deps.saved_owners, [CONTEXT.user_id])

    def test_failed_search_evidence_cannot_be_cited_in_final_feed(self):
        def failed_search(context, args):
            raise RuntimeError("PRIVATE synthetic failed search")
        deps = FakeFeedDependencies(CONTEXT, handler=failed_search,
            turns=[ModelTurn(tool_calls=[search_call()]), final_turn()])
        with self.assertRaises(FeedWorkflowError) as caught:
            deps.workflow().run(CONTEXT)
        self.assertEqual(caught.exception.stage, "backend_handoff")
        self.assertEqual(deps.agent_result.execution.status, "failed")
        self.assertEqual(deps.agent_result.execution.total_tool_calls, 1)
        self.assertIn("tool_exception", [error.code for error in deps.agent_result.execution.errors])
        self.assertEqual(len(deps.client.requests), 1)
        self.assertIsNone(deps.agent_result.output)
        self.assertIsNone(deps.agent_result.execution.final_answer)
        self.assertEqual(deps.agent_result.sources, [])
        self.assertNotIn("backend_handoff", deps.events)
        self.assertEqual(deps.saved, [])
        self.assertEqual(deps.saved_owners, [])

    def test_handoff_has_no_fake_persistence_default(self):
        deps = FakeFeedDependencies(CONTEXT)
        with self.assertRaises(TypeError):
            FeedWorkflow(context_prepare=deps.load_context, feed_execution=deps.execution)


class SharedFeedContextIntegrationTests(unittest.TestCase):
    def production(self, context, snapshot, turns, *, handler=None):
        kwargs = {} if handler is None else {"handler": handler}
        deps = FakeFeedDependencies(context, turns=turns, **kwargs)
        loader = DefaultFeedContextLoader(source=FakeFeedContextSource(snapshot),
            clock=lambda: SOURCE_NOW, service_timezone=SERVICE_TZ)
        def prepare(context):
            deps._enter("context_prepare", context)
            return prepare_production_feed_input(context, loader)
        workflow = deps.workflow()
        workflow.context_prepare = prepare
        return deps, workflow

    def test_production_all_patients_reuse_execution_runner_and_handoff(self):
        context = CONTEXT.model_copy(update={"patient_id": None})
        deps, workflow = self.production(context, full_snapshot(),
            [ModelTurn(tool_calls=[search_call()]), final_turn()])
        with patch.object(deps.runner, "run_structured", wraps=deps.runner.run_structured) as run:
            self.assertIs(workflow.run(context), deps.receipt)
        run.assert_called_once()
        self.assertEqual(deps.events, list(STAGES))
        payload = json.loads(run.call_args.args[0])
        self.assertEqual(payload["feed_scope"], "caregiver_all")
        self.assertEqual([p["patient_ref"] for p in payload["patients"]], ["patient_1", "patient_2"])
        self.assertEqual(deps.saved_owners, [context.user_id])
        self.assertEqual(deps.saved[0].sources[0].pmid, "FAKE-PMID")
        for messages, _ in deps.client.requests:
            text = json.dumps([m.model_dump(mode="json") for m in messages], ensure_ascii=False)
            self.assertNotIn(SOURCE_PRIVATE, text)
            self.assertNotIn(str(context.user_id), text)
            self.assertFalse(any(m.kind == "agent_context" for m in messages))

    def test_production_cold_start_can_search_and_handoff_general_news(self):
        context = CONTEXT.model_copy(update={"patient_id": None})
        turn = final_turn()
        value = json.loads(turn.text)
        reason = "치매 가족 돌봄에 참고할 수 있는 최신 연구이기 때문입니다."
        value["decisions"][0]["personal_reason"] = reason
        query = "General dementia cognitive health caregiver research"
        turns = [ModelTurn(tool_calls=[search_call(query=query)]), ModelTurn(text=json.dumps(value))]
        deps, workflow = self.production(context, empty_snapshot(), turns)
        with patch.object(deps.runner, "run_structured", wraps=deps.runner.run_structured) as run:
            self.assertIs(workflow.run(context), deps.receipt)
        run.assert_called_once()
        self.assertEqual(deps.events, list(STAGES))
        self.assertEqual(deps.search_queries, [query])
        self.assertEqual(deps.saved[0].output.items[0].personal_reason, reason)
        self.assertEqual(deps.saved[0].sources[0].pmid, "FAKE-PMID")
        messages = deps.client.requests[0][0]
        payload = json.loads(next(m.content for m in messages if m.kind == "user_input"))
        self.assertEqual(payload, {"feed_scope": "caregiver_all", "caregiver": None, "patients": [], "recent_feed_titles": []})
        policy = "\n".join(m.content for m in messages if m.role == "system")
        self.assertIn("Cold Start", policy)
        self.assertIn("일반적인 정보적 관련성", policy)
        self.assertNotIn("근거/개인화 정보가 부족하면 items=[]", policy)

    def test_cold_start_empty_evidence_and_invalid_or_failed_citations(self):
        context = CONTEXT.model_copy(update={"patient_id": None})
        for behavior in ("empty", "invalid_ref", "failed_search"):
            def handler(context, args):
                if behavior == "failed_search":
                    raise RuntimeError("PRIVATE failed search")
                return EvidencePackage(query=args.query, evidence=[])
            turns = [ModelTurn(tool_calls=[search_call()]), final_turn(count=0 if behavior == "empty" else 1)]
            deps, workflow = self.production(context, empty_snapshot(), turns, handler=handler)
            with self.subTest(behavior=behavior):
                if behavior == "empty":
                    self.assertIs(workflow.run(context), deps.receipt)
                    self.assertEqual(deps.saved[0].output.items, [])
                else:
                    with self.assertRaises(FeedWorkflowError):
                        workflow.run(context)
                    self.assertEqual(deps.agent_result.execution.status,
                                     "failed" if behavior == "failed_search" else "invalid_response")
                    self.assertIsNone(deps.agent_result.output)
                    self.assertEqual(deps.agent_result.sources, [])
                    self.assertEqual(deps.saved, [])

    def test_unprepared_dtos_stop_at_stage_one_before_execution(self):
        context = CONTEXT.model_copy(update={"patient_id": None})
        for value in (None, {}, snapshot([], full=False), "", "  ", "not JSON", "[]", "null"):
            deps = FakeFeedDependencies(context)
            workflow = deps.workflow()
            workflow.context_prepare = lambda context: value
            with self.assertRaises(FeedWorkflowError) as caught:
                workflow.run(context)
            self.assertEqual(caught.exception.stage, "context_prepare")
            self.assertEqual(deps.client.requests, [])
            self.assertEqual(deps.saved, [])


class FeedArchitectureTests(unittest.TestCase):
    def test_workflow_has_no_engine_creation_or_tool_loop(self):
        self.assertFalse(issubclass(FeedWorkflow, AgentLoopRunner))
        tree = ast.parse(inspect.getsource(feed))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                self.assertNotEqual(getattr(node.func, "id", None), "AgentLoopRunner")
                self.assertNotIn(getattr(node.func, "attr", None), ("generate", "run_structured", "execute"))
            self.assertNotIsInstance(node, (ast.For, ast.While))
        self.assertFalse(hasattr(feed, "run_feed_agent_loop"))

    def test_shared_engine_defaults_and_registry_are_unchanged(self):
        signature = inspect.signature(AgentLoopRunner)
        self.assertEqual(signature.parameters["max_tool_rounds"].default, 5)
        self.assertEqual(signature.parameters["max_total_tool_calls"].default, 10)
        self.assertEqual(production_registry().definitions(), [])
        self.assertEqual({name.value for name in ToolName},
                         {"get_patient_profile", "get_recent_care_logs", "search_evidence"})
        self.assertNotIn("workflow_type", inspect.getsource(agent_loop_runner))

    def test_test_query_hook_precedes_each_search_and_rejection_skips_handler(self):
        events = []
        def validate(context, args):
            events.append("validate")
            if args.query == "[FAKE BLOCKED]":
                raise ValueError("rejected fixture")
        def search(context, args):
            events.append("search")
            return EvidencePackage(query=args.query, evidence=[])
        registry = build_feed_test_registry(query_validator=validate, handler=search)
        spec = registry.get("search_evidence")
        spec.handler(CONTEXT, EvidenceSearchInput(query="synthetic care"))
        with self.assertRaises(ValueError):
            spec.handler(CONTEXT, EvidenceSearchInput(query="[FAKE BLOCKED]"))
        self.assertEqual(events, ["validate", "search", "validate"])
