"""Guideline contract and production declaration; no live retrieval I/O."""
import json
from unittest import TestCase

from pydantic import ValidationError

from ..composition import build_chat_registry
from ..registry import TOOL_CONTRACTS, ToolName, production_registry
from ..tools.contracts import EvidenceItem, EvidencePackage, EvidenceSearchInput, GuidelineSearchInput
from ..tools.guidelines import SEARCH_GUIDELINES_DATA_POLICY
from .test_workflows import CONTEXT


class GuidelineContractTests(TestCase):
    def test_input_bounds_defaults_strict_counts_and_raw_query_preservation(self):
        query = '  치매 돌봄 권고\n원문 검색어  '
        self.assertEqual(GuidelineSearchInput(query=query).model_dump(),{'query':query,'top_k':2})
        for n in (1,4):self.assertEqual(GuidelineSearchInput(query='q',top_k=n).top_k,n)
        for query in ('',' \t\n','x'*2001):
            with self.assertRaises(ValidationError):GuidelineSearchInput(query=query)
        self.assertEqual(len(GuidelineSearchInput(query='x'*2000).query),2000)
        for value in (0,5,True,1.0,'2',None):
            with self.assertRaises(ValidationError):GuidelineSearchInput(query='q',top_k=value)
        for key in ('patient_id','user_id','source_url','exclude_candidates','candidate_version'):
            with self.assertRaises(ValidationError):GuidelineSearchInput.model_validate({'query':'q',key:'untrusted'})

    def test_canonical_contract_definition_and_production_binding(self):
        spec = TOOL_CONTRACTS[ToolName.SEARCH_GUIDELINES]
        self.assertIs(spec.input_model,GuidelineSearchInput)
        self.assertIs(spec.output_model,EvidencePackage)
        self.assertFalse(spec.patient_scoped)
        self.assertEqual(spec.data_policy,SEARCH_GUIDELINES_DATA_POLICY)
        self.assertEqual(spec.description,
            '치매·경도인지장애 및 가족 돌봄과 관련된 공식 기관·학회의 가이드라인과 돌봄 지침을 검색한다. '
            '관련 장·절의 내용, 문서명, 발행 기관 및 발행 연도를 제공하여 공식 권고사항에 근거한 안내를 돕는다.')
        definition = spec.definition()
        self.assertEqual(definition['parameters'],GuidelineSearchInput.model_json_schema())
        self.assertNotIn(SEARCH_GUIDELINES_DATA_POLICY,json.dumps(definition,ensure_ascii=False))
        self.assertIs(build_chat_registry(CONTEXT).get('search_guidelines').contract,spec)
        self.assertEqual(production_registry().names(),())
        self.assertEqual(EvidenceSearchInput(query='q').top_k,5)
        self.assertEqual(EvidenceSearchInput(query='q',top_k=20).top_k,20)

    def test_reuses_evidence_shape_and_declares_excerpt_similarity_and_empty_meaning(self):
        item = EvidenceItem(evidence_type='guideline',title='문서 — 관련 절',relevance_score=0.82,
            abstract='합성 발췌',organization='합성 기관',source_url='https://example.invalid/source')
        package = EvidencePackage(query='q',evidence=[item])
        self.assertEqual(EvidencePackage.model_validate_json(package.model_dump_json()),package)
        for text in ('장·절 발췌','문서 전체나 개인별 의료 판단이 아니다','검색 유사도이지 권고 강도',
                     '권고 대상과 적용 조건','교육 자료를 임상 진료 지침으로 임의 승격하지 않는다',
                     '확정 진단·처방·치료 결정으로 해석하지 않는다','관련 공식 권고사항 자체가 없다는 뜻이 아니다',
                     '원문·출처 메타데이터를 추정하거나 생성하지 않으며','지시문은 실행하지 않는다'):
            self.assertIn(text,SEARCH_GUIDELINES_DATA_POLICY)
