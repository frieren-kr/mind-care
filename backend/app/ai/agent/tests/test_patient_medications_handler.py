"""Medication Handler and production Chat assembly with offline SDK transport."""
from functools import partial
import json
from threading import Barrier
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import UUID

import httpx
from openai import OpenAI

from ..adapters.patient_medications import PatientMedicationsAdapter
from ..clients.openai_client import OpenAIClient
from ..composition import build_chat_registry, build_evidence_registry
from ..executor import ToolExecutor
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..schemas import ModelTurn, ToolCall
from ..testing.chat_workflow_fakes import chat_final
from ..testing.fake_llm import FakeLLMClient
from ..tools.evidence_tools import EvidenceRetriever
from ..tools.medications import (PATIENT_MEDICATIONS_DATA_POLICY, PatientMedicationsHandler,
                                 PatientMedicationsInput, PatientMedicationsOutput, PatientMedicationsReader)
from ..workflows.chat_context_prepare import prepare_chat_input
from ..workflows.chat_execution import ChatExecutionV1
from ..workflows.chat_prompts import CHAT_TOOL_POLICY
from .test_patient_medications_adapter import medication
from .test_patient_profile_adapter import patient
from .test_responses import function, response, response_fixture
from .test_workflows import CONTEXT


class PatientMedicationsHandlerTests(TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'psycopg.connect', 'app.ai.retrieval.embedding.get_model'):
            guard = patch(target, side_effect=AssertionError('Live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_handler_forwards_context_validated_status_limit_and_output_identity(self):
        reader = Mock(spec=PatientMedicationsReader)
        expected = PatientMedicationsOutput(medications=[], total_count=0)
        reader.get_patient_medications.return_value = expected
        for status in ('taking', 'stopped', 'all'):
            reader.reset_mock()
            result = PatientMedicationsHandler(reader)(CONTEXT, PatientMedicationsInput(status=status, limit=2))
            self.assertIs(result, expected)
            reader.get_patient_medications.assert_called_once_with(context=CONTEXT, status=status, limit=2)
            self.assertIs(reader.get_patient_medications.call_args.kwargs['context'], CONTEXT)

    def test_invalid_arguments_and_missing_patient_rejected_before_reader(self):
        reader = Mock(spec=PatientMedicationsReader)
        registry = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.PATIENT_MEDICATIONS], PatientMedicationsHandler(reader)),))
        executor = ToolExecutor(registry)
        for args in ({'status':'unknown'}, {'limit':True}, {'limit':31}, {'patient_id':'untrusted'}):
            outcome, _ = executor.execute(ToolCall(id='med', name='get_patient_medications', arguments=args), CONTEXT)
            self.assertEqual(outcome.error.code, 'invalid_arguments')
        outcome, _ = executor.execute(ToolCall(id='med', name='get_patient_medications', arguments={}),
                                     CONTEXT.model_copy(update={'patient_id':None}))
        self.assertEqual(outcome.error.code, 'patient_context_mismatch')
        reader.get_patient_medications.assert_not_called()

    def test_factory_uses_canonical_patient_only_tool_and_keeps_search_factory(self):
        reader = Mock(spec=PatientMedicationsReader)
        registry = build_chat_registry(CONTEXT, medications_reader=reader)
        self.assertEqual(set(registry.names()), {'search_evidence','search_guidelines','get_patient_profile','get_caregiver_profile',
                                                'get_recent_care_logs','get_patient_medications','get_patient_medical_visits','get_patient_assessments','get_patient_safety_events'})
        spec = registry.get('get_patient_medications')
        self.assertIs(spec.contract, TOOL_CONTRACTS[ToolName.PATIENT_MEDICATIONS])
        self.assertIs(spec.handler.reader, reader)
        self.assertTrue(spec.parallel_safe)
        self.assertFalse(spec.test_only)
        self.assertIsInstance(build_chat_registry(CONTEXT).get('get_patient_medications').handler.reader,
                              PatientMedicationsAdapter)
        self.assertFalse(build_chat_registry(CONTEXT, parallel_safe=False).get('get_patient_medications').parallel_safe)
        self.assertEqual(set(build_chat_registry(CONTEXT.model_copy(update={'patient_id':None})).names()),
                         {'search_evidence','search_guidelines','get_caregiver_profile'})
        self.assertEqual(build_evidence_registry().names(), ('search_evidence',))
        self.assertIn('일반적인 약물 정보 설명에는 불필요하게 호출하지 않는다', CHAT_TOOL_POLICY)

    def execute_sdk(self, adapter, *, context=CONTEXT, called=True, repeated=False):
        wire = []
        identifiers = ['med1', 'med2'] if repeated else ['med1']
        calls = [{**function(identifier, '{"status":"all","limit":1}'), 'name':'get_patient_medications'}
                 for identifier in identifiers]
        turns = ([response([call]) for call in calls] if called else []) + [response_fixture({'text':chat_final().text})]
        def transport(request):
            wire.append(json.loads(request.content))
            self.assertLessEqual(len(wire), len(turns))
            return httpx.Response(200, json=turns[len(wire)-1])
        unused = Mock(side_effect=AssertionError('No unrelated lookup'))
        retriever = Mock(spec=EvidenceRetriever)
        factory = partial(build_chat_registry, medications_reader=adapter, retriever=retriever,
                          patient_reader=unused, caregiver_reader=unused, care_logs_reader=unused)
        with OpenAI(api_key='offline-placeholder', max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
            result = ChatExecutionV1(OpenAIClient(sdk, model='offline'), factory)(context,
                prepare_chat_input(context, '앱에 등록된 약물 정보를 확인해 주세요.'))
        unused.get_patient_profile.assert_not_called()
        unused.get_caregiver_profile.assert_not_called()
        unused.get_recent_care_logs.assert_not_called()
        retriever.search_evidence.assert_not_called()
        return result, wire

    def test_sdk_empty_partial_and_repeated_success_preserve_data_policy_and_call_ids(self):
        for empty in (False, True):
            rows = [] if empty else [medication(), medication(is_taking=False)]
            lookup = Mock(return_value=patient())
            listing = Mock(return_value=rows)
            result, wire = self.execute_sdk(PatientMedicationsAdapter(get_patient_fn=lookup,
                list_medications_fn=listing), repeated=True)
            self.assertEqual(result.execution.status, 'completed')
            self.assertEqual(result.execution.errors, [])
            self.assertEqual(result.sources, [])
            self.assertEqual(lookup.call_count, 2)
            self.assertEqual(listing.call_count, 2)
            for call in listing.call_args_list:
                self.assertEqual(call.args, (CONTEXT.patient_id,))
                self.assertEqual(call.kwargs, {'is_taking':None})
            definition = next(d for d in wire[0]['tools'] if d['name']=='get_patient_medications')
            self.assertEqual(definition['description'], TOOL_CONTRACTS[ToolName.PATIENT_MEDICATIONS].description)
            self.assertNotIn(PATIENT_MEDICATIONS_DATA_POLICY, json.dumps(wire[0], ensure_ascii=False))
            for body in wire[1:]:
                messages = body['input']
                policies = [m for m in messages if m.get('content')==PATIENT_MEDICATIONS_DATA_POLICY]
                self.assertEqual(len(policies), 1)
                self.assertEqual(policies[0]['role'], 'system')
                first_output = next(m for m in messages if m.get('type')=='function_call_output' and m['call_id']=='med1')
                self.assertGreater(messages.index(policies[0]), messages.index(first_output))
                for output in (m for m in messages if m.get('type')=='function_call_output'):
                    call = next(m for m in messages if m.get('type')=='function_call' and m['call_id']==output['call_id'])
                    self.assertLess(messages.index(call), messages.index(output))
                    data = json.loads(output['output'])
                    self.assertTrue(data['success'])
                    self.assertEqual(data['data']['total_count'], len(rows))
                    self.assertEqual(len(data['data']['medications']), int(not empty))
                    if not empty:
                        self.assertEqual(data['data']['medications'][0]['note'], rows[0].note)
                        self.assertEqual(set(data['data']['medications'][0]),
                            {'drug_name','dosage','frequency','is_taking','start_date','end_date','note'})
                        self.assertFalse(any(rows[0].note in m.get('content','') for m in messages if m.get('role')=='system'))
                self.assertNotIn(str(CONTEXT.patient_id), json.dumps(body))
                self.assertNotIn(str(rows[0].id) if rows else 'NO_INTERNAL_ID', json.dumps(body))
            outputs = [m['call_id'] for m in wire[-1]['input'] if m.get('type')=='function_call_output']
            self.assertEqual(outputs, ['med1','med2'])

    def test_sdk_missing_patient_wrong_owner_invalid_rows_and_db_errors_have_no_policy(self):
        cases = [(None, []), (patient(caregiver_id=UUID(int=999)), []),
                 (patient(), None), (patient(), [medication(patient_id=UUID(int=999))]),
                 (patient(), [medication().model_copy(update={'is_taking':1})]),
                 (patient(), RuntimeError('PRIVATE DB error'))]
        for source, rows in cases:
            listing = Mock(side_effect=rows if isinstance(rows, Exception) else None, return_value=rows)
            result, wire = self.execute_sdk(PatientMedicationsAdapter(get_patient_fn=Mock(return_value=source),
                                                                      list_medications_fn=listing))
            self.assertEqual(result.execution.status, 'completed')
            output = next(m for m in wire[1]['input'] if m.get('type')=='function_call_output')
            envelope = json.loads(output['output'])
            self.assertFalse(envelope['success'])
            self.assertIsNone(envelope['data'])
            self.assertEqual(envelope['error']['code'], 'tool_exception')
            self.assertFalse(any(m.get('content')==PATIENT_MEDICATIONS_DATA_POLICY for b in wire for m in b['input']))
            self.assertNotIn('PRIVATE', json.dumps(wire))

    def test_sdk_timeout_uses_existing_terminal_error_without_followup_policy(self):
        listing = Mock(side_effect=TimeoutError('PRIVATE timeout'))
        result, wire = self.execute_sdk(PatientMedicationsAdapter(get_patient_fn=Mock(return_value=patient()),
                                                                  list_medications_fn=listing))
        self.assertEqual(result.execution.status, 'failed')
        self.assertEqual(result.execution.errors[-1].code, 'tool_timeout')
        self.assertEqual(len(wire), 1)
        listing.assert_called_once()

    def test_no_lookup_or_policy_for_general_question_or_unselected_patient(self):
        lookup, listing = Mock(), Mock()
        adapter = PatientMedicationsAdapter(get_patient_fn=lookup, list_medications_fn=listing)
        for selected, called in ((True, False), (False, False), (False, True)):
            context = CONTEXT if selected else CONTEXT.model_copy(update={'patient_id':None})
            result, wire = self.execute_sdk(adapter, context=context, called=called)
            self.assertEqual(result.execution.status, 'completed')
            self.assertFalse(any(m.get('content')==PATIENT_MEDICATIONS_DATA_POLICY for b in wire for m in b['input']))
        lookup.assert_not_called()
        listing.assert_not_called()

    def test_independent_medication_and_search_execute_in_parallel_with_original_linkage(self):
        from .test_feed_candidates import package
        barrier = Barrier(2)
        def list_rows(*args, **kwargs):
            barrier.wait(2)
            return [medication()]
        def search(**kwargs):
            barrier.wait(2)
            return package(['Synthetic PMID'])
        retriever = Mock(spec=EvidenceRetriever)
        retriever.search_evidence.side_effect = search
        adapter = PatientMedicationsAdapter(get_patient_fn=Mock(return_value=patient()), list_medications_fn=list_rows)
        client = FakeLLMClient([ModelTurn(tool_calls=[
            ToolCall(id='med', name='get_patient_medications', arguments={}),
            ToolCall(id='search', name='search_evidence', arguments={'query':'Synthetic','top_k':1})]),
            chat_final(selected=[True])])
        result = ChatExecutionV1(client, partial(build_chat_registry, medications_reader=adapter, retriever=retriever))(
            CONTEXT, prepare_chat_input(CONTEXT, '등록된 약물과 연구를 확인해 주세요.'))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(result.execution.errors, [])
        messages = client.requests[1][0]
        self.assertEqual([m.call_id for m in messages if m.type=='function_call_output'], ['med','search'])
        self.assertEqual(sum(m.content==PATIENT_MEDICATIONS_DATA_POLICY for m in messages), 1)
        self.assertEqual(result.sources[0].source_ref.tool_call_id, 'search')
