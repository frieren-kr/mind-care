"""RAG-to-Agent boundary checks with no live embedding, DB or Provider."""
from copy import deepcopy
from datetime import date
import inspect
import json
from types import SimpleNamespace
import warnings
from unittest import TestCase
from unittest.mock import Mock, patch

from pydantic import ValidationError

from app.ai.retrieval import search as rag
from ..adapters.rag_evidence import RagEvidenceRetriever
from ..tools.contracts import EvidencePackage
from ..tools.evidence_tools import EvidenceRetriever
from .test_workflows import CONTEXT


def package():
    return rag.EvidencePackage(query="RAG returned query", evidence=[
        rag.EvidenceItem(evidence_type="new_research", pmid="SYNTHETIC-PMID",
            title="Synthetic research", publication_year=2025, study_type="guideline",
            relevance_score=-0.4, ai_summary="Synthetic summary", abstract="Synthetic abstract",
            full_text_available=True, journal="Synthetic journal", doi="SYNTHETIC-DOI",
            organization="Synthetic organization", source_url="https://example.invalid/paper"),
        rag.EvidenceItem(evidence_type="guideline", title="Synthetic guideline", relevance_score=0.6),
    ])


class SearchEvidenceAdapterTests(TestCase):
    def test_protocol_signature_and_agent_return_type(self):
        self.assertEqual(inspect.signature(RagEvidenceRetriever.search_evidence),
                         inspect.signature(EvidenceRetriever.search_evidence))
        result = RagEvidenceRetriever(search_fn=Mock(return_value=package())).search_evidence(
            query="synthetic", top_k=5, context=CONTEXT)
        self.assertIs(type(result), EvidencePackage)

    def test_query_and_k_are_forwarded_without_identity_or_filter_overrides(self):
        query = " \t치매 돌봄 연구\n "
        for top_k in (1, 5, 20):
            with self.subTest(top_k=top_k):
                search = Mock(return_value=package())
                result = RagEvidenceRetriever(search_fn=search).search_evidence(
                    query=query, top_k=top_k, context=CONTEXT)
                search.assert_called_once_with(query=query, k=top_k)
                self.assertEqual(result.query, "RAG returned query")

    def test_all_fields_nulls_negative_scores_order_duplicates_and_source_are_preserved(self):
        raw = package()
        raw.evidence.append(deepcopy(raw.evidence[0]))
        before = raw.model_dump(mode="json")
        result = RagEvidenceRetriever(search_fn=Mock(return_value=raw)).search_evidence(
            query="synthetic", top_k=1, context=CONTEXT)
        # No re-ranking, dedup or additional cap even when the upstream returns
        # more rows: top_k semantics belong to RAG, not a new Adapter policy.
        self.assertEqual(result.model_dump(mode="json"), before)
        self.assertEqual(raw.model_dump(mode="json"), before)
        self.assertEqual(result.evidence[0].relevance_score, -0.4)
        self.assertEqual(result.evidence[0].evidence_type, "new_research")
        self.assertEqual(result.evidence[0].study_type, "guideline")
        self.assertIsNone(result.evidence[1].pmid)
        self.assertIsNone(result.evidence[1].source_url)
        self.assertFalse(result.evidence[1].full_text_available)
        result.evidence[0].title = "Changed Agent copy"
        self.assertEqual(raw.model_dump(mode="json"), before)

    def test_successful_empty_result_and_none_patient_scope(self):
        raw = rag.EvidencePackage(query="no eligible hits", evidence=[])
        for patient_id in (None, CONTEXT.patient_id):
            search = Mock(return_value=raw)
            result = RagEvidenceRetriever(search_fn=search).search_evidence(query="synthetic", top_k=5,
                context=CONTEXT.model_copy(update={"patient_id": patient_id}))
            self.assertEqual(result.model_dump(), {"query": raw.query, "evidence": []})
            search.assert_called_once_with(query="synthetic", k=5)

    def test_search_errors_propagate_same_instance_without_retry_or_empty_fallback(self):
        for error in (RuntimeError("PRIVATE DB error"), TimeoutError("PRIVATE timeout")):
            with self.subTest(error=type(error).__name__):
                search = Mock(side_effect=error)
                with self.assertRaises(type(error)) as caught:
                    RagEvidenceRetriever(search_fn=search).search_evidence(query="synthetic", top_k=5,
                        context=CONTEXT)
                self.assertIs(caught.exception, error)
                search.assert_called_once_with(query="synthetic", k=5)

    def test_agent_contract_rejects_invalid_scores_without_clamping(self):
        for score in (-1.01, 1.01, float("nan"), float("inf"), float("-inf")):
            with self.subTest(score=score):
                raw = package()
                raw.evidence[0].relevance_score = score
                before = raw.evidence[0].relevance_score
                with self.assertRaises(ValidationError):
                    RagEvidenceRetriever(search_fn=Mock(return_value=raw)).search_evidence(
                        query="synthetic", top_k=5, context=CONTEXT)
                if score == score:
                    self.assertEqual(raw.evidence[0].relevance_score, before)

    def test_unknown_or_missing_fields_and_wrong_types_are_rejected(self):
        payload = package().model_dump(mode="json")
        missing = deepcopy(payload)
        del missing["evidence"][0]["title"]
        bad_rows = deepcopy(payload)
        bad_rows["evidence"] = "PRIVATE malformed rows"
        bad_type = deepcopy(payload)
        bad_type["evidence"][0]["evidence_type"] = "other"
        extra = deepcopy(payload)
        extra["evidence"][0]["paper_id"] = "PRIVATE ID"
        for invalid in (missing, bad_rows, bad_type, extra):
            with self.subTest(payload=invalid):
                raw = Mock(model_dump=Mock(return_value=invalid))
                with self.assertRaises(ValidationError):
                    RagEvidenceRetriever(search_fn=Mock(return_value=raw)).search_evidence(
                        query="synthetic", top_k=5, context=CONTEXT)

    def test_mutated_output_is_rejected_without_raw_serialization_warnings(self):
        raw = package()
        raw.evidence[0].title = ["PRIVATE source text"]
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            with self.assertRaises(ValidationError):
                RagEvidenceRetriever(search_fn=Mock(return_value=raw)).search_evidence(
                    query="synthetic", top_k=5, context=CONTEXT)
        self.assertEqual(captured, [])

    def test_serialization_failure_is_propagated_without_empty_fallback(self):
        error = ValueError("PRIVATE serializer detail")
        raw = Mock(model_dump=Mock(side_effect=error))
        search = Mock(return_value=raw)
        with self.assertRaises(ValueError) as caught:
            RagEvidenceRetriever(search_fn=search).search_evidence(query="synthetic", top_k=5, context=CONTEXT)
        self.assertIs(caught.exception, error)
        raw.model_dump.assert_called_once_with(mode="json", warnings=False)
        search.assert_called_once()

    def test_default_adapter_resolves_real_rag_entry_point_at_call_time(self):
        source = RagEvidenceRetriever()
        with patch.object(rag, "search", return_value=package()) as search:
            result = source.search_evidence(query="synthetic", top_k=20, context=CONTEXT)
        search.assert_called_once_with(query="synthetic", k=20)
        self.assertEqual(result.model_dump(mode="json"), package().model_dump(mode="json"))

    def test_real_rag_strip_and_empty_result_with_mocked_embedding_and_db(self):
        with patch.object(rag, "embed_query", return_value=[0.0]) as embed, \
             patch.object(rag, "search_similar", return_value=[]) as search_db, \
             patch.object(rag, "get_papers_by_ids") as papers:
            result = RagEvidenceRetriever().search_evidence(query=" \t치매 돌봄 연구\n ", top_k=7,
                context=CONTEXT.model_copy(update={"patient_id": None}))
        self.assertEqual(result.query, "치매 돌봄 연구")
        self.assertEqual(result.evidence, [])
        embed.assert_called_once_with("치매 돌봄 연구")
        search_db.assert_called_once_with([0.0], top_k=7 * rag.CANDIDATE_MULTIPLIER,
                                         model_name=rag.MODEL_NAME)
        papers.assert_not_called()

    def test_real_rag_metadata_policy_is_preserved_with_mocked_db_rows(self):
        paper = SimpleNamespace(paper_id=CONTEXT.patient_id, external_id="SYNTHETIC-EXTERNAL-ID",
            title="Dementia caregiver guideline", abstract=None, published_date=date(2025, 1, 1),
            publication_types=["Guideline"], mesh_terms=["Dementia"], journal=None, doi=None)
        hit = SimpleNamespace(paper_id=paper.paper_id, score=-0.4)
        with patch.object(rag, "embed_query", return_value=[0.0]), \
             patch.object(rag, "search_similar", return_value=[hit]), \
             patch.object(rag, "get_papers_by_ids", return_value=[paper]):
            result = RagEvidenceRetriever().search_evidence(query="synthetic", top_k=5, context=CONTEXT)
        item = result.evidence[0]
        self.assertEqual(item.pmid, paper.external_id)
        self.assertEqual(item.source_url, rag.PUBMED_URL.format(pmid=paper.external_id))
        self.assertEqual(item.study_type, "guideline")
        self.assertEqual(item.evidence_type, "new_research")
        self.assertEqual(item.relevance_score, -0.4)
        self.assertIsNone(item.doi)

    def test_injected_search_does_not_resolve_production_search(self):
        with patch.object(rag, "search", side_effect=AssertionError("Live RAG must not run")):
            result = RagEvidenceRetriever(search_fn=Mock(return_value=package())).search_evidence(
                query="synthetic", top_k=5, context=CONTEXT)
        self.assertEqual(json.loads(result.model_dump_json()), package().model_dump(mode="json"))
