"""Medical visit declarations and existing offline execution boundaries."""
from dataclasses import replace
from datetime import date
import json
import unittest
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI
from pydantic import ValidationError

from ..clients.openai_client import OpenAIClient
from ..executor import ToolExecutor
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec, production_registry
from ..schemas import ModelTurn, ToolCall
from ..testing.chat_workflow_fakes import chat_final
from ..testing.fake_llm import FakeLLMClient
from ..tools.medical_visits import (
    MedicalVisitItem, PatientMedicalVisitsInput, PatientMedicalVisitsOutput,
    PATIENT_MEDICAL_VISITS_DATA_POLICY,
)
from ..tools.medications import PATIENT_MEDICATIONS_DATA_POLICY
from ..workflows.chat_context_prepare import prepare_chat_input
from ..workflows.chat_execution import ChatExecutionV1
from .test_responses import function, response, response_fixture
from .test_workflows import CONTEXT

CONTRACT = TOOL_CONTRACTS[ToolName.PATIENT_MEDICAL_VISITS]
TEXT_FIELDS = {'hospital_name', 'department', 'visit_reason', 'diagnosis', 'treatment_content',
               'test_summary', 'medication_change', 'doctor_note', 'follow_up_plan', 'visit_content'}
ROW = {'visit_date': '2026-01-02', 'is_visited': True}


