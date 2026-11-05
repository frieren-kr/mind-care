"""Calendar resolution and production Chat safety messages with offline boundaries."""
from datetime import date, datetime, timedelta, timezone
from functools import partial
import json
from threading import Barrier
from unittest import TestCase
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI

from app import user_schemas
from ..adapters.patient_safety_events import PatientSafetyEventsAdapter
from ..clients.openai_client import OpenAIClient
from ..composition import build_chat_registry, build_evidence_registry
from ..executor import ToolExecutor
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..schemas import ModelTurn, ToolCall
from ..testing.chat_workflow_fakes import chat_final
from ..testing.fake_llm import FakeLLMClient
from ..tools.evidence_tools import EvidenceRetriever
from ..tools.safety_events import (PATIENT_SAFETY_EVENTS_DATA_POLICY, PatientSafetyEventsHandler,
    PatientSafetyEventsInput, PatientSafetyEventsOutput, PatientSafetyEventsReader, SafetyEventsPeriod,
    resolve_safety_period)
from ..workflows.chat_context_prepare import prepare_chat_input
from ..workflows.chat_execution import ChatExecutionV1
from ..workflows.chat_prompts import CHAT_TOOL_POLICY
from .test_patient_safety_events_adapter import TODAY, event
from .test_patient_profile_adapter import patient
from .test_responses import function, response, response_fixture
from .test_workflows import CONTEXT


