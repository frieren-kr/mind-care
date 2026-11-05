"""Rolling-window Handler and Chat assembly over an injected future DB boundary."""
from datetime import datetime, timedelta, timezone
from functools import partial
import json
from threading import Barrier
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from openai import OpenAI

from ..adapters.recent_care_logs import RecentCareLogsAdapter
from ..clients.openai_client import OpenAIClient
from ..composition import build_chat_registry, build_evidence_registry
from ..executor import ToolExecutor
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..testing.chat_workflow_fakes import chat_call, chat_final
from ..testing.fake_llm import FakeLLMClient
from ..schemas import ModelTurn
from ..tools.contracts import RecentCareLogsInput, RecentCareLogsOutput, RecentCareLogsPeriod
from ..tools.evidence_tools import EvidenceRetriever
from ..tools.recent_care_logs import RECENT_CARE_LOGS_DATA_POLICY, RecentCareLogsHandler, RecentCareLogsReader
from ..workflows.chat_context_prepare import prepare_chat_input
from ..workflows.chat_execution import ChatExecutionV1
from .test_recent_care_logs_adapter import START, END, collection, row
from .test_responses import function, response, response_fixture
from .test_workflows import CONTEXT


class RecentCareLogsHandlerTests(TestCase):
    def setUp(self):
        for target in ('socket.socket.connect','psycopg.connect','app.ai.retrieval.embedding.get_model'):
            guard=patch(target,side_effect=AssertionError('Live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_clock_read_once_and_exact_utc_window_forwarded_with_context_and_limit(self):
        fixed=datetime(2026,11,5,2,30,tzinfo=timezone(timedelta(hours=9)))
        for days in (1,14,90):
            clock=Mock(return_value=fixed)
            reader=Mock(spec=RecentCareLogsReader)
            expected=RecentCareLogsOutput(period=RecentCareLogsPeriod(start_at=START,end_at=END),logs=[],total_count=0)
            reader.get_recent_care_logs.return_value=expected
            result=RecentCareLogsHandler(reader,now_fn=clock)(CONTEXT,RecentCareLogsInput(days=days,limit=2))
            clock.assert_called_once_with()
            reader.get_recent_care_logs.assert_called_once_with(context=CONTEXT,
                start_at=fixed.astimezone(timezone.utc)-timedelta(hours=days*24),
                end_at=fixed.astimezone(timezone.utc),limit=2)
            self.assertIs(reader.get_recent_care_logs.call_args.kwargs['context'],CONTEXT)
            self.assertIs(result,expected)
            self.assertIs(reader.get_recent_care_logs.call_args.kwargs['end_at'].tzinfo,timezone.utc)

    def test_both_dst_transitions_use_elapsed_24_hours_instead_of_local_date_subtraction(self):
        ny=ZoneInfo('America/New_York')
        for fixed in (datetime(2026,3,8,3,30,tzinfo=ny),datetime(2026,11,1,1,30,tzinfo=ny,fold=1)):
            clock=Mock(return_value=fixed);reader=Mock(spec=RecentCareLogsReader)
            RecentCareLogsHandler(reader,now_fn=clock)(CONTEXT,RecentCareLogsInput(days=1))
            args=reader.get_recent_care_logs.call_args.kwargs
            self.assertEqual(args['end_at']-args['start_at'],timedelta(hours=24))
            self.assertEqual(args['end_at'],fixed.astimezone(timezone.utc))
            self.assertNotEqual(args['start_at'],(fixed-timedelta(days=1)).astimezone(timezone.utc))
            clock.assert_called_once_with()

    def test_invalid_clock_fails_before_reader_and_is_not_silently_localized(self):
        for value in (datetime(2026,10,1),None,'not-a-time'):
            reader=Mock(spec=RecentCareLogsReader);clock=Mock(return_value=value)
            with self.subTest(value=value),self.assertRaises(ValueError):
                RecentCareLogsHandler(reader,now_fn=clock)(CONTEXT,RecentCareLogsInput())
            clock.assert_called_once_with()
            reader.get_recent_care_logs.assert_not_called()

    def test_executor_validates_input_before_clock_or_reader(self):
        reader=Mock(spec=RecentCareLogsReader);clock=Mock(return_value=END)
        handler=RecentCareLogsHandler(reader,now_fn=clock)
        tools=ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.RECENT_CARE_LOGS],handler),))
        for args in ({'days':0},{'limit':31},{'days':True},{'patient_id':'untrusted'}):
            outcome,_=ToolExecutor(tools).execute(chat_call(ToolName.RECENT_CARE_LOGS,args=args),CONTEXT)
            self.assertEqual(outcome.error.code,'invalid_arguments')
        clock.assert_not_called()
        reader.get_recent_care_logs.assert_not_called()

    def test_selected_patient_registry_adds_canonical_logs_but_user_scope_omits_it(self):
        tools=build_chat_registry(CONTEXT)
        self.assertEqual(set(tools.names()),{'search_evidence','get_patient_profile','get_caregiver_profile','get_recent_care_logs','get_patient_medications','get_patient_medical_visits','get_patient_assessments'})
        spec=tools.get('get_recent_care_logs')
        self.assertIs(spec.contract,TOOL_CONTRACTS[ToolName.RECENT_CARE_LOGS])
        self.assertTrue(spec.parallel_safe)
        self.assertFalse(spec.test_only)
        self.assertIsInstance(spec.handler,RecentCareLogsHandler)
        self.assertIsInstance(spec.handler.reader,RecentCareLogsAdapter)
        self.assertIsNone(spec.handler.reader.query_fn)
        self.assertEqual(set(build_chat_registry(CONTEXT.model_copy(update={'patient_id':None})).names()),
                         {'search_evidence','get_caregiver_profile'})
        self.assertFalse(build_chat_registry(CONTEXT,parallel_safe=False).get('get_recent_care_logs').parallel_safe)
        self.assertEqual(build_evidence_registry().names(),('search_evidence',))

    def execute_sdk(self,query_fn,*,context=CONTEXT,called=True):
        wire=[]
        call={**function('logs','{"days":1,"limit":2}'),'name':'get_recent_care_logs'}
        final=response_fixture({'text':chat_final('실제 반환 범위의 합성 설명').text})
        turns=[response([call]),final] if called else [final]
        def transport(request):
            wire.append(json.loads(request.content));self.assertLessEqual(len(wire),len(turns))
            return httpx.Response(200,json=turns[len(wire)-1])
        patient_reader=Mock(side_effect=AssertionError('No patient profile lookup'))
        caregiver_reader=Mock(side_effect=AssertionError('No caregiver profile lookup'))
        retriever=Mock(spec=EvidenceRetriever)
        factory=partial(build_chat_registry,retriever=retriever,patient_reader=patient_reader,
                        caregiver_reader=caregiver_reader,care_logs_reader=RecentCareLogsAdapter(query_fn=query_fn))
        with OpenAI(api_key='offline-placeholder',max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
            result=ChatExecutionV1(OpenAIClient(sdk,model='offline'),factory)(
                context,prepare_chat_input(context,'최근 돌봄 기록을 확인해 주세요.'))
        retriever.search_evidence.assert_not_called()
        patient_reader.get_patient_profile.assert_not_called()
        caregiver_reader.get_caregiver_profile.assert_not_called()
        return result,wire

    def test_sdk_success_empty_and_partial_results_deliver_policy_after_original_call_output(self):
        for empty in (False,True):
            def query(**kwargs):
                return collection([] if empty else [row(logged_at=kwargs['end_at']-timedelta(hours=1)),
                    row(logged_at=kwargs['start_at'],content=None,mood_tag=None)],0 if empty else 12)
            lookup=Mock(side_effect=query)
            result,wire=self.execute_sdk(lookup)
            self.assertEqual(result.execution.status,'completed')
            self.assertEqual(result.execution.errors,[])
            lookup.assert_called_once()
            args=lookup.call_args.kwargs
            self.assertEqual(set(args),{'user_id','patient_id','start_at','end_at','limit'})
            self.assertEqual(args['user_id'],CONTEXT.user_id)
            self.assertEqual(args['patient_id'],CONTEXT.patient_id)
            self.assertEqual(args['limit'],2)
            self.assertEqual(args['end_at']-args['start_at'],timedelta(hours=24))
            self.assertFalse(any(m.get('content')==RECENT_CARE_LOGS_DATA_POLICY for m in wire[0]['input']))
            messages=wire[1]['input']
            output=next(m for m in messages if m.get('type')=='function_call_output' and m['call_id']=='logs')
            value=json.loads(output['output'])
            self.assertTrue(value['success'])
            self.assertEqual(value['data']['total_count'],0 if empty else 12)
            self.assertEqual(len(value['data']['logs']),0 if empty else 2)
            self.assertEqual(value['data']['period'],{'start_at':args['start_at'].isoformat().replace('+00:00','Z'),
                                                     'end_at':args['end_at'].isoformat().replace('+00:00','Z')})
            policies=[m for m in messages if m.get('content')==RECENT_CARE_LOGS_DATA_POLICY]
            self.assertEqual(len(policies),1)
            self.assertEqual(policies[0]['role'],'system')
            self.assertGreater(messages.index(policies[0]),messages.index(output))
            for body in wire:
                self.assertNotIn(str(CONTEXT.user_id),json.dumps(body))
                self.assertNotIn(str(CONTEXT.patient_id),json.dumps(body))
            if not empty:
                self.assertEqual(set(value['data']['logs'][0]),{'logged_at','content','mood_tag'})
                self.assertEqual(value['data']['logs'][0]['content'],row().content)
                self.assertFalse(any(row().content in m.get('content','') for m in messages if m.get('role')=='system'))

    def test_missing_binding_source_errors_and_bad_scope_are_failures_not_empty_success(self):
        queries=[None, Mock(side_effect=PermissionError('PRIVATE denied')),
                 Mock(side_effect=RuntimeError('PRIVATE DB error')),
                 Mock(side_effect=lambda **k:collection([row(user_id=UUID(int=987),logged_at=k['start_at'])]))]
        for query in queries:
            with self.subTest(unconfigured=query is None):
                result,wire=self.execute_sdk(query)
                self.assertEqual(result.execution.status,'completed')
                output=next(m for m in wire[1]['input'] if m.get('type')=='function_call_output')
                value=json.loads(output['output'])
                self.assertFalse(value['success'])
                self.assertIsNone(value['data'])
                self.assertEqual(value['error']['code'],'tool_exception')
                self.assertFalse(any(m.get('content')==RECENT_CARE_LOGS_DATA_POLICY for body in wire for m in body['input']))
                self.assertNotIn('PRIVATE',json.dumps(wire))
                if query is not None:query.assert_called_once()

    def test_db_timeout_uses_existing_executor_failure_and_never_becomes_empty(self):
        query=Mock(side_effect=TimeoutError('PRIVATE timeout'))
        result,wire=self.execute_sdk(query)
        self.assertEqual(result.execution.status,'failed')
        self.assertEqual(result.execution.errors[-1].code,'tool_timeout')
        self.assertEqual(len(wire),1)
        query.assert_called_once()
        self.assertFalse(any(m.get('content')==RECENT_CARE_LOGS_DATA_POLICY for m in wire[0]['input']))

    def test_no_patient_or_no_tool_call_never_queries_or_delivers_policy(self):
        for selected,called in ((False,False),(False,True),(True,False)):
            query=Mock(side_effect=AssertionError('No lookup expected'))
            context=CONTEXT if selected else CONTEXT.model_copy(update={'patient_id':None})
            result,wire=self.execute_sdk(query,context=context,called=called)
            self.assertEqual(result.execution.status,'completed')
            query.assert_not_called()
            self.assertEqual('get_recent_care_logs' in {d['name'] for d in wire[0]['tools']},selected)
            self.assertFalse(any(m.get('content')==RECENT_CARE_LOGS_DATA_POLICY for body in wire for m in body['input']))

    def test_injected_logs_and_research_execute_in_parallel_with_original_call_linkage(self):
        from .test_feed_candidates import package
        barrier=Barrier(2)
        def query(**kwargs):
            barrier.wait(2)
            return collection([row(logged_at=kwargs['start_at'])])
        def retrieve(**kwargs):
            barrier.wait(2)
            return package(['Synthetic PMID'])
        retriever=Mock(spec=EvidenceRetriever)
        retriever.search_evidence.side_effect=retrieve
        lookup=Mock(side_effect=query)
        factory=partial(build_chat_registry,retriever=retriever,
                        care_logs_reader=RecentCareLogsAdapter(query_fn=lookup))
        calls=[chat_call(ToolName.RECENT_CARE_LOGS,identifier='logs',args={'days':1,'limit':2}),
               chat_call(ToolName.SEARCH_EVIDENCE,identifier='search',args={'query':'Synthetic','top_k':1})]
        client=FakeLLMClient([ModelTurn(tool_calls=calls),chat_final(selected=[True])])
        result=ChatExecutionV1(client,factory)(CONTEXT,prepare_chat_input(CONTEXT,'돌봄 기록과 연구를 확인해 주세요.'))
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(result.execution.errors,[])
        self.assertEqual([m.call_id for m in client.requests[1][0] if m.type=='function_call_output'],['logs','search'])
        self.assertEqual(sum(m.content==RECENT_CARE_LOGS_DATA_POLICY for m in client.requests[1][0]),1)
        self.assertEqual(result.sources[0].source_ref.tool_call_id,'search')
        lookup.assert_called_once()
