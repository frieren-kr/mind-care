"""Visit Handler and Chat SDK messages over injected DB boundaries; offline only."""
from datetime import date, datetime, timedelta, timezone
from functools import partial
import json
from threading import Barrier
from unittest import TestCase
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI

from app import user_schemas
from ..adapters.patient_medical_visits import PatientMedicalVisitsAdapter
from ..clients.openai_client import OpenAIClient
from ..composition import build_chat_registry, build_evidence_registry
from ..executor import ToolExecutor
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..schemas import ModelTurn, ToolCall
from ..testing.chat_workflow_fakes import chat_final
from ..testing.fake_llm import FakeLLMClient
from ..tools.evidence_tools import EvidenceRetriever
from ..tools.medical_visits import (PATIENT_MEDICAL_VISITS_DATA_POLICY, PatientMedicalVisitsHandler,
    PatientMedicalVisitsInput, PatientMedicalVisitsOutput, PatientMedicalVisitsReader)
from ..workflows.chat_context_prepare import prepare_chat_input
from ..workflows.chat_execution import ChatExecutionV1
from ..workflows.chat_prompts import CHAT_TOOL_POLICY
from .test_medical_visits_contract import TEXT_FIELDS
from .test_patient_medical_visits_adapter import TODAY, visit
from .test_patient_profile_adapter import patient
from .test_responses import function, response, response_fixture
from .test_workflows import CONTEXT


