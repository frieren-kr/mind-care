"""Production caregiver Handler/Registry with fake DB and SDK MockTransport."""
from functools import partial
import inspect
import json
from threading import Barrier
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import UUID

import httpx
from openai import OpenAI

from ..adapters.caregiver_profile import CaregiverProfileAdapter
from ..adapters.patient_profile import PatientProfileAdapter
from ..clients.openai_client import OpenAIClient
from ..composition import build_chat_registry, build_evidence_registry
from ..executor import ToolExecutor
from ..registry import TOOL_CONTRACTS, ToolName
from ..schemas import ModelTurn
from ..testing.chat_workflow_fakes import chat_call, chat_final
from ..testing.fake_llm import FakeLLMClient
from ..tools.caregiver_profile import (
    CAREGIVER_PROFILE_DATA_POLICY, CaregiverProfileHandler, CaregiverProfileInput,
    CaregiverProfileOutput, CaregiverProfileReader,
)
from ..tools.evidence_tools import EvidenceRetriever
from ..tools.patient_profile import PATIENT_PROFILE_DATA_POLICY
from ..workflows.chat_context_prepare import prepare_chat_input
from ..workflows.chat_execution import ChatExecutionV1
from ..workflows.chat_prompts import CHAT_TOOL_POLICY
from .test_caregiver_profile_adapter import profile
from .test_patient_profile_adapter import patient
from .test_feed_candidates import package
from .test_responses import function, response, response_fixture
from .test_workflows import CONTEXT


USER_CONTEXT = CONTEXT.model_copy(update={'patient_id':None})


