"""Production assessment Handler/Chat/Executor wiring over offline DB fakes."""
from functools import partial
import json
from threading import Barrier
from unittest import TestCase
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI

from app.db import user_functions
from ..adapters.patient_assessments import PatientAssessmentsAdapter
from ..clients.openai_client import OpenAIClient
from ..composition import build_chat_registry, build_evidence_registry
from ..executor import ToolExecutor
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..schemas import ModelTurn, ToolCall
from ..testing.chat_workflow_fakes import chat_final
from ..testing.fake_llm import FakeLLMClient
from ..tools.assessments import (PATIENT_ASSESSMENTS_DATA_POLICY, PatientAssessmentsHandler,
    PatientAssessmentsInput, PatientAssessmentsOutput, PatientAssessmentsReader)
from ..tools.evidence_tools import EvidenceRetriever
from ..workflows.chat_context_prepare import prepare_chat_input
from ..workflows.chat_execution import ChatExecutionV1
from ..workflows.chat_prompts import CHAT_TOOL_POLICY
from .test_patient_assessments_adapter import assessment
from .test_patient_profile_adapter import patient
from .test_responses import function, response, response_fixture
from .test_workflows import CONTEXT


class PatientAssessmentsHandlerTests(TestCase):
    def setUp(self):
        for target in ('socket.socket.connect','psycopg.connect','app.ai.retrieval.embedding.get_model'):
            guard = patch(target,side_effect=AssertionError('Live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_handler_preserves_context_input_alias_and_reader_output_identity(self):
        reader = Mock(spec=PatientAssessmentsReader)
        expected = PatientAssessmentsOutput(assessments=[])
        reader.get_patient_assessments.return_value = expected
        for mode in ('latest_by_type','history'):
            reader.reset_mock()
            result = PatientAssessmentsHandler(reader)(CONTEXT,
                PatientAssessmentsInput(mode=mode,assessment_type=' k-mmse ',limit=2))
            self.assertIs(result,expected)
            reader.get_patient_assessments.assert_called_once_with(context=CONTEXT,mode=mode,assessment_type=' k-mmse ',limit=2)
            self.assertIs(reader.get_patient_assessments.call_args.kwargs['context'],CONTEXT)

    def test_invalid_input_or_missing_patient_fails_before_reader(self):
        reader = Mock(spec=PatientAssessmentsReader)
        executor = ToolExecutor(ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.PATIENT_ASSESSMENTS],PatientAssessmentsHandler(reader)),)))
        for args in ({'mode':'bad'},{'limit':True},{'limit':31},{'patient_id':'untrusted'},{'assessment_type':'  '}):
            outcome,_ = executor.execute(ToolCall(id='assessment',name='get_patient_assessments',arguments=args),CONTEXT)
            self.assertEqual(outcome.error.code,'invalid_arguments')
        outcome,_ = executor.execute(ToolCall(id='assessment',name='get_patient_assessments',arguments={}),
                                    CONTEXT.model_copy(update={'patient_id':None}))
        self.assertEqual(outcome.error.code,'patient_context_mismatch')
        reader.get_patient_assessments.assert_not_called()

    def test_factory_adds_canonical_patient_only_tool_and_keeps_search_factory(self):
        reader = Mock(spec=PatientAssessmentsReader)
        registry = build_chat_registry(CONTEXT,assessments_reader=reader)
        self.assertEqual(set(registry.names()),{'search_evidence','search_guidelines','get_patient_profile','get_caregiver_profile',
            'get_recent_care_logs','get_patient_medications','get_patient_medical_visits','get_patient_assessments','get_patient_safety_events'})
        spec = registry.get('get_patient_assessments')
        self.assertIs(spec.contract,TOOL_CONTRACTS[ToolName.PATIENT_ASSESSMENTS])
        self.assertIs(spec.handler.reader,reader)
        self.assertTrue(spec.parallel_safe)
        self.assertFalse(spec.test_only)
        self.assertIsInstance(build_chat_registry(CONTEXT).get('get_patient_assessments').handler.reader,PatientAssessmentsAdapter)
        self.assertFalse(build_chat_registry(CONTEXT,parallel_safe=False).get('get_patient_assessments').parallel_safe)
        self.assertEqual(set(build_chat_registry(CONTEXT.model_copy(update={'patient_id':None})).names()),{'search_evidence','search_guidelines','get_caregiver_profile'})
        self.assertEqual(build_evidence_registry().names(),('search_evidence',))
        self.assertIn('일반적인 검사 설명에는 불필요하게 호출하지 않는다',CHAT_TOOL_POLICY)

    def execute_sdk(self, adapter, *, args=None, context=CONTEXT, called=True, repeated=False):
        wire = []
        identifiers = ('assessment1','assessment2') if repeated else ('assessment1',)
        calls = [{**function(identifier,json.dumps(args or {})),'name':'get_patient_assessments'} for identifier in identifiers]
        turns = ([response([call]) for call in calls] if called else []) + [response_fixture({'text':chat_final().text})]
        def transport(request):
            wire.append(json.loads(request.content))
            self.assertLessEqual(len(wire),len(turns))
            return httpx.Response(200,json=turns[len(wire)-1])
        unused = Mock(side_effect=AssertionError('No unrelated lookup'))
        retriever = Mock(spec=EvidenceRetriever)
        factory = partial(build_chat_registry,assessments_reader=adapter,retriever=retriever,
            patient_reader=unused,caregiver_reader=unused,care_logs_reader=unused,medications_reader=unused,visits_reader=unused)
        with OpenAI(api_key='offline-placeholder',max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
            result = ChatExecutionV1(OpenAIClient(sdk,model='offline'),factory)(context,
                prepare_chat_input(context,'앱에 등록된 검사 결과를 확인해 주세요.'))
        for name in ('get_patient_profile','get_caregiver_profile','get_recent_care_logs','get_patient_medications','get_patient_medical_visits'):
            getattr(unused,name).assert_not_called()
        retriever.search_evidence.assert_not_called()
        return result,wire

    def test_sdk_both_modes_empty_and_repeated_success_preserve_metadata_data_and_policy(self):
        for mode in ('latest_by_type','history'):
            for empty in (False,True):
                raw = assessment(score=None if empty else 22.0)
                query = Mock(return_value=[] if empty else [raw])
                adapter = PatientAssessmentsAdapter(get_patient_fn=Mock(return_value=patient()),
                    get_latest_assessments_fn=query,list_assessments_fn=query)
                result,wire = self.execute_sdk(adapter,args={'mode':mode,'assessment_type':' k-mmse ','limit':2},repeated=True)
                self.assertEqual(result.execution.status,'completed')
                self.assertEqual(result.execution.errors,[])
                self.assertEqual(result.sources,[])
                self.assertEqual([t.result_count for t in result.execution.called_tools],[int(not empty)]*2)
                self.assertEqual(query.call_count,2)
                self.assertNotIn(PATIENT_ASSESSMENTS_DATA_POLICY,json.dumps(wire[0],ensure_ascii=False))
                for body in wire[1:]:
                    messages = body['input']
                    policies = [m for m in messages if m.get('content')==PATIENT_ASSESSMENTS_DATA_POLICY]
                    self.assertEqual(len(policies),1)
                    self.assertEqual(policies[0]['role'],'system')
                    outputs = [m for m in messages if m.get('type')=='function_call_output']
                    self.assertGreater(messages.index(policies[0]),messages.index(outputs[0]))
                    for output in outputs:
                        call = next(m for m in messages if m.get('type')=='function_call' and m['call_id']==output['call_id'])
                        self.assertLess(messages.index(call),messages.index(output))
                        value = json.loads(output['output'])
                        self.assertTrue(value['success'])
                        self.assertEqual(set(value['data']),{'assessments'})
                        self.assertEqual(len(value['data']['assessments']),int(not empty))
                        if not empty:
                            item = value['data']['assessments'][0]
                            info = next(i for i in user_functions.list_assessment_types() if i.assessment_type=='K-MMSE')
                            self.assertEqual(item,{'assessment_type':'K-MMSE','assessment_name':info.name,
                                'score':raw.score,'score_min':info.min,'score_max':info.max,'higher_is_worse':info.higher_is_worse,
                                'assessed_at':raw.assessed_at.isoformat(),'assessed_by':raw.assessed_by,'result_detail':raw.result_detail})
                            self.assertFalse(any(raw.result_detail in m.get('content','') for m in messages if m.get('role')=='system'))
                    self.assertNotIn(str(CONTEXT.patient_id),json.dumps(body))
                    self.assertNotIn(str(raw.id),json.dumps(body))
                self.assertEqual([m['call_id'] for m in wire[-1]['input'] if m.get('type')=='function_call_output'],['assessment1','assessment2'])

    def test_sdk_source_errors_missing_fields_and_unknown_input_never_become_empty_success(self):
        missing = assessment().model_dump()
        del missing['score']
        for raw,kind in ((None,None),([missing],None),([assessment().model_copy(update={'score':True})],None),
                         (RuntimeError('PRIVATE DB'),None),([], 'unsupported-exam')):
            query = Mock(return_value=raw,side_effect=raw if isinstance(raw,Exception) else None)
            args = {} if kind is None else {'assessment_type':kind}
            result,wire = self.execute_sdk(PatientAssessmentsAdapter(get_patient_fn=Mock(return_value=patient()),
                get_latest_assessments_fn=query),args=args)
            self.assertEqual(result.execution.status,'completed')
            output = next(m for m in wire[1]['input'] if m.get('type')=='function_call_output')
            value = json.loads(output['output'])
            self.assertFalse(value['success'])
            self.assertIsNone(value['data'])
            self.assertEqual(value['error']['code'],'tool_exception')
            self.assertIsNone(result.execution.called_tools[0].result_count)
            self.assertFalse(any(m.get('content')==PATIENT_ASSESSMENTS_DATA_POLICY for b in wire for m in b['input']))
            self.assertNotIn('PRIVATE',json.dumps(wire))
            if kind is not None:query.assert_not_called()

    def test_sdk_timeout_remains_terminal_and_does_not_deliver_policy(self):
        query = Mock(side_effect=TimeoutError('PRIVATE timeout'))
        result,wire = self.execute_sdk(PatientAssessmentsAdapter(get_patient_fn=Mock(return_value=patient()),get_latest_assessments_fn=query))
        self.assertEqual(result.execution.status,'failed')
        self.assertEqual(result.execution.errors[-1].code,'tool_timeout')
        self.assertEqual(len(wire),1)
        query.assert_called_once()

    def test_no_lookup_or_policy_when_general_question_or_patient_unselected(self):
        lookup,query = Mock(),Mock()
        adapter = PatientAssessmentsAdapter(get_patient_fn=lookup,get_latest_assessments_fn=query)
        for selected,called in ((True,False),(False,False),(False,True)):
            context = CONTEXT if selected else CONTEXT.model_copy(update={'patient_id':None})
            result,wire = self.execute_sdk(adapter,context=context,called=called)
            self.assertEqual(result.execution.status,'completed')
            self.assertFalse(any(m.get('content')==PATIENT_ASSESSMENTS_DATA_POLICY for b in wire for m in b['input']))
        lookup.assert_not_called()
        query.assert_not_called()

    def test_parallel_assessments_and_search_preserve_call_ids_and_sources(self):
        from .test_feed_candidates import package
        barrier = Barrier(2)
        def latest(key):
            barrier.wait(2)
            return [assessment()]
        def search(**kwargs):
            barrier.wait(2)
            return package(['Synthetic PMID'])
        retriever = Mock(spec=EvidenceRetriever)
        retriever.search_evidence.side_effect = search
        adapter = PatientAssessmentsAdapter(get_patient_fn=Mock(return_value=patient()),get_latest_assessments_fn=latest)
        client = FakeLLMClient([ModelTurn(tool_calls=[
            ToolCall(id='assessment',name='get_patient_assessments',arguments={}),
            ToolCall(id='search',name='search_evidence',arguments={'query':'Synthetic','top_k':1})]),chat_final(selected=[True])])
        result = ChatExecutionV1(client,partial(build_chat_registry,assessments_reader=adapter,retriever=retriever))(
            CONTEXT,prepare_chat_input(CONTEXT,'검사 기록과 연구를 확인해 주세요.'))
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(result.execution.errors,[])
        messages = client.requests[1][0]
        self.assertEqual([m.call_id for m in messages if m.type=='function_call_output'],['assessment','search'])
        self.assertEqual(sum(m.content==PATIENT_ASSESSMENTS_DATA_POLICY for m in messages),1)
        self.assertEqual(result.sources[0].source_ref.tool_call_id,'search')

    def test_executor_trace_counts_only_validated_returned_assessments_and_safe_logs(self):
        for count in (0,2):
            rows = [assessment() for _ in range(count)]
            adapter = PatientAssessmentsAdapter(get_patient_fn=Mock(return_value=patient()),list_assessments_fn=Mock(return_value=rows))
            executor = ToolExecutor(ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.PATIENT_ASSESSMENTS],PatientAssessmentsHandler(adapter)),)))
            with self.assertLogs('app.ai.agent.executor',level='INFO') as captured:
                outcome,trace = executor.execute(ToolCall(id='assessment',name='get_patient_assessments',arguments={'mode':'history'}),CONTEXT)
            self.assertTrue(outcome.success)
            self.assertEqual(trace.result_count,count)
            self.assertIn(f'result_count={count}','\n'.join(captured.output))
            self.assertNotIn('지시','\n'.join(captured.output))