class PatientMedicalVisitsHandlerTests(TestCase):
    def setUp(self):
        for target in ('socket.socket.connect','psycopg.connect','app.ai.retrieval.embedding.get_model'):
            guard = patch(target, side_effect=AssertionError('Live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_handler_forwards_arguments_and_reads_clock_once_only_for_upcoming(self):
        for status in ('completed','unvisited','all','upcoming'):
            reader = Mock(spec=PatientMedicalVisitsReader)
            expected = PatientMedicalVisitsOutput(visits=[])
            reader.get_patient_medical_visits.return_value = expected
            clock = Mock(return_value=TODAY)
            result = PatientMedicalVisitsHandler(reader, today_fn=clock)(CONTEXT,
                PatientMedicalVisitsInput(status=status, start_date=TODAY, end_date=TODAY, limit=2))
            self.assertIs(result, expected)
            reader.get_patient_medical_visits.assert_called_once_with(context=CONTEXT, status=status,
                start_date=TODAY, end_date=TODAY, limit=2, today_kst=TODAY if status=='upcoming' else None)
            self.assertIs(reader.get_patient_medical_visits.call_args.kwargs['context'], CONTEXT)
            self.assertEqual(clock.call_count, int(status=='upcoming'))

    def test_default_clock_reuses_server_today_and_handles_kst_midnight(self):
        for instant, expected in ((datetime(2026,11,4,14,59,59,tzinfo=timezone.utc),date(2026,11,4)),
                                  (datetime(2026,11,4,15,tzinfo=timezone.utc),TODAY)):
            reader = Mock(spec=PatientMedicalVisitsReader)
            with patch.object(user_schemas, 'datetime', wraps=datetime) as clock:
                clock.now.side_effect = lambda tz:instant.astimezone(tz)
                PatientMedicalVisitsHandler(reader)(CONTEXT, PatientMedicalVisitsInput(status='upcoming'))
                clock.now.assert_called_once_with(user_schemas.APP_TIMEZONE)
            self.assertEqual(reader.get_patient_medical_visits.call_args.kwargs['today_kst'], expected)

    def test_invalid_clock_or_input_does_not_reach_reader(self):
        reader = Mock(spec=PatientMedicalVisitsReader)
        for value in (None, '2026-11-05', datetime(2026,11,5)):
            with self.assertRaises(ValueError):
                PatientMedicalVisitsHandler(reader, today_fn=Mock(return_value=value))(
                    CONTEXT, PatientMedicalVisitsInput(status='upcoming'))
        clock = Mock(return_value=TODAY)
        registry = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.PATIENT_MEDICAL_VISITS],
            PatientMedicalVisitsHandler(reader, today_fn=clock)),))
        for args in ({'limit':True}, {'limit':21}, {'today_kst':'2026-11-05'}, {'patient_id':'untrusted'},
                     {'start_date':'2026-11-05','end_date':'2026-11-04'}):
            outcome, _ = ToolExecutor(registry).execute(ToolCall(id='visits',name='get_patient_medical_visits',arguments=args),CONTEXT)
            self.assertEqual(outcome.error.code, 'invalid_arguments')
        clock.assert_not_called()
        reader.get_patient_medical_visits.assert_not_called()

    def test_factory_registers_canonical_selected_patient_tool_and_preserves_search_only(self):
        reader = Mock(spec=PatientMedicalVisitsReader)
        registry = build_chat_registry(CONTEXT, visits_reader=reader)
        self.assertEqual(set(registry.names()), {'search_evidence','get_patient_profile','get_caregiver_profile',
            'get_recent_care_logs','get_patient_medications','get_patient_medical_visits'})
        spec = registry.get('get_patient_medical_visits')
        self.assertIs(spec.contract,TOOL_CONTRACTS[ToolName.PATIENT_MEDICAL_VISITS])
        self.assertIs(spec.handler.reader,reader)
        self.assertTrue(spec.parallel_safe)
        self.assertFalse(spec.test_only)
        default = build_chat_registry(CONTEXT).get('get_patient_medical_visits').handler.reader
        self.assertIsInstance(default,PatientMedicalVisitsAdapter)
        self.assertIsNone(default.upcoming_query_fn)
        self.assertFalse(build_chat_registry(CONTEXT,parallel_safe=False).get('get_patient_medical_visits').parallel_safe)
        self.assertEqual(set(build_chat_registry(CONTEXT.model_copy(update={'patient_id':None})).names()),
                         {'search_evidence','get_caregiver_profile'})
        self.assertEqual(build_evidence_registry().names(),('search_evidence',))
        self.assertIn('일반적인 진료·검사 설명에는 불필요하게 호출하지 않는다',CHAT_TOOL_POLICY)

    def execute_sdk(self, adapter, *, args=None, repeated=False, called=True, context=CONTEXT):
        wire = []
        identifiers = ('visit1','visit2') if repeated else ('visit1',)
        calls = [{**function(identifier,json.dumps(args or {})),'name':'get_patient_medical_visits'} for identifier in identifiers]
        turns = ([response([call]) for call in calls] if called else []) + [response_fixture({'text':chat_final().text})]
        def transport(request):
            wire.append(json.loads(request.content))
            self.assertLessEqual(len(wire),len(turns))
            return httpx.Response(200,json=turns[len(wire)-1])
        unused = Mock(side_effect=AssertionError('No unrelated lookup'))
        retriever = Mock(spec=EvidenceRetriever)
        factory = partial(build_chat_registry, visits_reader=adapter, retriever=retriever,
            patient_reader=unused, caregiver_reader=unused, care_logs_reader=unused, medications_reader=unused)
        with patch.object(user_schemas,'today',return_value=TODAY), \
             OpenAI(api_key='offline-placeholder',max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
            result = ChatExecutionV1(OpenAIClient(sdk,model='offline'),factory)(context,
                prepare_chat_input(context,'앱에 등록된 진료 기록을 확인해 주세요.'))
        for name in ('get_patient_profile','get_caregiver_profile','get_recent_care_logs','get_patient_medications'):
            getattr(unused,name).assert_not_called()
        retriever.search_evidence.assert_not_called()
        return result, wire

    def test_sdk_all_statuses_preserve_exact_projection_and_success_only_policy_once(self):
        for status in ('completed','unvisited','all','upcoming'):
            for empty in (False,True):
                raw = visit(is_visited=status not in ('upcoming','unvisited'))
                query = Mock(return_value=[] if empty else [raw])
                result, wire = self.execute_sdk(PatientMedicalVisitsAdapter(get_patient_fn=Mock(return_value=patient()),
                    list_medical_visits_fn=query,upcoming_query_fn=query),args={'status':status,'limit':2},repeated=True)
                self.assertEqual(result.execution.status,'completed')
                self.assertEqual(result.execution.errors,[])
                self.assertEqual(result.sources,[])
                self.assertEqual(query.call_count,2)
                self.assertNotIn(PATIENT_MEDICAL_VISITS_DATA_POLICY,json.dumps(wire[0],ensure_ascii=False))
                for body in wire[1:]:
                    messages = body['input']
                    policies = [m for m in messages if m.get('content')==PATIENT_MEDICAL_VISITS_DATA_POLICY]
                    self.assertEqual(len(policies),1)
                    self.assertEqual(policies[0]['role'],'system')
                    outputs = [m for m in messages if m.get('type')=='function_call_output']
                    self.assertGreater(messages.index(policies[0]),messages.index(outputs[0]))
                    for output in outputs:
                        call = next(m for m in messages if m.get('type')=='function_call' and m['call_id']==output['call_id'])
                        self.assertLess(messages.index(call),messages.index(output))
                        value = json.loads(output['output'])
                        self.assertTrue(value['success'])
                        self.assertEqual(set(value['data']),{'visits'})
                        self.assertEqual(len(value['data']['visits']),int(not empty))
                        if not empty:
                            expected = {k:v for k,v in raw.model_dump(mode='json').items() if k in TEXT_FIELDS | {'visit_date','is_visited'}}
                            self.assertEqual(value['data']['visits'],[expected])
                            self.assertFalse(any(raw.visit_content in m.get('content','') for m in messages if m.get('role')=='system'))
                    self.assertNotIn(str(CONTEXT.patient_id),json.dumps(body))
                    self.assertNotIn(str(raw.id),json.dumps(body))
                self.assertEqual([m['call_id'] for m in wire[-1]['input'] if m.get('type')=='function_call_output'],['visit1','visit2'])

    def test_sdk_empty_intersection_succeeds_but_missing_upcoming_binding_does_not(self):
        for past in (True,False):
            lookup = Mock(return_value=patient())
            legacy = Mock(side_effect=AssertionError('No fallback'))
            args = {'status':'upcoming'}
            if past:args['end_date']=(TODAY-timedelta(days=1)).isoformat()
            result,wire = self.execute_sdk(PatientMedicalVisitsAdapter(get_patient_fn=lookup,list_medical_visits_fn=legacy),args=args)
            self.assertEqual(result.execution.status,'completed')
            output = next(m for m in wire[1]['input'] if m.get('type')=='function_call_output')
            envelope = json.loads(output['output'])
            self.assertIs(envelope['success'],past)
            if past:self.assertEqual(envelope['data'],{'visits':[]})
            else:
                self.assertIsNone(envelope['data'])
                self.assertEqual(envelope['error']['code'],'tool_exception')
            self.assertEqual(sum(m.get('content')==PATIENT_MEDICAL_VISITS_DATA_POLICY for m in wire[1]['input']),int(past))
            lookup.assert_called_once()
            legacy.assert_not_called()

    def test_sdk_invalid_source_or_permission_failure_has_no_policy_or_private_details(self):
        missing = visit().model_dump()
        del missing['diagnosis']
        for data in (None,[missing],[visit(is_visited=False)],RuntimeError('PRIVATE DB'),PermissionError('PRIVATE denied')):
            query = Mock(return_value=data,side_effect=data if isinstance(data,Exception) else None)
            result,wire = self.execute_sdk(PatientMedicalVisitsAdapter(get_patient_fn=Mock(return_value=patient()),list_medical_visits_fn=query))
            self.assertEqual(result.execution.status,'completed')
            output = next(m for m in wire[1]['input'] if m.get('type')=='function_call_output')
            envelope = json.loads(output['output'])
            self.assertFalse(envelope['success'])
            self.assertIsNone(envelope['data'])
            self.assertEqual(envelope['error']['code'],'tool_exception')
            self.assertFalse(any(m.get('content')==PATIENT_MEDICAL_VISITS_DATA_POLICY for b in wire for m in b['input']))
            self.assertNotIn('PRIVATE',json.dumps(wire))

    def test_sdk_timeout_stops_via_existing_executor_behavior(self):
        query = Mock(side_effect=TimeoutError('PRIVATE timeout'))
        result,wire = self.execute_sdk(PatientMedicalVisitsAdapter(get_patient_fn=Mock(return_value=patient()),list_medical_visits_fn=query))
        self.assertEqual(result.execution.status,'failed')
        self.assertEqual(result.execution.errors[-1].code,'tool_timeout')
        self.assertEqual(len(wire),1)
        query.assert_called_once()

    def test_no_patient_or_general_question_never_queries_or_delivers_policy(self):
        lookup, query = Mock(), Mock()
        adapter = PatientMedicalVisitsAdapter(get_patient_fn=lookup,list_medical_visits_fn=query,upcoming_query_fn=query)
        for selected,called in ((True,False),(False,False),(False,True)):
            context = CONTEXT if selected else CONTEXT.model_copy(update={'patient_id':None})
            result,wire = self.execute_sdk(adapter,context=context,called=called)
            self.assertEqual(result.execution.status,'completed')
            self.assertFalse(any(m.get('content')==PATIENT_MEDICAL_VISITS_DATA_POLICY for b in wire for m in b['input']))
        lookup.assert_not_called()
        query.assert_not_called()

    def test_parallel_visits_and_search_preserve_provider_ids_and_source_snapshot(self):
        from .test_feed_candidates import package
        barrier = Barrier(2)
        def listing(*args,**kwargs):
            barrier.wait(2)
            return [visit()]
        def search(**kwargs):
            barrier.wait(2)
            return package(['Synthetic PMID'])
        retriever = Mock(spec=EvidenceRetriever)
        retriever.search_evidence.side_effect = search
        adapter = PatientMedicalVisitsAdapter(get_patient_fn=Mock(return_value=patient()),list_medical_visits_fn=listing)
        client = FakeLLMClient([ModelTurn(tool_calls=[
            ToolCall(id='visit',name='get_patient_medical_visits',arguments={}),
            ToolCall(id='search',name='search_evidence',arguments={'query':'Synthetic','top_k':1})]),chat_final(selected=[True])])
        result = ChatExecutionV1(client,partial(build_chat_registry,visits_reader=adapter,retriever=retriever))(
            CONTEXT,prepare_chat_input(CONTEXT,'진료 기록과 연구를 확인해 주세요.'))
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(result.execution.errors,[])
        messages = client.requests[1][0]
        self.assertEqual([m.call_id for m in messages if m.type=='function_call_output'],['visit','search'])
        self.assertEqual(sum(m.content==PATIENT_MEDICAL_VISITS_DATA_POLICY for m in messages),1)
        self.assertEqual(result.sources[0].source_ref.tool_call_id,'search')
