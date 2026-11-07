"""Synthetic policy/wiring regressions, NOT an evaluation of real LLM quality.

The final judgments below are expert-scripted. Assertions establish policy
visibility, preserved reasons/categories/provenance and count boundaries only.
"""
import json
import unittest
from uuid import UUID
from unittest.mock import patch

from ..feed_candidates import build_feed_decision_policy
from ..outputs import FeedAnswerV2, StructuredAgentResult, validate_final_output
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..schemas import AgentResult, ModelTurn, ToolCall
from ..testing.fake_llm import FakeLLMClient
from ..tools.contracts import EvidenceItem, EvidencePackage
from ..tools.search_evidence import SEARCH_EVIDENCE_DATA_POLICY
from ..workflows.feed_execution import FeedExecutionV1
from ..workflows.feed_prompts import FeedPromptBuilder
from .test_workflows import CONTEXT


def paper(pmid, title, study='rct'):
    return EvidenceItem(pmid=pmid, title=title, study_type=study, abstract=title,
                        evidence_type='new_research', relevance_score=0.7)


def choose(reason, category='care'):
    return {'include': True, 'personal_reason': reason, 'category': category}


class FeedQualityPolicyTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'psycopg.connect', 'app.ai.retrieval.embedding.get_model'):
            guard = patch(target, side_effect=AssertionError('Live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def scripted(self, rows, judgments, *, query='sleep disturbances in people with dementia', context=None, patients=None):
        prepared = json.dumps({'feed_scope':'caregiver_all', 'patients': patients or []}, ensure_ascii=False)
        client = FakeLLMClient([
            ModelTurn(tool_calls=[ToolCall(id='original_search',name='search_evidence',arguments={'query':query,'top_k':5})]),
            ModelTurn(text=json.dumps({'decisions':judgments},ensure_ascii=False))])
        searches=[]
        def handler(context,args):
            searches.append((args.query,args.top_k))
            return EvidencePackage(query=args.query,evidence=rows)
        execution = FeedExecutionV1(client,ToolRegistry((ToolSpec(
            TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE],handler,parallel_safe=True),)))
        result=execution(context or CONTEXT,prepared)
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(len(client.requests),2)
        self.assertEqual(searches,[(query,5)])
        self.assertEqual([s.source_ref for s in result.sources],[i.source_ref for i in result.output.items])
        projection=json.loads(next(m.content for m in client.requests[1][0] if m.kind=='evidence'))
        self.assertLessEqual(projection['candidate_count'],15)
        self.assertLessEqual(len(result.output.items),5)
        self.assertEqual(next(m.content for m in client.requests[0][0] if m.role=='user'),prepared)
        return result,client,projection

    def test_policy_separates_patient_symptoms_and_caregiver_topics(self):
        initial=FeedPromptBuilder().build_system_prompt(CONTEXT)
        final=build_feed_decision_policy().content
        for rule in ('환자 중심 query','환자의 수면을 보호자 수면으로 바꾸지 않는다',
                     'caregiver/family를 불필요하게 섞지 않는다','별도 독립 주제','미충족 환자 증상',
                     '모든 증상마다 논문 1편을 강제하지 않는다','사용자마다 실제로 다른 신호','caregiver_all'):
            self.assertIn(rule,initial)
        self.assertIn('연구 대상·목적·실제 결과와 Context의 연결',final)
        self.assertIn('환자에 관한 연구와 보호자에 관한 연구를 혼동하지 않는다', SEARCH_EVIDENCE_DATA_POLICY)
        self.assertIn('표현만 감춰 관련성 부족을 숨기지 않는다',final)
        for phrase in ('기록되지는 않았지만','직접 일치하는 내용은 제한적','구체적인 관련성은 없지만'):
            self.assertIn(phrase,final)
        self.assertIn('include=false',final)

    def test_scripted_patient_sleep_rejects_caregiver_sleep_and_keeps_patient_source(self):
        reason='밤에 잠을 잘 못 자는 환자를 돕는 방법을 살펴본 연구예요.'
        result,client,_=self.scripted([
            paper('caregiver','Family caregiver insomnia and sleep quality'),
            paper('patient','Sleep disturbances in people with dementia')],
            [{'include':False},choose(reason,'treatment')],patients=[{'patient_ref':'p1','symptoms':['수면 문제']}])
        self.assertEqual([s.pmid for s in result.sources],['patient'])
        self.assertEqual(result.output.items[0].source_ref.evidence_index,1)
        self.assertEqual(result.output.items[0].personal_reason,reason)
        final_policy=next(m.content for m in client.requests[1][0] if m.role=='system' and 'FeedDecisionDraft' in (m.content or ''))
        self.assertIn('MCI 환자의 낮잠 증가만으로 보호자의 불면증 치료 연구를 환자 수면 근거로 추천하지 않는다',final_policy)
        self.assertEqual(sum(m.content == SEARCH_EVIDENCE_DATA_POLICY for m in client.requests[1][0]), 1)

    def test_scripted_wandering_and_falls_have_priority_and_original_round_robin_sources(self):
        queries=['wandering behavior management in dementia','fall prevention in people with dementia']
        client=FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id=f'search_{i}',name='search_evidence',arguments={'query':q}) for i,q in enumerate(queries)]),
            ModelTurn(text=json.dumps({'decisions':[choose('길을 잃고 돌아다니는 환자를 돕는 방법을 살펴봤어요.'),
                                                   choose('환자가 넘어지는 일을 줄이는 방법을 살펴봤어요.','prevention')]}))])
        searched=[]
        def handler(context,args):
            searched.append(args.query)
            return EvidencePackage(query=args.query,evidence=[paper(args.query,args.query)])
        result=FeedExecutionV1(client,ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE],handler,parallel_safe=True),)))(
            CONTEXT,'{"feed_scope":"caregiver_all","patients":[{"patient_ref":"p1","symptoms":["배회"],"safety_events":["낙상"]}]}')
        self.assertEqual(result.execution.status,'completed')
        self.assertCountEqual(searched,queries)
        self.assertTrue(all('caregiver' not in q and 'family' not in q for q in searched))
        self.assertEqual([s.source_ref.tool_call_id for s in result.sources],['search_0','search_1'])
        self.assertEqual([i.category for i in result.output.items],['care','prevention'])
        self.assertEqual(len(client.requests),2)

    def test_scripted_categories_follow_paper_purpose_and_easy_korean_is_preserved(self):
        reasons=['약을 쓰지 않는 방법으로 증상을 줄일 수 있는지 살펴봤어요.',
                 '환자가 넘어지는 일을 예방하는 방법을 살펴봤어요.',
                 '기억력과 판단력을 확인하는 검사 방법을 비교했어요.',
                 '가족이 함께 돌보는 데 도움을 주는 방법을 살펴봤어요.']
        categories=['treatment','prevention','diagnosis','care']
        result,_,_=self.scripted([paper(str(i),title) for i,title in enumerate([
            'Nonpharmacological treatment trial','Fall prevention trial','Screening test accuracy','Family care support'])],
            [choose(reason,cat) for reason,cat in zip(reasons,categories)])
        self.assertEqual([i.category for i in result.output.items],categories)
        self.assertEqual([i.personal_reason for i in result.output.items],reasons)
        policy=build_feed_decision_policy().content
        for term in ('짧은 문장','영어·약어','약을 쓰지 않는','도움을 주는 방법','기존 연구를 모아 정리한 글',
                     '여러 분야의 전문가가 함께하는','넘어짐','연구 방식·대상·결과·한계를 바꾸지 않고'):
            self.assertIn(term,policy)
        self.assertIn('환자의 등록 증상이 아니라 논문의 주된 목적',policy)

    def test_other_is_not_automatically_opinion_and_protocol_is_not_completed_evidence(self):
        result,_,projection=self.scripted([
            paper('unknown','Patient sleep management outcomes','other'),
            paper('protocol','Protocol for a sleep intervention trial','protocol'),
            paper('trial','Completed patient sleep intervention trial','rct')],
            [choose('환자의 수면을 돕는 방법을 비교한 연구예요.','treatment'),
             choose('환자의 수면 문제를 관리하는 방법을 다뤘어요.')])
        self.assertEqual([s.pmid for s in result.sources],['trial','unknown'])
        self.assertEqual(projection['candidate_count'],2)
        self.assertNotIn('Protocol for a sleep intervention trial',str(projection['candidates']))
        self.assertNotIn('evidence_level',EvidenceItem.model_fields)
        self.assertTrue(all(set(row)=={'status','title','study_type','derived_evidence_grade','evidence_excerpt'} for row in projection['candidates']))
        policy=build_feed_decision_policy().content
        for term in ('미분류 후보를 유형만으로 자동 제외하지 않는다','프로토콜을 완료된 효과 검증 연구보다 우선하지 않는다','실제 DB evidence_level/화면 표시 등급이 아니며'):
            self.assertIn(term,policy)
        for term in ('other 또는 null', '연구 가치가 없거나 효과가 없다는 의미가 아니다',
                     'Protocol은 연구 계획이며 완료된 효과 검증 결과가 아니다'):
            self.assertIn(term, SEARCH_EVIDENCE_DATA_POLICY)

    def test_counts_distinguish_raw_merged_and_selected_without_filling_to_five(self):
        result,_,projection=self.scripted([
            paper('a','Patient sleep management'),paper('a','Duplicate'),paper(None,'No PMID'),
            paper('b','Caregiver sleep'),paper(None,'No identity')],
            [choose('환자의 수면을 돕는 방법을 살펴봤어요.'),{'include':False}])
        self.assertEqual(projection['last_search']['returned_evidence_count'],5)
        self.assertEqual(projection['candidate_count'],2)
        self.assertEqual(len(result.output.items),1)
        small,_,projection=self.scripted([paper('one','Patient sleep')],[choose('환자의 수면을 다뤘어요.')])
        self.assertEqual(projection['last_search']['returned_evidence_count'],1)
        self.assertEqual(len(small.output.items),1)
        empty,_,_=self.scripted([paper('unrelated','Caregiver employment opinions')],[{'include':False}])
        self.assertEqual(empty.output.items,[])

    def test_cold_start_and_shared_useful_paper_do_not_force_zero_or_global_dedup(self):
        rows=[paper('shared','General dementia exercise research')]
        for user in (1,2):
            result,_,_=self.scripted(rows,[choose('치매 환자의 운동 방법을 알아보는 데 참고할 연구예요.','treatment')],
                context=CONTEXT.model_copy(update={'user_id':UUID(int=user),'patient_id':None}))
            self.assertEqual([s.pmid for s in result.sources],['shared'])
            self.assertEqual(len(result.output.items),1)

    def test_actual_handoff_can_further_exclude_unmapped_or_missing_pmids(self):
        from app.api.recommendations import backend_handoff
        from app.schemas import PaperDetailResponse
        package=EvidencePackage(query='synthetic',evidence=[paper('mapped','A'),paper('unmapped','B'),paper(None,'C')])
        output,sources=validate_final_output(json.dumps({'schema_version':'2','response_type':'feed','items':[
            {'source_ref':{'tool_call_id':'source','evidence_index':i},'personal_reason':'합성 연구를 확인하는 테스트예요.', 'category':'care'}
            for i in range(3)]}),FeedAnswerV2,{'source':package})
        result=StructuredAgentResult(execution=AgentResult(status='completed',request_id=CONTEXT.request_id,
            tool_rounds=1,total_tool_calls=1,called_tools=[],errors=[]),output=output,sources=sources)
        with patch('app.api.recommendations.functions.get_paper_details_by_pmids',return_value={
                'mapped':PaperDetailResponse(id=UUID(int=3),title='DB title',evidence_level='2')}) as read:
            handoff=backend_handoff(result)
        read.assert_called_once_with(['mapped','unmapped'])
        self.assertEqual(len(result.output.items),3)
        self.assertEqual(len(handoff),1)
        self.assertEqual(handoff[0].title,'DB title')