class CaregiverProfileHandlerTests(TestCase):
    def setUp(self):
        for target in ('socket.socket.connect','psycopg.connect','app.ai.retrieval.embedding.get_model'):
            guard = patch(target,side_effect=AssertionError('Live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_handler_only_passes_same_context_to_reader_and_returns_result(self):
        result = CaregiverProfileOutput(profile=None)
        reader = Mock(spec=CaregiverProfileReader)
        reader.get_caregiver_profile.return_value = result
        returned = CaregiverProfileHandler(reader)(USER_CONTEXT,CaregiverProfileInput())
        reader.get_caregiver_profile.assert_called_once_with(context=USER_CONTEXT)
        self.assertIs(reader.get_caregiver_profile.call_args.kwargs['context'],USER_CONTEXT)
        self.assertIs(returned,result)
        self.assertIs(inspect.signature(CaregiverProfileHandler.__init__).parameters['reader'].annotation,
                      CaregiverProfileReader)

    def test_canonical_caregiver_spec_is_always_registered_without_mutating_search_factory(self):
        for context in (CONTEXT,USER_CONTEXT):
            tools = build_chat_registry(context)
            expected = {'search_evidence','get_caregiver_profile'}
            if context.patient_id is not None: expected.update({'get_patient_profile','get_recent_care_logs','get_patient_medications','get_patient_medical_visits','get_patient_assessments','get_patient_safety_events'})
            self.assertEqual(set(tools.names()),expected)
            self.assertEqual(tools.mode,'production')
            spec = tools.get('get_caregiver_profile')
            self.assertIs(spec.contract,TOOL_CONTRACTS[ToolName.CAREGIVER_PROFILE])
            self.assertFalse(spec.contract.patient_scoped)
            self.assertFalse(spec.test_only)
            self.assertTrue(spec.parallel_safe)
            self.assertIsInstance(spec.handler,CaregiverProfileHandler)
            self.assertIsInstance(spec.handler.reader,CaregiverProfileAdapter)
            self.assertIsNone(spec.handler.reader.get_caregiver_profile_fn)
            self.assertTrue(all(not build_chat_registry(context,parallel_safe=False).get(n).parallel_safe for n in tools.names()))
            self.assertIsNot(tools,build_chat_registry(context))
        self.assertEqual(build_evidence_registry().names(),('search_evidence',))

    def test_invalid_extra_arguments_are_rejected_before_any_db_lookup(self):
        lookup = Mock(side_effect=AssertionError('Invalid arguments must not query'))
        tools = build_chat_registry(USER_CONTEXT,caregiver_reader=CaregiverProfileAdapter(get_caregiver_profile_fn=lookup))
        for field in ('user_id','patient_id','relationship','include_scores'):
            outcome,_=ToolExecutor(tools).execute(chat_call(ToolName.CAREGIVER_PROFILE,args={field:'untrusted'}),USER_CONTEXT)
            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error.code,'invalid_arguments')
        lookup.assert_not_called()

    def test_bad_rows_db_errors_and_timeouts_never_become_successful_absence(self):
        invalid_score=profile().model_copy(update={'burden_score':89})
        invalid_time=profile().model_copy(update={'updated_at':profile().updated_at.replace(tzinfo=None)})
        cases=[profile(user_id=UUID(int=988)),invalid_score,invalid_time,
               RuntimeError('PRIVATE_CAREGIVER_DATA'),TimeoutError('PRIVATE_CAREGIVER_DATA')]
        for value in cases:
            with self.subTest(kind=type(value)):
                lookup=Mock(side_effect=value) if isinstance(value,Exception) else Mock(return_value=value)
                tools=build_chat_registry(USER_CONTEXT,caregiver_reader=CaregiverProfileAdapter(get_caregiver_profile_fn=lookup))
                with self.assertLogs('app.ai.agent.executor',level='INFO') as logs:
                    outcome,trace=ToolExecutor(tools).execute(chat_call(ToolName.CAREGIVER_PROFILE),USER_CONTEXT)
                self.assertFalse(outcome.success)
                self.assertIsNone(outcome.data)
                self.assertEqual(outcome.error.code,'tool_timeout' if isinstance(value,TimeoutError) else 'tool_exception')
                self.assertFalse(trace.success)
                self.assertNotIn('PRIVATE_CAREGIVER_DATA',outcome.model_dump_json())
                self.assertNotIn('PRIVATE_CAREGIVER_DATA','\n'.join(logs.output))
                lookup.assert_called_once_with(USER_CONTEXT.user_id)

    def execute_sdk(self,lookup,*,context=USER_CONTEXT,called=True):
        wire=[]
        final=response_fixture({'text':chat_final('저장된 간병인 정보 범위의 합성 설명').text})
        call={**function('caregiver','{}'),'name':'get_caregiver_profile'}
        turns=[response([call]),final] if called else [final]
        def transport(request):
            self.assertEqual(request.url.path,'/v1/responses')
            wire.append(json.loads(request.content))
            self.assertLessEqual(len(wire),len(turns))
            return httpx.Response(200,json=turns[len(wire)-1])
        retriever=Mock(spec=EvidenceRetriever)
        patient_lookup=Mock(side_effect=AssertionError('Caregiver request must not query patient'))
        factory=partial(build_chat_registry,retriever=retriever,
            patient_reader=PatientProfileAdapter(get_patient_fn=patient_lookup),
            caregiver_reader=CaregiverProfileAdapter(get_caregiver_profile_fn=lookup))
        with OpenAI(api_key='offline-placeholder',max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
            result=ChatExecutionV1(OpenAIClient(sdk,model='offline'),factory)(
                context,prepare_chat_input(context,'앱에 등록된 내 정보를 확인해 주세요.'))
        patient_lookup.assert_not_called()
        retriever.search_evidence.assert_not_called()
        return result,wire

    def test_sdk_normal_sparse_and_absent_profiles_succeed_with_or_without_selected_patient(self):
        for context in (CONTEXT,USER_CONTEXT):
            for raw in (profile(),profile(relationship=None,burden_score=None,mood_score=None,lifestyle_tags=[]),None):
                with self.subTest(patient_selected=context.patient_id is not None,absent=raw is None):
                    lookup=Mock(return_value=raw)
                    result,wire=self.execute_sdk(lookup,context=context)
                    self.assertEqual(result.execution.status,'completed')
                    self.assertEqual(result.execution.errors,[])
                    self.assertEqual(result.sources,[])
                    self.assertFalse(any(m.get('content')==CAREGIVER_PROFILE_DATA_POLICY for m in wire[0]['input']))
                    messages=wire[1]['input']
                    output=next(m for m in messages if m.get('type')=='function_call_output' and m['call_id']=='caregiver')
                    value=json.loads(output['output'])
                    self.assertTrue(value['success'])
                    expected=None if raw is None else {k:v for k,v in raw.model_dump(mode='json').items() if k!='user_id'}
                    self.assertEqual(value['data'],{'profile':expected})
                    policies=[m for m in messages if m.get('content')==CAREGIVER_PROFILE_DATA_POLICY]
                    self.assertEqual(len(policies),1)
                    self.assertEqual(policies[0]['role'],'system')
                    self.assertGreater(messages.index(policies[0]),messages.index(output))
                    self.assertFalse(any(m.get('content')==PATIENT_PROFILE_DATA_POLICY for m in messages))
                    for body in wire:
                        serialized=json.dumps(body,ensure_ascii=False)
                        self.assertNotIn(str(context.user_id),serialized)
                        if context.patient_id is not None:self.assertNotIn(str(context.patient_id),serialized)
                    if raw is not None:
                        self.assertFalse(any(raw.relationship in m.get('content','') for m in messages
                                             if m.get('role')=='system' and raw.relationship is not None))
                    lookup.assert_called_once_with(context.user_id)

    def test_sdk_owner_mismatch_db_exception_and_invalid_output_do_not_deliver_policy(self):
        cases=[profile(user_id=UUID(int=988)),RuntimeError('PRIVATE_CAREGIVER_DATA'),
               profile().model_copy(update={'mood_score':28}),
               profile().model_copy(update={'updated_at':profile().updated_at.replace(tzinfo=None)})]
        for raw in cases:
            with self.subTest(kind=type(raw)):
                lookup=Mock(side_effect=raw) if isinstance(raw,Exception) else Mock(return_value=raw)
                result,wire=self.execute_sdk(lookup)
                self.assertEqual(result.execution.status,'completed')
                output=next(m for m in wire[1]['input'] if m.get('type')=='function_call_output')
                value=json.loads(output['output'])
                self.assertFalse(value['success'])
                self.assertIsNone(value['data'])
                self.assertEqual(value['error']['code'],'tool_exception')
                self.assertFalse(any(m.get('content')==CAREGIVER_PROFILE_DATA_POLICY for body in wire for m in body['input']))
                self.assertNotIn('PRIVATE_CAREGIVER_DATA',json.dumps(wire))
                lookup.assert_called_once_with(USER_CONTEXT.user_id)

    def test_general_question_exposes_only_definition_and_selection_guidance_without_lookup(self):
        lookup=Mock(side_effect=AssertionError('No automatic caregiver lookup'))
        result,wire=self.execute_sdk(lookup,called=False)
        self.assertEqual(result.execution.status,'completed')
        lookup.assert_not_called()
        definition=next(d for d in wire[0]['tools'] if d['name']=='get_caregiver_profile')
        self.assertEqual(definition,TOOL_CONTRACTS[ToolName.CAREGIVER_PROFILE].definition())
        self.assertIn('get_caregiver_profile은 간병인의 앱에 저장된 관계, 자가점검 점수 또는 생활환경 정보를 실제로 확인해야 할 때 사용한다.',CHAT_TOOL_POLICY)
        self.assertTrue(any('일반적인 상담에는 불필요하게 호출하지 않는다.' in m.get('content','') for m in wire[0]['input'] if m.get('role')=='system'))
        self.assertFalse(any(m.get('content')==CAREGIVER_PROFILE_DATA_POLICY for m in wire[0]['input']))

    def test_three_independent_tools_run_in_parallel_with_separate_policies_and_call_ids(self):
        barrier=Barrier(3)
        def caregiver_lookup(key):
            barrier.wait(2)
            self.assertEqual(key,CONTEXT.user_id)
            return profile()
        def patient_lookup(key):
            barrier.wait(2)
            self.assertEqual(key,CONTEXT.patient_id)
            return patient()
        retriever=Mock(spec=EvidenceRetriever)
        def retrieve(**kwargs):
            barrier.wait(2)
            return package(['Synthetic PMID'])
        retriever.search_evidence.side_effect=retrieve
        factory=partial(build_chat_registry,retriever=retriever,
            patient_reader=PatientProfileAdapter(get_patient_fn=patient_lookup),
            caregiver_reader=CaregiverProfileAdapter(get_caregiver_profile_fn=caregiver_lookup))
        calls=[chat_call(ToolName.CAREGIVER_PROFILE,identifier='caregiver'),chat_call(identifier='patient'),
               chat_call(ToolName.SEARCH_EVIDENCE,identifier='search',args={'query':'Synthetic','top_k':1})]
        client=FakeLLMClient([ModelTurn(tool_calls=calls),chat_final(selected=[True])])
        result=ChatExecutionV1(client,factory)(CONTEXT,prepare_chat_input(CONTEXT,'등록 정보와 연구를 확인해 주세요.'))
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(result.execution.errors,[])
        self.assertEqual([m.call_id for m in client.requests[1][0] if m.type=='function_call_output'],['caregiver','patient','search'])
        for policy in (CAREGIVER_PROFILE_DATA_POLICY,PATIENT_PROFILE_DATA_POLICY):
            self.assertEqual(sum(m.content==policy for m in client.requests[1][0]),1)
        self.assertEqual(result.sources[0].source_ref.tool_call_id,'search')