class PatientSafetyEventsHandlerTests(TestCase):
    def setUp(self):
        for target in ('socket.socket.connect','psycopg.connect','app.ai.retrieval.embedding.get_model'):
            guard = patch(target,side_effect=AssertionError('Live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_recent_days_are_inclusive_calendar_dates_with_default_and_year_boundary(self):
        for args,reference,start,end in (({},TODAY,date(2026,10,7),TODAY),
            ({'days':1},TODAY,TODAY,TODAY),({'days':31},TODAY,date(2026,10,6),TODAY),
            ({},date(2026,1,1),date(2025,12,3),date(2026,1,1))):
            result = resolve_safety_period(PatientSafetyEventsInput(**args),reference)
            self.assertEqual((result.start_date,result.end_date),(start,end))

    def test_relative_month_zero_year_rollover_and_gregorian_leap_boundaries(self):
        for reference,ago,start,end in ((date(2026,1,15),0,date(2026,1,1),date(2026,1,31)),
            (date(2026,1,15),1,date(2025,12,1),date(2025,12,31)),
            (date(2026,1,15),13,date(2024,12,1),date(2024,12,31)),
            (date(2024,3,1),1,date(2024,2,1),date(2024,2,29)),
            (date(2025,3,1),1,date(2025,2,1),date(2025,2,28)),
            (date(1900,3,1),1,date(1900,2,1),date(1900,2,28)),
            (date(2000,3,1),1,date(2000,2,1),date(2000,2,29))):
            period = resolve_safety_period(PatientSafetyEventsInput(mode='relative_month',months_ago=ago),reference)
            self.assertEqual((period.start_date,period.end_date),(start,end))

    def test_specific_month_and_extreme_years_keep_full_current_month(self):
        for args,reference,start,end in (({'mode':'specific_month','year':2024,'month':2},TODAY,date(2024,2,1),date(2024,2,29)),
            ({'mode':'relative_month','months_ago':0},TODAY,date(2026,11,1),date(2026,11,30)),
            ({'mode':'specific_month','year':1,'month':1},date.min,date(1,1,1),date(1,1,31)),
            ({'mode':'specific_month','year':9999,'month':12},date.max,date(9999,12,1),date.max),
            ({'mode':'relative_month','months_ago':119987},date.max,date(1,1,1),date(1,1,31))):
            period = resolve_safety_period(PatientSafetyEventsInput(**args),reference)
            self.assertEqual((period.start_date,period.end_date),(start,end))

    def test_future_month_and_date_underflow_fail_before_reader_without_clipping(self):
        for args,reference in (({'mode':'specific_month','year':2026,'month':12},TODAY),
            ({'mode':'specific_month','year':2027,'month':1},TODAY),
            ({'mode':'relative_month','months_ago':119988},date.max),
            ({'mode':'relative_month','months_ago':2**256},TODAY),({'days':2},date.min)):
            reader = Mock(spec=PatientSafetyEventsReader)
            clock = Mock(return_value=reference)
            with self.subTest(args=args),self.assertRaises((ValueError,OverflowError)):
                PatientSafetyEventsHandler(reader,today_fn=clock)(CONTEXT,PatientSafetyEventsInput(**args))
            clock.assert_called_once_with()
            reader.get_patient_safety_events.assert_not_called()

    def test_handler_reads_clock_once_for_every_mode_and_checks_returned_period(self):
        cases = (({'days':1},TODAY,TODAY),
                 ({'mode':'relative_month','months_ago':0},date(2026,11,1),date(2026,11,30)),
                 ({'mode':'specific_month','year':2024,'month':2},date(2024,2,1),date(2024,2,29)))
        for args,start,end in cases:
            expected = PatientSafetyEventsOutput(period=SafetyEventsPeriod(start_date=start,end_date=end),events=[])
            reader = Mock(spec=PatientSafetyEventsReader)
            reader.get_patient_safety_events.return_value = expected
            clock = Mock(return_value=TODAY)
            result = PatientSafetyEventsHandler(reader,today_fn=clock)(CONTEXT,PatientSafetyEventsInput(**args))
            self.assertIs(result,expected)
            clock.assert_called_once_with()
            reader.get_patient_safety_events.assert_called_once_with(context=CONTEXT,start_date=start,end_date=end)
            self.assertIs(reader.get_patient_safety_events.call_args.kwargs['context'],CONTEXT)
        reader.get_patient_safety_events.return_value = PatientSafetyEventsOutput(
            period=SafetyEventsPeriod(start_date=TODAY,end_date=TODAY),events=[])
        with self.assertRaisesRegex(ValueError,'period mismatch'):
            PatientSafetyEventsHandler(reader,today_fn=lambda:TODAY)(CONTEXT,PatientSafetyEventsInput())

    def test_default_server_today_handles_kst_midnight_and_rejects_invalid_clock(self):
        for instant,day in ((datetime(2026,11,4,14,59,59,tzinfo=timezone.utc),date(2026,11,4)),
                            (datetime(2026,11,4,15,tzinfo=timezone.utc),TODAY)):
            reader = Mock(spec=PatientSafetyEventsReader)
            reader.get_patient_safety_events.return_value = PatientSafetyEventsOutput(
                period=SafetyEventsPeriod(start_date=day,end_date=day),events=[])
            with patch.object(user_schemas,'datetime',wraps=datetime) as clock:
                clock.now.side_effect = lambda tz:instant.astimezone(tz)
                PatientSafetyEventsHandler(reader)(CONTEXT,PatientSafetyEventsInput(days=1))
                clock.now.assert_called_once_with(user_schemas.APP_TIMEZONE)
        for value in (None,'2026-11-05',datetime(2026,11,5)):
            reader = Mock(spec=PatientSafetyEventsReader)
            with self.assertRaises(ValueError):
                PatientSafetyEventsHandler(reader,today_fn=lambda:value)(CONTEXT,PatientSafetyEventsInput())
            reader.get_patient_safety_events.assert_not_called()

    def test_input_validation_precedes_clock_and_executor_revalidates_reader_output(self):
        reader = Mock(spec=PatientSafetyEventsReader)
        clock = Mock(return_value=TODAY)
        executor = ToolExecutor(ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.PATIENT_SAFETY_EVENTS],
            PatientSafetyEventsHandler(reader,today_fn=clock)),)))
        for args in ({'days':None},{'days':True},{'mode':'relative_month','months_ago':0,'days':30},
                     {'mode':'specific_month','year':2026},{'limit':31},{'patient_id':'untrusted'}):
            outcome,_ = executor.execute(ToolCall(id='safety',name='get_patient_safety_events',arguments=args),CONTEXT)
            self.assertEqual(outcome.error.code,'invalid_arguments')
        clock.assert_not_called()
        reader.get_patient_safety_events.assert_not_called()
        good = PatientSafetyEventsOutput(period=SafetyEventsPeriod(start_date=TODAY,end_date=TODAY),
            events=[{'event_date':TODAY,'has_fall':False,'has_wandering':False,'has_missing':False,'note':None}])
        good.events[0].has_fall = 1
        reader.get_patient_safety_events.return_value = good
        outcome,trace = executor.execute(ToolCall(id='safety',name='get_patient_safety_events',arguments={'days':1}),CONTEXT)
        self.assertEqual(outcome.error.code,'invalid_output')
        self.assertIsNone(trace.result_count)

    def test_factory_registers_canonical_patient_only_safety_and_keeps_existing_tools(self):
        reader = Mock(spec=PatientSafetyEventsReader)
        tools = build_chat_registry(CONTEXT,safety_events_reader=reader)
        self.assertEqual(set(tools.names()),{'search_evidence','get_patient_profile','get_caregiver_profile',
            'get_recent_care_logs','get_patient_medications','get_patient_medical_visits','get_patient_assessments','get_patient_safety_events'})
        spec = tools.get('get_patient_safety_events')
        self.assertIs(spec.contract,TOOL_CONTRACTS[ToolName.PATIENT_SAFETY_EVENTS])
        self.assertIs(spec.handler.reader,reader)
        self.assertTrue(spec.parallel_safe)
        self.assertFalse(spec.test_only)
        self.assertIsInstance(build_chat_registry(CONTEXT).get('get_patient_safety_events').handler.reader,PatientSafetyEventsAdapter)
        self.assertFalse(build_chat_registry(CONTEXT,parallel_safe=False).get('get_patient_safety_events').parallel_safe)
        self.assertEqual(set(build_chat_registry(CONTEXT.model_copy(update={'patient_id':None})).names()),{'search_evidence','get_caregiver_profile'})
        self.assertEqual(build_evidence_registry().names(),('search_evidence',))
        self.assertIn('긴급한 안전 대응이 필요한 상황에서는 조회보다 즉각적인 안전 안내를 우선한다',CHAT_TOOL_POLICY)

    def execute_sdk(self,adapter,*,args=None,called=True,repeated=False,context=CONTEXT):
        wire = []
        calls = [{**function(identifier,json.dumps(args or {})),'name':'get_patient_safety_events'}
                 for identifier in (('safety1','safety2') if repeated else ('safety1',))]
        turns = ([response([call]) for call in calls] if called else []) + [response_fixture({'text':chat_final().text})]
        def transport(request):
            wire.append(json.loads(request.content))
            self.assertLessEqual(len(wire),len(turns))
            return httpx.Response(200,json=turns[len(wire)-1])
        unused = Mock(side_effect=AssertionError('No unrelated lookup'))
        retriever = Mock(spec=EvidenceRetriever)
        factory = partial(build_chat_registry,safety_events_reader=adapter,retriever=retriever,
            patient_reader=unused,caregiver_reader=unused,care_logs_reader=unused,medications_reader=unused,
            visits_reader=unused,assessments_reader=unused)
        with patch.object(user_schemas,'today',return_value=TODAY), \
             OpenAI(api_key='offline-placeholder',max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
            result = ChatExecutionV1(OpenAIClient(sdk,model='offline'),factory)(context,
                prepare_chat_input(context,'앱에 저장된 날짜별 안전 체크 기록을 확인해 주세요.'))
        for name in ('get_patient_profile','get_caregiver_profile','get_recent_care_logs','get_patient_medications',
                     'get_patient_medical_visits','get_patient_assessments'):
            getattr(unused,name).assert_not_called()
        retriever.search_evidence.assert_not_called()
        return result,wire

    def test_sdk_all_modes_empty_and_repeated_success_keep_period_flags_policy_and_count(self):
        cases = ({},{'days':1},{'mode':'relative_month','months_ago':0},{'mode':'specific_month','year':2024,'month':2})
        for args in cases:
            for empty in (False,True):
                raw_rows = []
                def query(key,**kwargs):
                    self.assertEqual(key,CONTEXT.patient_id)
                    self.assertEqual(kwargs['limit'],31)
                    first = min(kwargs['end_date'],TODAY)
                    raw_rows[:] = [] if empty else [event(event_date=first)]
                    if not empty and kwargs['start_date'] < first:
                        raw_rows.append(event(event_date=kwargs['start_date'],has_missing=True,note=None))
                    return raw_rows
                listing = Mock(side_effect=query)
                result,wire = self.execute_sdk(PatientSafetyEventsAdapter(get_patient_fn=Mock(return_value=patient()),
                    list_safety_events_fn=listing),args=args,repeated=True)
                self.assertEqual(result.execution.status,'completed')
                self.assertEqual(result.execution.errors,[])
                self.assertEqual(result.sources,[])
                self.assertEqual(listing.call_count,2)
                self.assertEqual([t.result_count for t in result.execution.called_tools],[len(raw_rows)]*2)
                self.assertNotIn(PATIENT_SAFETY_EVENTS_DATA_POLICY,json.dumps(wire[0],ensure_ascii=False))
                for body in wire[1:]:
                    messages = body['input']
                    policies = [m for m in messages if m.get('content')==PATIENT_SAFETY_EVENTS_DATA_POLICY]
                    self.assertEqual(len(policies),1)
                    self.assertEqual(policies[0]['role'],'system')
                    outputs = [m for m in messages if m.get('type')=='function_call_output']
                    self.assertGreater(messages.index(policies[0]),messages.index(outputs[0]))
                    for output in outputs:
                        call = next(m for m in messages if m.get('type')=='function_call' and m['call_id']==output['call_id'])
                        self.assertLess(messages.index(call),messages.index(output))
                        value = json.loads(output['output'])
                        self.assertTrue(value['success'])
                        data = value['data']
                        self.assertEqual(set(data),{'period','events'})
                        bounds = listing.call_args.kwargs
                        self.assertEqual(data['period'],{'start_date':bounds['start_date'].isoformat(),'end_date':bounds['end_date'].isoformat()})
                        self.assertEqual(data['events'],[{k:v for k,v in row.model_dump(mode='json').items()
                            if k in ('event_date','has_fall','has_wandering','has_missing','note')} for row in raw_rows])
                        if not empty:
                            self.assertFalse(data['events'][0]['has_fall'])
                            self.assertFalse(any(raw_rows[0].note in m.get('content','') for m in messages if m.get('role')=='system'))
                    self.assertNotIn(str(CONTEXT.patient_id),json.dumps(body))
                    self.assertNotIn(str(event().id),json.dumps(body))
                self.assertEqual([m['call_id'] for m in wire[-1]['input'] if m.get('type')=='function_call_output'],['safety1','safety2'])

    def test_sdk_failures_and_future_month_requests_are_not_empty_success_or_policy(self):
        missing = event().model_dump()
        del missing['note']
        cases = ((None,{}),([missing],{}),(RuntimeError('PRIVATE DB'),{}),(PermissionError('PRIVATE denied'),{}),
                 ([event().model_copy(update={'has_fall':1})],{}),
                 ([],{'mode':'specific_month','year':2026,'month':12}))
        for raw,args in cases:
            listing = Mock(return_value=raw,side_effect=raw if isinstance(raw,Exception) else None)
            result,wire = self.execute_sdk(PatientSafetyEventsAdapter(get_patient_fn=Mock(return_value=patient()),
                list_safety_events_fn=listing),args=args)
            self.assertEqual(result.execution.status,'completed')
            output = next(m for m in wire[1]['input'] if m.get('type')=='function_call_output')
            value = json.loads(output['output'])
            self.assertFalse(value['success'])
            self.assertIsNone(value['data'])
            self.assertEqual(value['error']['code'],'tool_exception')
            self.assertIsNone(result.execution.called_tools[0].result_count)
            self.assertFalse(any(m.get('content')==PATIENT_SAFETY_EVENTS_DATA_POLICY for b in wire for m in b['input']))
            self.assertNotIn('PRIVATE',json.dumps(wire))
            if args:listing.assert_not_called()

    def test_sdk_timeout_is_terminal_without_followup_policy(self):
        listing = Mock(side_effect=TimeoutError('PRIVATE timeout'))
        result,wire = self.execute_sdk(PatientSafetyEventsAdapter(get_patient_fn=Mock(return_value=patient()),list_safety_events_fn=listing))
        self.assertEqual(result.execution.status,'failed')
        self.assertEqual(result.execution.errors[-1].code,'tool_timeout')
        self.assertEqual(len(wire),1)
        listing.assert_called_once()

    def test_no_lookup_or_policy_for_general_question_or_unselected_patient(self):
        lookup,listing = Mock(),Mock()
        adapter = PatientSafetyEventsAdapter(get_patient_fn=lookup,list_safety_events_fn=listing)
        for selected,called in ((True,False),(False,False),(False,True)):
            context = CONTEXT if selected else CONTEXT.model_copy(update={'patient_id':None})
            result,wire = self.execute_sdk(adapter,context=context,called=called)
            self.assertEqual(result.execution.status,'completed')
            self.assertFalse(any(m.get('content')==PATIENT_SAFETY_EVENTS_DATA_POLICY for b in wire for m in b['input']))
        lookup.assert_not_called()
        listing.assert_not_called()

    def test_parallel_safety_and_search_keep_provider_ids_and_research_source(self):
        from .test_feed_candidates import package
        barrier = Barrier(2)
        def listing(key,**kwargs):
            barrier.wait(2)
            return [event()]
        def search(**kwargs):
            barrier.wait(2)
            return package(['Synthetic PMID'])
        retriever = Mock(spec=EvidenceRetriever)
        retriever.search_evidence.side_effect = search
        adapter = PatientSafetyEventsAdapter(get_patient_fn=Mock(return_value=patient()),list_safety_events_fn=listing)
        client = FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id='safety',name='get_patient_safety_events',arguments={}),
            ToolCall(id='search',name='search_evidence',arguments={'query':'Synthetic','top_k':1})]),chat_final(selected=[True])])
        with patch.object(user_schemas,'today',return_value=TODAY):
            result = ChatExecutionV1(client,partial(build_chat_registry,safety_events_reader=adapter,retriever=retriever))(
                CONTEXT,prepare_chat_input(CONTEXT,'안전 체크와 연구를 확인해 주세요.'))
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(result.execution.errors,[])
        messages = client.requests[1][0]
        self.assertEqual([m.call_id for m in messages if m.type=='function_call_output'],['safety','search'])
        self.assertEqual(sum(m.content==PATIENT_SAFETY_EVENTS_DATA_POLICY for m in messages),1)
        self.assertEqual(result.sources[0].source_ref.tool_call_id,'search')