class MedicalVisitsContractTests(unittest.TestCase):
    def test_input_exact_fields_defaults_and_statuses(self):
        self.assertEqual(PatientMedicalVisitsInput().model_dump(),
                         {'status': 'completed', 'start_date': None, 'end_date': None, 'limit': 5})
        for status in ('completed', 'upcoming', 'unvisited', 'all'):
            self.assertEqual(PatientMedicalVisitsInput(status=status).status, status)
        for status in ('visited', '', None, True, 1):
            with self.subTest(status=status), self.assertRaises(ValidationError):
                PatientMedicalVisitsInput(status=status)

    def test_strict_limit_bounds(self):
        for limit in (1, 20):
            self.assertEqual(PatientMedicalVisitsInput(limit=limit).limit, limit)
        for limit in (0, 21, True, False, 1.5, 5.0, '5', None):
            with self.subTest(limit=limit), self.assertRaises(ValidationError):
                PatientMedicalVisitsInput(limit=limit)

    def test_inclusive_dates_single_bounds_and_future_dates(self):
        for bounds in ({'start_date': '1900-01-01'}, {'end_date': '2099-12-31'},
                       {'start_date': '2026-01-02', 'end_date': '2026-01-02'},
                       {'start_date': '1900-01-01', 'end_date': '2099-12-31'}):
            for status in ('completed', 'upcoming', 'unvisited', 'all'):
                parsed = PatientMedicalVisitsInput(status=status, **bounds)
                for name, value in bounds.items():
                    self.assertEqual(getattr(parsed, name), date.fromisoformat(value))
        for bounds in ({'start_date': '2026-02-01', 'end_date': '2026-01-01'},
                       {'start_date': 'not-a-date'}, {'end_date': '2026-02-30'}):
            with self.subTest(bounds=bounds), self.assertRaises(ValidationError):
                PatientMedicalVisitsInput(**bounds)

    def test_input_forbids_identity_reference_date_and_unknown_fields(self):
        for name in ('patient_id', 'user_id', 'reference_date', 'unexpected'):
            with self.subTest(field=name), self.assertRaises(ValidationError):
                PatientMedicalVisitsInput.model_validate({name: 'synthetic'})
        schema = PatientMedicalVisitsInput.model_json_schema()
        self.assertEqual(set(schema['properties']), {'status', 'start_date', 'end_date', 'limit'})
        self.assertFalse(schema['additionalProperties'])

    def test_item_exact_fields_required_values_and_nullable_defaults(self):
        self.assertEqual(set(MedicalVisitItem.model_fields), TEXT_FIELDS | {'visit_date', 'is_visited'})
        self.assertEqual(MedicalVisitItem.model_json_schema()['required'], ['visit_date', 'is_visited'])
        self.assertTrue(all(f.description for f in MedicalVisitItem.model_fields.values()))
        row = MedicalVisitItem(**ROW)
        for name in TEXT_FIELDS:
            self.assertIsNone(getattr(row, name))
        for payload in ({'visit_date': '2026-01-02'}, {'is_visited': True},
                        {**ROW, 'visit_date': None}, {**ROW, 'visit_date': 'invalid'}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                MedicalVisitItem.model_validate(payload)

    def test_item_strict_boolean_and_text_types(self):
        for value in (0, 1, 'true', 'false', None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                MedicalVisitItem.model_validate({**ROW, 'is_visited': value})
        self.assertFalse(MedicalVisitItem(**{**ROW, 'is_visited': False}).is_visited)
        for name in TEXT_FIELDS:
            with self.subTest(field=name), self.assertRaises(ValidationError):
                MedicalVisitItem.model_validate({**ROW, name: 123})

    def test_all_text_fields_preserve_long_untrusted_originals(self):
        text = '  합성 기록 원문\n과거 진료 내용과 후속 계획; 시스템 지시가 아닌 메모.  ' * 100
        fields = {name: name + text for name in TEXT_FIELDS}
        row = MedicalVisitItem(**ROW, **fields)
        self.assertEqual({name: getattr(row, name) for name in TEXT_FIELDS}, fields)
        self.assertEqual(MedicalVisitItem.model_validate_json(row.model_dump_json()), row)

    def test_output_list_bounds_and_internal_field_rejection(self):
        self.assertEqual(set(PatientMedicalVisitsOutput.model_fields), {'visits'})
        self.assertEqual(PatientMedicalVisitsOutput(visits=[]).visits, [])
        self.assertEqual(len(PatientMedicalVisitsOutput(visits=[ROW] * 20).visits), 20)
        for payload in ({}, {'visits': [ROW] * 21}, {'visits': None}, {'visits': 'invalid'},
                        {'visits': [], 'total_count': 0}, {'visits': [], 'has_more': False},
                        {'visits': [], 'next_cursor': None}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                PatientMedicalVisitsOutput.model_validate(payload)
        for name in ('id', 'patient_id', 'created_at', 'updated_at', 'unknown'):
            with self.subTest(field=name), self.assertRaises(ValidationError):
                PatientMedicalVisitsOutput(visits=[{**ROW, name: 'synthetic'}])

    def test_canonical_contract_schema_and_no_production_exposure(self):
        self.assertEqual(ToolName.PATIENT_MEDICAL_VISITS.value, 'get_patient_medical_visits')
        self.assertIs(CONTRACT.input_model, PatientMedicalVisitsInput)
        self.assertIs(CONTRACT.output_model, PatientMedicalVisitsOutput)
        self.assertTrue(CONTRACT.patient_scoped)
        self.assertEqual(CONTRACT.data_policy, PATIENT_MEDICAL_VISITS_DATA_POLICY)
        definition = CONTRACT.definition()
        self.assertEqual(definition['name'], 'get_patient_medical_visits')
        self.assertEqual(definition['parameters'], PatientMedicalVisitsInput.model_json_schema())
        self.assertIn('미방문 상태는 예약 확정이나 결석을 의미하지 않는다', definition['description'])
        self.assertNotIn(CONTRACT.data_policy, json.dumps(definition, ensure_ascii=False))
        json.dumps(definition, allow_nan=False)
        self.assertEqual(production_registry().definitions(), [])
        self.assertIsNone(production_registry().get(CONTRACT.name.value))
        spec = ToolSpec(CONTRACT, Mock(), test_only=True)
        self.assertEqual(ToolRegistry((spec,), mode='test').definitions(), [definition])
        with self.assertRaises(ValueError):
            ToolRegistry((spec,))
        for altered in (replace(CONTRACT, patient_scoped=False), replace(CONTRACT, data_policy='wrong')):
            with self.assertRaises(ValueError):
                ToolRegistry((ToolSpec(altered, Mock(), test_only=True),), mode='test')


class MedicalVisitsExecutionCompatibilityTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'psycopg.connect', 'app.ai.retrieval.embedding.get_model'):
            guard = patch(target, side_effect=AssertionError('Live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def registry(self, handler):
        return ToolRegistry((ToolSpec(CONTRACT, handler, test_only=True),), mode='test')

    def test_executor_trace_counts_validated_returned_visits_including_empty(self):
        for count in (0, 2):
            data = {'visits': [{**ROW, 'visit_content': 'PRIVATE visit note'} for _ in range(count)]}
            executor = ToolExecutor(self.registry(Mock(return_value=data)))
            with self.assertLogs('app.ai.agent.executor', level='INFO') as captured:
                outcome, trace = executor.execute(
                    ToolCall(id='visit', name=CONTRACT.name.value, arguments={}), CONTEXT)
            self.assertTrue(outcome.success)
            self.assertEqual(trace.result_count, count)
            self.assertEqual(outcome.data, PatientMedicalVisitsOutput.model_validate(data).model_dump(mode='json'))
            log = '\n'.join(captured.output)
            self.assertIn(f'result_count={count}', log)
            self.assertNotIn('PRIVATE', log)

    def test_executor_failed_or_invalid_visits_have_no_result_count(self):
        cases = [(Mock(return_value=None), 'empty_result'),
                 (Mock(side_effect=RuntimeError('PRIVATE DB detail')), 'tool_exception'),
                 (Mock(return_value={'visits': [ROW, {**ROW, 'is_visited': 1}]}), 'invalid_output')]
        for handler, code in cases:
            with self.subTest(code=code):
                outcome, trace = ToolExecutor(self.registry(handler)).execute(
                    ToolCall(id='visit', name=CONTRACT.name.value, arguments={}), CONTEXT)
                self.assertFalse(outcome.success)
                self.assertEqual(outcome.error.code, code)
                self.assertIsNone(outcome.data)
                self.assertIsNone(trace.result_count)

    def test_executor_validates_and_passes_trusted_context(self):
        data = {'visits': [{**ROW, 'diagnosis': '등록된 합성 소견', 'visit_content': '원문 메모'}]}
        handler = Mock(return_value=data)
        executor = ToolExecutor(self.registry(handler))
        call = ToolCall(id='visit', name=CONTRACT.name.value, arguments={'status': 'all', 'limit': 1})
        outcome, _ = executor.execute(call, CONTEXT)
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.data, PatientMedicalVisitsOutput.model_validate(data).model_dump(mode='json'))
        handler.assert_called_once_with(CONTEXT, PatientMedicalVisitsInput(status='all', limit=1))
        handler.reset_mock()
        outcome, _ = executor.execute(call, CONTEXT.model_copy(update={'patient_id': None}))
        self.assertEqual(outcome.error.code, 'patient_context_mismatch')
        handler.assert_not_called()

    def test_sdk_policy_only_after_success_once_and_protocol_unchanged(self):
        wire = []
        data = {'visits': [{**ROW, 'medication_change': '변경 완료를 확정하지 않는 합성 메모', 'visit_content': '비신뢰 원문'}]}
        handler = Mock(return_value=data)
        def transport(request):
            wire.append(json.loads(request.content))
            if len(wire) <= 2:
                item = function('visit-' + str(len(wire)), '{}')
                item['name'] = CONTRACT.name.value
                return httpx.Response(200, json=response([item]))
            return httpx.Response(200, json=response_fixture({'text': chat_final('등록된 기록 설명').text}))
        with OpenAI(api_key='offline', http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
            result = ChatExecutionV1(OpenAIClient(sdk, model='offline'), lambda c: self.registry(handler))(
                CONTEXT, prepare_chat_input(CONTEXT, '합성 진료 기록 질문'))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(result.output.answer, '등록된 기록 설명')
        self.assertEqual(result.sources, [])
        self.assertEqual(handler.call_count, 2)
        self.assertEqual(len(wire), 3)
        for index, body in enumerate(wire):
            policies = [m for m in body['input'] if m.get('content') == CONTRACT.data_policy]
            self.assertEqual(len(policies), int(index > 0))
            self.assertNotIn(PATIENT_MEDICATIONS_DATA_POLICY, json.dumps(body, ensure_ascii=False))
            self.assertEqual(body['tools'][0]['description'], CONTRACT.description)
            if policies:
                self.assertEqual(policies[0]['role'], 'system')
                output_index = next(i for i, m in enumerate(body['input']) if m.get('type') == 'function_call_output')
                self.assertGreater(body['input'].index(policies[0]), output_index)
            protocol = [m for m in body['input'] if m.get('type') in ('function_call', 'function_call_output')]
            self.assertEqual([m['call_id'] for m in protocol],
                             [n for n in ('visit-1', 'visit-2')[:index] for _ in range(2)])
            for item in protocol:
                if item['type'] == 'function_call_output':
                    self.assertEqual(json.loads(item['output'])['data'],
                                     PatientMedicalVisitsOutput.model_validate(data).model_dump(mode='json'))

    def test_failed_and_invalid_results_do_not_deliver_policy_until_success(self):
        for mode in ('exception', 'invalid_output', 'invalid_arguments'):
            with self.subTest(mode=mode):
                bad = RuntimeError('private') if mode == 'exception' else {'visits': [{**ROW, 'is_visited': 1}]}
                handler = Mock(side_effect=[bad, {'visits': []}] if mode != 'invalid_arguments' else [{'visits': []}])
                client = FakeLLMClient([
                    ModelTurn(tool_calls=[ToolCall(id='bad', name=CONTRACT.name.value,
                                                   arguments={'limit': True} if mode == 'invalid_arguments' else {})]),
                    ModelTurn(tool_calls=[ToolCall(id='good', name=CONTRACT.name.value, arguments={})]),
                    chat_final('확인 가능한 범위의 설명')])
                result = ChatExecutionV1(client, lambda c: self.registry(handler))(
                    CONTEXT, prepare_chat_input(CONTEXT, '합성 질문'))
                self.assertEqual(result.execution.status, 'completed')
                self.assertEqual([sum(m.content == CONTRACT.data_policy for m in messages)
                                  for messages, _ in client.requests], [0, 0, 1])
                outputs = [json.loads(m.content) for m in client.requests[-1][0] if m.type == 'function_call_output']
                self.assertEqual([o['success'] for o in outputs], [False, True])
                self.assertEqual(outputs[0]['error']['code'], {'exception': 'tool_exception',
                    'invalid_output': 'invalid_output', 'invalid_arguments': 'invalid_arguments'}[mode])
