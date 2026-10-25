"""Production search assembly through existing Runner/Feed/Chat using Fake I/O."""
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.ai.retrieval import search as rag
from ..adapters.rag_evidence import RagEvidenceRetriever
from ..agent_loop_runner import AgentLoopRunner
from ..composition import build_evidence_registry
from ..outputs import ChatAnswerV1, FeedAnswerV2
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec, production_registry
from ..schemas import ModelTurn
from ..testing.chat_workflow_fakes import chat_final
from ..testing.fake_llm import FakeLLMClient
from ..workflows.chat_context_prepare import prepare_chat_input
from ..workflows.chat_execution import ChatExecutionV1
from ..workflows.feed import FeedWorkflow, FeedWorkflowError
from ..workflows.feed_execution import FeedExecutionV1
from .test_feed_execution import final_turn, prepared_input, search_call
from .test_search_evidence_adapter import package
from .test_workflows import CONTEXT, call, envelopes


class SearchEvidenceIntegrationTests(TestCase):
    def setUp(self):
        self.context = CONTEXT.model_copy(update={"patient_id": None})
        for target in ("psycopg.connect", "app.ai.retrieval.embedding.get_model"):
            guard = patch(target, side_effect=AssertionError("Live I/O forbidden in offline tests"))
            guard.start()
            self.addCleanup(guard.stop)

    def registry(self, search):
        return build_evidence_registry(retriever=RagEvidenceRetriever(search_fn=search))

    def test_runner_delivers_production_tool_result_and_default_limits_unchanged(self):
        search = Mock(return_value=package())
        registry = self.registry(search)
        client = FakeLLMClient([ModelTurn(tool_calls=[search_call()]), ModelTurn(text="Synthetic answer")])
        runner = AgentLoopRunner(client, registry)
        result = runner.run("Synthetic request", self.context)
        self.assertEqual((runner.max_tool_rounds, runner.max_total_tool_calls), (5, 10))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.total_tool_calls, 1)
        self.assertEqual(result.errors, [])
        self.assertEqual(envelopes(client)[0]["data"], package().model_dump(mode="json"))
        self.assertEqual(client.requests[0][1], registry.definitions())
        search.assert_called_once_with(query="Dementia sleep research", k=5)
        self.assertEqual(production_registry().definitions(), [])

    def test_default_factory_reaches_real_rag_function_through_runner_without_live_search(self):
        client = FakeLLMClient([ModelTurn(tool_calls=[search_call()]), final_turn()])
        paper = SimpleNamespace(paper_id=CONTEXT.patient_id, external_id="SYNTHETIC-PMID",
            title="Dementia caregiver guideline", abstract=None, published_date=None,
            publication_types=["Guideline"], mesh_terms=["Dementia"], journal=None, doi=None)
        hit = SimpleNamespace(paper_id=paper.paper_id, score=-0.4)
        with patch.object(rag, "embed_query", return_value=[0.0]) as embed, \
             patch.object(rag, "search_similar", return_value=[hit]) as nearest, \
             patch.object(rag, "get_papers_by_ids", return_value=[paper]), \
             patch.object(rag, "search", wraps=rag.search) as search:
            result = AgentLoopRunner(client, build_evidence_registry()).run_structured(
                "Synthetic request", self.context, output_model=FeedAnswerV2)
        self.assertEqual(result.execution.status, "completed")
        search.assert_called_once_with(query="Dementia sleep research", k=8)
        embed.assert_called_once_with("Dementia sleep research")
        nearest.assert_called_once_with([0.0], top_k=24, model_name=rag.MODEL_NAME)
        self.assertEqual(result.sources[0].pmid, "SYNTHETIC-PMID")
        self.assertEqual(result.sources[0].study_type, "guideline")
        self.assertEqual(result.sources[0].evidence_type, "new_research")

    def test_feed_accepts_registry_and_resolves_original_metadata(self):
        search = Mock(return_value=package())
        client = FakeLLMClient([ModelTurn(tool_calls=[search_call()]), final_turn()])
        execution = FeedExecutionV1(client, self.registry(search))
        result = execution(self.context, prepared_input())
        self.assertEqual((execution.runner.max_tool_rounds, execution.runner.max_total_tool_calls), (3, 9))
        self.assertEqual(result.execution.status, "completed")
        self.assertIsInstance(result.output, FeedAnswerV2)
        self.assertEqual(result.execution.errors, [])
        self.assertEqual(result.sources[0].source_ref, result.output.items[0].source_ref)
        for field in ("pmid", "doi", "title", "study_type", "evidence_type", "source_url", "journal"):
            self.assertEqual(getattr(result.sources[0], field), getattr(package().evidence[0], field))
        messages = json.dumps([m.model_dump(mode="json") for m in client.requests[0][0]])
        self.assertNotIn(str(self.context.user_id), messages)
        self.assertNotIn(self.context.request_id, messages)

    def test_feed_workflow_handoff_receives_validated_structured_result(self):
        client = FakeLLMClient([ModelTurn(tool_calls=[search_call()]), final_turn()])
        execution = FeedExecutionV1(client, self.registry(Mock(return_value=package())))
        prepare = Mock(return_value=prepared_input())
        receipt = object()
        handoff = Mock(return_value=receipt)
        result = FeedWorkflow(context_prepare=prepare, feed_execution=execution,
                              backend_handoff=handoff).run(self.context)
        self.assertIs(result, receipt)
        prepare.assert_called_once_with(self.context)
        self.assertEqual(handoff.call_args.args[1].execution.status, "completed")
        self.assertEqual(handoff.call_args.args[1].sources[0].pmid, "SYNTHETIC-PMID")

    def test_missing_call_out_of_range_or_empty_evidence_reference_is_rejected(self):
        injected = []
        for call_id, index in (("missing", 0), ("search_1", 2)):
            draft = json.loads(final_turn().text)
            draft["decisions"][0]["source_ref"] = {"tool_call_id": call_id, "evidence_index": index}
            injected.append(ModelTurn(text=json.dumps(draft)))
        for final, raw, code in ((injected[0], package(), "invalid_feed_decision"),
                                (injected[1], package(), "invalid_feed_decision"),
                                (final_turn(), rag.EvidencePackage(query="synthetic", evidence=[]), "candidate_count_mismatch")):
            client = FakeLLMClient([ModelTurn(tool_calls=[search_call()]), final])
            result = FeedExecutionV1(client, self.registry(Mock(return_value=raw)))(
                self.context, prepared_input())
            self.assertEqual(result.execution.status, "invalid_response")
            self.assertEqual(result.execution.errors[-1].code, code)
            self.assertIsNone(result.output)
            self.assertEqual(result.sources, [])

    def test_empty_search_can_finish_with_empty_feed(self):
        search = Mock(return_value=rag.EvidencePackage(query="synthetic", evidence=[]))
        client = FakeLLMClient([ModelTurn(tool_calls=[search_call()]), final_turn(count=0)])
        result = FeedExecutionV1(client, self.registry(search))(self.context, prepared_input())
        self.assertEqual(result.execution.status, "completed")
        self.assertEqual(result.output.items, [])
        self.assertEqual(result.sources, [])
        self.assertTrue(envelopes(client)[0]["success"])

    def test_generic_feed_search_error_stops_without_retry_or_citation(self):
        for final, status in ((final_turn(count=0), "failed"), (final_turn(), "failed")):
            search = Mock(side_effect=RuntimeError("PRIVATE retrieval error"))
            client = FakeLLMClient([ModelTurn(tool_calls=[search_call()]), final])
            result = FeedExecutionV1(client, self.registry(search))(self.context, prepared_input())
            self.assertEqual(result.execution.status, status)
            self.assertEqual(result.execution.errors[0].code, "tool_exception")
            self.assertEqual(result.sources, [])
            self.assertEqual(len(client.requests), 1)
            self.assertEqual(envelopes(client), [])
            self.assertNotIn("PRIVATE", result.model_dump_json())
            search.assert_called_once()

    def test_search_timeout_stops_runner_and_workflow_before_next_turn_or_handoff(self):
        search = Mock(side_effect=TimeoutError("PRIVATE retrieval timeout"))
        client = FakeLLMClient([ModelTurn(tool_calls=[search_call()]), final_turn(empty=True)])
        execution = FeedExecutionV1(client, self.registry(search))
        handoff = Mock()
        with self.assertRaises(FeedWorkflowError) as caught:
            FeedWorkflow(context_prepare=Mock(return_value=prepared_input()), feed_execution=execution,
                         backend_handoff=handoff).run(self.context)
        # Failed status is a normal execution result; the existing Workflow
        # rejects it at its handoff validation boundary before the callable.
        self.assertEqual(caught.exception.stage, "backend_handoff")
        self.assertEqual(len(client.requests), 1)
        handoff.assert_not_called()
        search.assert_called_once()
        # Inspect the unchanged Runner result without Workflow error wrapping.
        client = FakeLLMClient([ModelTurn(tool_calls=[search_call()]), final_turn(empty=True)])
        result = FeedExecutionV1(client, self.registry(search))(self.context, prepared_input())
        self.assertEqual(result.execution.status, "failed")
        self.assertEqual(result.execution.errors[0].code, "tool_timeout")

    def test_feed_budget_is_owned_by_existing_execution_not_handler(self):
        search = Mock(return_value=package())
        turns = [ModelTurn(tool_calls=[search_call(f"search_{i}", f"Research perspective {i}", revision=i-1)]) for i in range(1, 5)]
        client = FakeLLMClient(turns)
        result = FeedExecutionV1(client, self.registry(search))(self.context, prepared_input())
        self.assertEqual(result.execution.status, "limit_reached")
        self.assertEqual(result.execution.total_tool_calls, 3)
        self.assertEqual(search.call_count, 3)
        self.assertIsNone(result.output)

    def test_chat_uses_same_registry_via_request_factory_with_none_scope(self):
        search = Mock(return_value=package())
        supplied = self.registry(search)
        factory = Mock(return_value=supplied)
        client = FakeLLMClient([ModelTurn(tool_calls=[search_call()]), chat_final(refs=[
            {"tool_call_id": "search_1", "evidence_index": 1}])])
        execution = ChatExecutionV1(client, factory)
        result = execution(self.context, prepare_chat_input(self.context, "Synthetic question"))
        factory.assert_called_once_with(self.context)
        self.assertEqual(result.execution.status, "completed")
        self.assertIsInstance(result.output, ChatAnswerV1)
        self.assertEqual(result.sources[0].evidence_type, "guideline")
        self.assertIsNone(result.sources[0].pmid)
        self.assertEqual(supplied.mode, "production")
        self.assertEqual(len(supplied.definitions()), 1)

    def test_chat_factory_can_compose_shared_handler_with_another_allowed_tool(self):
        shared = self.registry(Mock(return_value=package())).get("search_evidence")
        profile = Mock(return_value={"symptoms": ["Synthetic stored signal"]})
        supplied = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.PATIENT_PROFILE], profile), shared))
        client = FakeLLMClient([ModelTurn(tool_calls=[call(), search_call()]), chat_final(refs=[
            {"tool_call_id": "search_1", "evidence_index": 0}])])
        result = ChatExecutionV1(client, lambda context: supplied)(
            CONTEXT, prepare_chat_input(CONTEXT, "Synthetic question"))
        self.assertEqual(result.execution.status, "completed")
        self.assertEqual(result.execution.total_tool_calls, 2)
        self.assertEqual(result.sources[0].pmid, "SYNTHETIC-PMID")
        profile.assert_called_once()
