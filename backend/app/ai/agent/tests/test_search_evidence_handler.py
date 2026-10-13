"""Thin Handler/production composition with the unchanged ToolExecutor."""
import inspect
import json
import threading
from unittest import TestCase
from unittest.mock import Mock

from ..adapters.rag_evidence import RagEvidenceRetriever
from ..composition import build_evidence_registry
from ..executor import ToolExecutor
from ..registry import TOOL_CONTRACTS, ToolName, production_registry
from ..tools.contracts import EvidencePackage, EvidenceSearchInput
from ..tools.evidence_tools import EvidenceRetriever
from ..tools.search_evidence import SearchEvidenceHandler
from .test_search_evidence_adapter import package
from .test_workflows import CONTEXT, call


class SearchEvidenceHandlerTests(TestCase):
    def test_handler_forwards_exact_validated_values_and_context_and_returns_same_result(self):
        result = EvidencePackage.model_validate(package().model_dump(mode="json"))
        retriever = Mock(spec=EvidenceRetriever)
        retriever.search_evidence.return_value = result
        args = EvidenceSearchInput(query=" \tSynthetic query\n", top_k=20)
        context = CONTEXT.model_copy(update={"patient_id": None})
        returned = SearchEvidenceHandler(retriever)(context, args)
        retriever.search_evidence.assert_called_once_with(query=args.query, top_k=20, context=context)
        self.assertIs(retriever.search_evidence.call_args.kwargs["context"], context)
        self.assertIs(returned, result)
        self.assertIs(inspect.signature(SearchEvidenceHandler.__init__).parameters["retriever"].annotation,
                      EvidenceRetriever)

    def test_production_factory_uses_only_canonical_search_without_test_mode(self):
        registry = build_evidence_registry()
        self.assertEqual(registry.mode, "production")
        self.assertEqual(registry.definitions(), [TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE].definition()])
        spec = registry.get("search_evidence")
        self.assertIs(spec.contract, TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE])
        self.assertFalse(spec.test_only)
        self.assertIsInstance(spec.handler, SearchEvidenceHandler)
        self.assertIsInstance(spec.handler.retriever, RagEvidenceRetriever)
        self.assertIsNone(spec.handler.retriever.search_fn)
        self.assertEqual(production_registry().definitions(), [])

    def test_executor_success_and_trace_with_no_patient_scope(self):
        search = Mock(return_value=package())
        registry = build_evidence_registry(retriever=RagEvidenceRetriever(search_fn=search))
        outcome, trace = ToolExecutor(registry).execute(call(ToolName.SEARCH_EVIDENCE,
            {"query": "synthetic", "top_k": 1}), CONTEXT.model_copy(update={"patient_id": None}))
        self.assertTrue(outcome.success)
        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.data, package().model_dump(mode="json"))
        self.assertEqual((outcome.call_id, outcome.tool_name), ("call_1", "search_evidence"))
        self.assertEqual((trace.success, trace.result_count, trace.error_type), (True, 2, None))
        search.assert_called_once_with(query="synthetic", k=1)

    def test_invalid_input_is_rejected_before_retriever(self):
        retriever = Mock(spec=EvidenceRetriever)
        executor = ToolExecutor(build_evidence_registry(retriever=retriever))
        invalid = ["{broken", {"query": " \t\n"}, {"query": "x" * 2001}, {},
                   *({"query": "synthetic", "top_k": value} for value in (0, 21, True, "5", 5.0)),
                   {"query": "synthetic", "patient_id": "PRIVATE"},
                   {"query": "synthetic", "exclude_animal": False}]
        for args in invalid:
            with self.subTest(args=args):
                outcome, _ = executor.execute(call(ToolName.SEARCH_EVIDENCE, args), CONTEXT)
                self.assertFalse(outcome.success)
                self.assertEqual(outcome.error.code, "invalid_arguments")
        retriever.search_evidence.assert_not_called()

    def test_rag_error_uses_existing_tool_exception_and_metadata_only_logs(self):
        search = Mock(side_effect=RuntimeError("PRIVATE query/evidence/DB detail"))
        executor = ToolExecutor(build_evidence_registry(retriever=RagEvidenceRetriever(search_fn=search)))
        with self.assertLogs("app.ai.agent.executor", level="INFO") as captured:
            outcome, trace = executor.execute(call(ToolName.SEARCH_EVIDENCE,
                {"query": "PRIVATE query"}), CONTEXT)
        self.assertFalse(outcome.success)
        self.assertIsNone(outcome.data)
        self.assertEqual(outcome.error.code, "tool_exception")
        self.assertEqual(trace.error_type, "tool_exception")
        self.assertNotIn("PRIVATE", json.dumps(outcome.model_dump(mode="json")))
        self.assertNotIn("PRIVATE", "\n".join(captured.output))
        search.assert_called_once_with(query="PRIVATE query", k=5)

    def test_adapter_conversion_failure_uses_existing_handler_exception_policy(self):
        raw = package()
        raw.evidence[0].relevance_score = 2.0
        executor = ToolExecutor(build_evidence_registry(retriever=RagEvidenceRetriever(search_fn=Mock(return_value=raw))))
        outcome, _ = executor.execute(call(ToolName.SEARCH_EVIDENCE, {"query": "synthetic"}), CONTEXT)
        self.assertFalse(outcome.success)
        self.assertIsNone(outcome.data)
        self.assertEqual(outcome.error.code, "tool_exception")

    def test_executor_output_validation_rejects_invalid_retriever_return(self):
        for raw in ({"query": "synthetic", "evidence": "PRIVATE invalid"},
                    EvidencePackage.model_construct(query="synthetic", evidence="PRIVATE invalid")):
            retriever = Mock(spec=EvidenceRetriever)
            retriever.search_evidence.return_value = raw
            outcome, _ = ToolExecutor(build_evidence_registry(retriever=retriever)).execute(
                call(ToolName.SEARCH_EVIDENCE, {"query": "synthetic"}), CONTEXT)
            self.assertFalse(outcome.success)
            self.assertIsNone(outcome.data)
            self.assertEqual(outcome.error.code, "invalid_output")

    def test_explicit_rag_timeout_keeps_executor_timeout_classification(self):
        search = Mock(side_effect=TimeoutError("PRIVATE timeout"))
        outcome, trace = ToolExecutor(build_evidence_registry(
            retriever=RagEvidenceRetriever(search_fn=search))).execute(
                call(ToolName.SEARCH_EVIDENCE, {"query": "synthetic"}), CONTEXT)
        self.assertEqual(outcome.error.code, "tool_timeout")
        self.assertEqual(trace.error_type, "tool_timeout")
        self.assertIsNone(outcome.data)
        search.assert_called_once()

    def test_blocking_retrieval_uses_existing_executor_wait_timeout(self):
        entered, release = threading.Event(), threading.Event()
        def slow_search(**kwargs):
            entered.set()
            release.wait(2)
            return package()
        search = Mock(side_effect=slow_search)
        try:
            outcome, _ = ToolExecutor(build_evidence_registry(
                retriever=RagEvidenceRetriever(search_fn=search)), timeout_seconds=0.05).execute(
                    call(ToolName.SEARCH_EVIDENCE, {"query": "synthetic"}), CONTEXT)
            self.assertTrue(entered.is_set())
            self.assertEqual(outcome.error.code, "tool_timeout")
            self.assertIsNone(outcome.data)
            search.assert_called_once()
        finally:
            release.set()
