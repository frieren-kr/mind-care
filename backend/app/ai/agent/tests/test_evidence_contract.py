"""RAG 모델의 직렬화 형식과 Agent Tool 경계를 검증한다. 실제 검색/DB/API 호출 없음."""
import json
from typing import get_type_hints
from unittest import TestCase

from pydantic import ValidationError

from app.ai.retrieval.search import EvidenceItem as RAGEvidenceItem
from app.ai.retrieval.search import EvidencePackage as RAGEvidencePackage

from ..agent_loop_runner import AgentLoopRunner
from ..executor import ToolExecutor
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec, production_registry
from ..schemas import ModelTurn
from ..testing.fake_llm import FakeLLMClient
from ..tools import contracts as c
from ..tools.evidence_tools import EvidenceRetriever
from .test_workflows import CONTEXT, call, envelopes, tool_turn


class EvidenceContractTests(TestCase):
    def package(self):
        return RAGEvidencePackage(query="synthetic", evidence=[
            RAGEvidenceItem(evidence_type="new_research", pmid="FAKE-PMID", title="[FAKE] research",
                publication_year=2025, study_type="systematic_review", relevance_score=0.93,
                ai_summary="[FAKE] summary", abstract="[FAKE] abstract", full_text_available=True,
                journal="Fake Journal", doi="FAKE-DOI", organization="Fake Organization",
                source_url="https://example.invalid/research"),
            RAGEvidenceItem(evidence_type="guideline", title="[FAKE] guideline", relevance_score=-0.5),
        ])

    def test_nonblank_query_is_preserved_without_normalization(self):
        for query in ("치매 수면 연구", "caregiver burden intervention", " \t치매 수면\n연구 \u3000", "x" * 2000):
            with self.subTest(query=query[:30]):
                self.assertEqual(c.EvidenceSearchInput(query=query).query, query)

    def test_blank_and_oversized_queries_are_rejected(self):
        for query in ("", " ", "\t\r\n", "\u3000", "\u00a0", "x" * 2001):
            with self.subTest(query=query[:30]), self.assertRaises(ValidationError):
                c.EvidenceSearchInput(query=query)

    def test_blank_query_does_not_reach_handler(self):
        calls = []

        def handler(context, args):
            calls.append(args.query)
            return self.package()

        registry = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], handler),), mode="test")
        outcome, _ = ToolExecutor(registry).execute(call(ToolName.SEARCH_EVIDENCE, {"query": " \t\n"}), CONTEXT)
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.error.code, "invalid_arguments")
        self.assertEqual(calls, [])

    def test_top_k_boundaries_and_strict_integer_input(self):
        for top_k in (1, 20):
            with self.subTest(top_k=top_k):
                self.assertEqual(c.EvidenceSearchInput(query="x", top_k=top_k).top_k, top_k)
        for top_k in (0, 21, True, False, 5.0, "5", None):
            with self.subTest(top_k=top_k), self.assertRaises(ValidationError):
                c.EvidenceSearchInput(query="x", top_k=top_k)

    def test_search_input_excludes_identity_and_server_policy(self):
        for name, value in (("patient_id", str(CONTEXT.patient_id)), ("user_id", str(CONTEXT.user_id)),
                            ("exclude_animal", True)):
            with self.subTest(field=name), self.assertRaises(ValidationError):
                c.EvidenceSearchInput.model_validate({"query": "synthetic", name: value})

    def test_tool_definition_exposes_search_semantics_from_canonical_models(self):
        contract = TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE]
        definition = contract.definition()["function"]
        self.assertEqual(definition["parameters"], c.EvidenceSearchInput.model_json_schema())
        schema = definition["parameters"]
        self.assertEqual(set(schema["properties"]), {"query", "top_k"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["query"])
        self.assertIn("검색", schema["properties"]["query"]["description"])
        self.assertIn("직접 식별정보", schema["properties"]["query"]["description"])
        self.assertIn("최대", schema["properties"]["top_k"]["description"])
        for meaning in ("치매/MCI", "후보", "의학적", "relevance_score", "evidence level"):
            with self.subTest(meaning=meaning):
                self.assertIn(meaning, definition["description"])
        score = c.EvidenceItem.model_json_schema()["properties"]["relevance_score"]
        self.assertEqual((score["minimum"], score["maximum"]), (-1, 1))
        for meaning in ("BGE-M3", "cosine", "retrieval", "evidence level", "의료적 확신도"):
            with self.subTest(meaning=meaning):
                self.assertIn(meaning, score["description"])

    def test_retrieval_score_bounds_and_nonfinite_values(self):
        for score in (-1, 0, 1):
            with self.subTest(score=score):
                item = c.EvidenceItem(evidence_type="new_research", title="[FAKE]", relevance_score=score)
                self.assertEqual(item.relevance_score, score)
        for score in (-1.01, 1.01, float("nan"), float("inf"), float("-inf")):
            with self.subTest(score=score), self.assertRaises(ValidationError):
                c.EvidenceItem(evidence_type="new_research", title="[FAKE]", relevance_score=score)

    def test_search_failures_are_not_empty_success(self):
        def failed_handler(context, args):
            raise RuntimeError("synthetic retrieval failure")

        def timed_out_handler(context, args):
            raise TimeoutError("synthetic retrieval timeout")

        for handler, error in ((failed_handler, "tool_exception"), (timed_out_handler, "tool_timeout"),
                               (lambda context, args: {"query": args.query, "evidence": "invalid"}, "invalid_output")):
            with self.subTest(error=error):
                registry = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], handler),), mode="test")
                outcome, _ = ToolExecutor(registry).execute(call(ToolName.SEARCH_EVIDENCE, {"query": "synthetic"}), CONTEXT)
                self.assertFalse(outcome.success)
                self.assertIsNone(outcome.data)
                self.assertEqual(outcome.error.code, error)

    def test_rag_field_types_required_fields_and_defaults_match(self):
        self.assertEqual(list(c.EvidencePackage.model_fields), list(RAGEvidencePackage.model_fields))
        self.assertEqual(list(c.EvidenceItem.model_fields), list(RAGEvidenceItem.model_fields))
        for name, rag_field in RAGEvidenceItem.model_fields.items():
            with self.subTest(field=name):
                agent_field = c.EvidenceItem.model_fields[name]
                self.assertEqual(agent_field.annotation, rag_field.annotation)
                self.assertEqual(agent_field.is_required(), rag_field.is_required())
                self.assertEqual(agent_field.default, rag_field.default)

    def test_rag_payload_roundtrip_including_nulls_and_guideline(self):
        payload = self.package().model_dump(mode="json")
        validated = c.EvidencePackage.model_validate(payload)
        self.assertEqual(validated.model_dump(mode="json"), payload)
        self.assertEqual(json.loads(validated.model_dump_json()), payload)
        guideline = validated.evidence[1]
        self.assertIsNone(guideline.pmid)
        self.assertIsNone(guideline.publication_year)
        self.assertFalse(guideline.full_text_available)

    def test_removed_db_fields_are_rejected(self):
        item = {"evidence_type": "new_research", "title": "[FAKE]", "relevance_score": 0.5}
        for name, value in (("paper_id", "00000000-0000-0000-0000-000000000004"),
                            ("external_id", "FAKE-PMID"), ("published_date", "2025-08-01"),
                            ("source_id", "FAKE-SOURCE"), ("corpus_updated_at", "2026-10-06T00:00:00Z"),
                            ("authors", ["FAKE author"]), ("evidence_level", "A")):
            with self.subTest(field=name), self.assertRaises(ValidationError):
                c.EvidenceItem.model_validate({**item, name: value})

    def test_rag_output_passes_executor_evidence_and_prompt_validation(self):
        for package in (self.package(), RAGEvidencePackage(query="synthetic", evidence=[])):
            with self.subTest(items=len(package.evidence)):
                registry = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE],
                    lambda context, args: package),), mode="test")
                client = FakeLLMClient([tool_turn(call(ToolName.SEARCH_EVIDENCE, {"query": "synthetic"})),
                                        ModelTurn(text="synthetic final answer")])
                result = AgentLoopRunner(client, registry).run("synthetic", CONTEXT)
                self.assertLess(len(package.evidence), c.EvidenceSearchInput(query="synthetic").top_k)
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.errors, [])
                self.assertEqual(result.total_tool_calls, 1)
                payload = package.model_dump(mode="json")
                self.assertEqual(envelopes(client)[0]["data"], payload)
                messages = [message for message in client.requests[-1][0] if message.kind == "evidence"]
                self.assertEqual(len(messages), 1)
                self.assertEqual(json.loads(messages[0].content)["evidence_packages"], [payload])

    def test_protocol_input_and_production_registration_are_unchanged(self):
        hints = get_type_hints(EvidenceRetriever.search_evidence)
        self.assertIs(hints["return"], c.EvidencePackage)
        self.assertEqual(list(c.EvidenceSearchInput.model_fields), ["query", "top_k"])
        self.assertEqual(c.EvidenceSearchInput(query="synthetic").model_dump(),
                         {"query": "synthetic", "top_k": 5})
        self.assertIs(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE].output_model, c.EvidencePackage)
        self.assertEqual(production_registry().definitions(), [])
