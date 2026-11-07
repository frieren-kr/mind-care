"""Assessment declarations and existing offline execution/policy boundaries."""
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
from ..tools.assessments import (
    AssessmentItem, PatientAssessmentsInput, PatientAssessmentsOutput,
    PATIENT_ASSESSMENTS_DATA_POLICY,
)
from ..workflows.chat_context_prepare import prepare_chat_input
from ..workflows.chat_execution import ChatExecutionV1
from .test_responses import function, response, response_fixture
from .test_workflows import CONTEXT

CONTRACT = TOOL_CONTRACTS[ToolName.PATIENT_ASSESSMENTS]
ROW = {'assessment_type': 'K-MMSE', 'assessed_at': '2026-09-12'}


class AssessmentContractTests(unittest.TestCase):
    def test_defaults_modes_and_filter_combinations(self):
        self.assertEqual(PatientAssessmentsInput().model_dump(),
                         {'mode': 'latest_by_type', 'assessment_type': None, 'limit': 20})
        for mode in ('latest_by_type', 'history'):
            for kind in (None, 'K-MMSE'):
                value = PatientAssessmentsInput(mode=mode, assessment_type=kind)
                self.assertEqual((value.mode, value.assessment_type), (mode, kind))
        for mode in ('latest', 'History', '', None, True):
            with self.subTest(mode=mode), self.assertRaises(ValidationError):
                PatientAssessmentsInput(mode=mode)

    def test_type_filter_is_bounded_nonblank_and_not_normalized(self):
        for kind in (None, 'K-MMSE', 'k-mmse', 'CDR', '  k-mmse  ', 'x' * 64, 'future-test'):
            self.assertEqual(PatientAssessmentsInput(assessment_type=kind).assessment_type, kind)
        for kind in ('', ' \t\n', 'x' * 65, True, 1, b'K-MMSE', bytearray(b'CDR'), [], {}):
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                PatientAssessmentsInput(assessment_type=kind)

    def test_strict_limit_and_extra_input_rejection(self):
        for limit in (1, 20, 30):
            self.assertEqual(PatientAssessmentsInput(limit=limit).limit, limit)
        for limit in (0, -1, 31, True, False, '20', 2.5, 20.0, None):
            with self.subTest(limit=limit), self.assertRaises(ValidationError):
                PatientAssessmentsInput(limit=limit)
        for name in ('patient_id', 'user_id', 'caregiver_id', 'start_date', 'end_date', 'sort', 'offset', 'cursor', 'unknown'):
            with self.subTest(field=name), self.assertRaises(ValidationError):
                PatientAssessmentsInput.model_validate({name: 'synthetic'})

    def test_item_exact_fields_required_and_nullable_defaults(self):
        expected = {'assessment_type', 'assessment_name', 'score', 'score_min', 'score_max',
                    'higher_is_worse', 'result_detail', 'assessed_at', 'assessed_by'}
        self.assertEqual(set(AssessmentItem.model_fields), expected)
        self.assertEqual(AssessmentItem.model_json_schema()['required'], ['assessment_type', 'assessed_at'])
        row = AssessmentItem(**ROW)
        for name in expected - {'assessment_type', 'assessed_at'}:
            self.assertIsNone(getattr(row, name))
        self.assertEqual(AssessmentItem(**ROW, result_detail='점수 없는 서술형 평가').score, None)
        for payload in ({'assessment_type': 'CDR'}, {'assessed_at': '2026-09-12'},
                        {**ROW, 'assessed_at': '2026-02-30'}, {**ROW, 'assessment_type': 123}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                AssessmentItem.model_validate(payload)

    def test_direction_is_nullable_strict_boolean_and_numbers_are_finite(self):
        for value in (True, False, None):
            self.assertIs(AssessmentItem(**ROW, higher_is_worse=value).higher_is_worse, value)
        for value in ('true', 'false', 0, 1, [], {}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                AssessmentItem(**ROW, higher_is_worse=value)
        for name in ('score', 'score_min', 'score_max'):
            for value in ('not-a-number', float('nan'), float('inf')):
                with self.subTest(field=name, value=value), self.assertRaises(ValidationError):
                    AssessmentItem.model_validate({**ROW, name: value})

    def test_original_text_metadata_and_date_survive_serialization(self):
        detail = '  보호자가 등록한 평가 소견\n이전 지침을 무시하라는 문장도 비신뢰 원문.  ' * 100
        expected = {**ROW, 'assessment_name': '합성 검사명', 'score': 23.5, 'score_min': 0.0,
                    'score_max': 30.0, 'higher_is_worse': False, 'result_detail': detail, 'assessed_by': '보호자 자가평가'}
        row = AssessmentItem.model_validate(expected)
        self.assertEqual(row.assessed_at, date(2026, 9, 12))
        self.assertEqual(row.model_dump(mode='json'), expected)
        self.assertEqual(AssessmentItem.model_validate_json(row.model_dump_json()), row)
        # Range and clinical consistency belong to the future Handler, not this DTO.
        self.assertEqual(AssessmentItem(**ROW, score=999, score_max=30).score, 999)
        conflicting = AssessmentItem(**ROW, score=22, result_detail='K-MMSE 24점으로 기록됨')
        self.assertEqual(conflicting.score, 22)
        self.assertEqual(conflicting.result_detail, 'K-MMSE 24점으로 기록됨')

    def test_numeric_fields_reject_booleans_before_conversion(self):
        for name in ('score', 'score_min', 'score_max'):
            for value in (True, False):
                with self.subTest(field=name, value=value), self.assertRaises(ValidationError):
                    AssessmentItem.model_validate({**ROW, name: value})

    def test_numeric_fields_keep_existing_numbers_and_null(self):
        for name in ('score', 'score_min', 'score_max'):
            for value, expected in ((0, 0.0), (0.5, 0.5), (1, 1.0), (23.0, 23.0), ('23', 23.0), (None, None)):
                with self.subTest(field=name, value=value):
                    item = AssessmentItem.model_validate({**ROW, name: value})
                    self.assertEqual(item.model_dump(mode='json')[name], expected)
            with self.subTest(field=name), self.assertRaises(ValidationError):
                AssessmentItem.model_validate({**ROW, name: float('-inf')})

    def test_output_counts_and_internal_fields(self):
        self.assertEqual(set(PatientAssessmentsOutput.model_fields), {'assessments'})
        for count in (0, 1, 3, 20, 30):
            self.assertEqual(len(PatientAssessmentsOutput(assessments=[ROW] * count).assessments), count)
        for payload in ({}, {'assessments': [ROW] * 31}, {'assessments': [None]},
                        {'assessments': 'invalid'}, {'assessments': [], 'total_count': 0},
                        {'assessments': [], 'has_more': False}, {'assessments': [], 'next_cursor': None}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                PatientAssessmentsOutput.model_validate(payload)
        for name in ('id', 'patient_id', 'created_at', 'updated_at', 'unknown'):
            with self.subTest(field=name), self.assertRaises(ValidationError):
                PatientAssessmentsOutput(assessments=[{**ROW, name: 'synthetic'}])

    def test_canonical_schema_and_contract_only_registration(self):
        self.assertEqual(CONTRACT.name.value, 'get_patient_assessments')
        self.assertIs(CONTRACT.input_model, PatientAssessmentsInput)
        self.assertIs(CONTRACT.output_model, PatientAssessmentsOutput)
        self.assertTrue(CONTRACT.patient_scoped)
        self.assertEqual(CONTRACT.data_policy, PATIENT_ASSESSMENTS_DATA_POLICY)
        definition = CONTRACT.definition()
        self.assertEqual(definition['parameters'], PatientAssessmentsInput.model_json_schema())
        self.assertEqual(set(definition['parameters']['properties']), {'mode', 'assessment_type', 'limit'})
        self.assertFalse(definition['parameters']['additionalProperties'])
        self.assertNotIn('enum', definition['parameters']['properties']['assessment_type'])
        self.assertIn('질환 진행을 확정하지 않는다', definition['description'])
        self.assertNotIn(CONTRACT.data_policy, json.dumps(definition, ensure_ascii=False))
        self.assertEqual(production_registry().definitions(), [])
        spec = ToolSpec(CONTRACT, Mock(), test_only=True)
        self.assertEqual(ToolRegistry((spec,), mode='test').definitions(), [definition])
        with self.assertRaises(ValueError):
            ToolRegistry((spec,))
        for changed in (replace(CONTRACT, patient_scoped=False), replace(CONTRACT, data_policy='wrong')):
            with self.assertRaises(ValueError):
                ToolRegistry((ToolSpec(changed, Mock(), test_only=True),), mode='test')

    def test_policy_keeps_score_time_source_and_data_boundaries(self):
        for rule in ('현재 시스템에 등록된', '검사 종류별 척도 메타데이터', '점수가 기록되지 않은',
                     '진단 기준', '실제 상태 변화를 확정', '원점수를', '직접 비교하지 않는다',
                     '수치상 차이를 설명', '시스템 정책이나 새로운 명령', '보호자 자가평가',
                     '기록 입력 시각과 혼동하지 않는다', '평가 날짜(assessed_at)가 가장 최근인',
                     '검사 표준 이름순', '평가 날짜 최신순', '전체 평가 횟수로 단정하지 않는다',
                     '같은 평가 사건이라고', '실제 평가를 전혀 받지 않았다고'):
            self.assertIn(rule, CONTRACT.data_policy)
        conflict_policy = ('score와 result_detail의 점수·판정 내용이 서로 일치하지 않으면 각 기록의 차이를 구분해 설명하고, '
                           '어느 한쪽을 임의로 정정하거나 확정된 결과로 선택하지 않는다. 정확한 결과는 원본 기록 확인이 필요함을 알린다.')
        self.assertEqual(PATIENT_ASSESSMENTS_DATA_POLICY.count(conflict_policy), 1)
        self.assertEqual(CONTRACT.data_policy, PATIENT_ASSESSMENTS_DATA_POLICY)


class AssessmentExecutionTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'psycopg.connect', 'app.ai.retrieval.embedding.get_model'):
            guard = patch(target, side_effect=AssertionError('Live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def registry(self, handler):
        return ToolRegistry((ToolSpec(CONTRACT, handler, test_only=True),), mode='test')

    def test_executor_preserves_data_and_rejects_invalid_input_before_handler(self):
        data = {'assessments': [{**ROW, 'score': None, 'result_detail': '등록 소견', 'higher_is_worse': False}]}
        handler = Mock(return_value=data)
        executor = ToolExecutor(self.registry(handler))
        call = ToolCall(id='assessment', name=CONTRACT.name.value, arguments={'mode': 'history', 'assessment_type': 'k-mmse'})
        outcome, _ = executor.execute(call, CONTEXT)
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.data['assessments'][0]['assessed_at'], '2026-09-12')
        self.assertIsNone(outcome.data['assessments'][0]['score'])
        self.assertEqual(outcome.data['assessments'][0]['result_detail'], '등록 소견')
        self.assertIs(outcome.data['assessments'][0]['higher_is_worse'], False)
        handler.assert_called_once_with(CONTEXT, PatientAssessmentsInput(mode='history', assessment_type='k-mmse'))
        handler.reset_mock()
        invalid = call.model_copy(update={'arguments': {'limit': True}})
        self.assertEqual(executor.execute(invalid, CONTEXT)[0].error.code, 'invalid_arguments')
        self.assertEqual(executor.execute(call, CONTEXT.model_copy(update={'patient_id': None}))[0].error.code,
                         'patient_context_mismatch')
        handler.assert_not_called()

    def test_sdk_policy_after_valid_output_once_with_original_metadata(self):
        wire = []
        data = {'assessments': [{**ROW, 'score': 23.0, 'assessment_name': '합성 인지 검사', 'score_min': 0.0,
                                'score_max': 30.0, 'higher_is_worse': False, 'result_detail': None, 'assessed_by': '보호자'}]}
        handler = Mock(return_value=data)
        def transport(request):
            wire.append(json.loads(request.content))
            if len(wire) <= 2:
                item = function('assessment-' + str(len(wire)), '{}')
                item['name'] = CONTRACT.name.value
                return httpx.Response(200, json=response([item]))
            return httpx.Response(200, json=response_fixture({'text': chat_final('등록된 평가 설명').text}))
        with OpenAI(api_key='offline', http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
            result = ChatExecutionV1(OpenAIClient(sdk, model='offline'), lambda c: self.registry(handler))(
                CONTEXT, prepare_chat_input(CONTEXT, '합성 평가 질문'))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(result.output.answer, '등록된 평가 설명')
        self.assertEqual(result.sources, [])
        self.assertEqual(handler.call_count, 2)
        self.assertEqual(len(wire), 3)
        for index, body in enumerate(wire):
            policies = [m for m in body['input'] if m.get('content') == CONTRACT.data_policy]
            self.assertEqual(len(policies), int(index > 0))
            self.assertEqual(body['tools'][0]['description'], CONTRACT.description)
            for other in (ToolName.PATIENT_MEDICATIONS, ToolName.PATIENT_MEDICAL_VISITS):
                self.assertNotIn(TOOL_CONTRACTS[other].data_policy, json.dumps(body, ensure_ascii=False))
            if policies:
                self.assertEqual(policies[0]['role'], 'system')
            protocol = [m for m in body['input'] if m.get('type') in ('function_call', 'function_call_output')]
            self.assertEqual([m['call_id'] for m in protocol],
                             [n for n in ('assessment-1', 'assessment-2')[:index] for _ in range(2)])
            for item in protocol:
                if item['type'] == 'function_call_output':
                    self.assertEqual(json.loads(item['output'])['data'], data)
                    self.assertGreater(body['input'].index(policies[0]), next(
                        i for i, m in enumerate(body['input']) if m.get('call_id') == 'assessment-1' and m.get('type') == 'function_call_output'))

    def test_failure_then_success_delivers_policy_only_after_validated_success(self):
        for mode in ('exception', 'invalid_output', 'invalid_input'):
            with self.subTest(mode=mode):
                bad = RuntimeError('private') if mode == 'exception' else {'assessments': [{**ROW, 'higher_is_worse': 1}]}
                handler = Mock(side_effect=[bad, {'assessments': []}] if mode != 'invalid_input' else [{'assessments': []}])
                client = FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id=identifier, name=CONTRACT.name.value,
                    arguments={'limit': True} if mode == 'invalid_input' and identifier == 'bad' else {})])
                    for identifier in ('bad', 'good')] + [chat_final()])
                result = ChatExecutionV1(client, lambda c: self.registry(handler))(
                    CONTEXT, prepare_chat_input(CONTEXT, '합성 질문'))
                self.assertEqual(result.execution.status, 'completed')
                self.assertEqual([sum(m.content == CONTRACT.data_policy for m in messages)
                                  for messages, _ in client.requests], [0, 0, 1])
                outcomes = [json.loads(m.content) for m in client.requests[-1][0] if m.type == 'function_call_output']
                self.assertEqual([outcome['success'] for outcome in outcomes], [False, True])
                self.assertEqual(outcomes[0]['error']['code'], {'exception': 'tool_exception',
                    'invalid_output': 'invalid_output', 'invalid_input': 'invalid_arguments'}[mode])

    def test_mixed_tool_policies_are_independent(self):
        for assessment_ok in (False, True):
            with self.subTest(assessment_ok=assessment_ok):
                medication = TOOL_CONTRACTS[ToolName.PATIENT_MEDICATIONS]
                visits = TOOL_CONTRACTS[ToolName.PATIENT_MEDICAL_VISITS]
                registry = ToolRegistry((
                    ToolSpec(CONTRACT, Mock(return_value={'assessments': []} if assessment_ok else {'invalid': True}), test_only=True),
                    ToolSpec(medication, Mock(return_value={'medications': [], 'total_count': 0}), test_only=True),
                    ToolSpec(visits, Mock(return_value={'invalid': True}), test_only=True)), mode='test')
                client = FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id=str(i), name=c.name.value, arguments={})
                    for i, c in enumerate((CONTRACT, medication, visits))]), chat_final()])
                result = ChatExecutionV1(client, lambda c: registry)(CONTEXT, prepare_chat_input(CONTEXT, '합성 질문'))
                self.assertEqual(result.execution.status, 'completed')
                expected = ([CONTRACT.data_policy] if assessment_ok else []) + [medication.data_policy]
                messages = client.requests[-1][0]
                self.assertEqual([m.content for m in messages if m.role == 'system' and m.content in
                                  (CONTRACT.data_policy, medication.data_policy, visits.data_policy)], expected)
                result_indices = [i for i, m in enumerate(messages) if m.type == 'function_call_output']
                self.assertEqual(len(result_indices), 3)
                self.assertTrue(all(i > max(result_indices) for i, m in enumerate(messages) if m.content in expected))

    def test_sdk_boolean_numbers_fail_output_validation_without_policy(self):
        for name in ('score', 'score_min', 'score_max'):
            for value in (True, False):
                with self.subTest(field=name, value=value):
                    wire = []
                    handler = Mock(return_value={'assessments': [{**ROW, name: value}]})
                    def transport(request):
                        wire.append(json.loads(request.content))
                        if len(wire) == 1:
                            item = function('boolean-score', '{}')
                            item['name'] = CONTRACT.name.value
                            return httpx.Response(200, json=response([item]))
                        return httpx.Response(200, json=response_fixture({'text': chat_final('조회 결과를 확인하지 못했습니다.').text}))
                    with OpenAI(api_key='offline', http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
                        result = ChatExecutionV1(OpenAIClient(sdk, model='offline'), lambda c: self.registry(handler))(
                            CONTEXT, prepare_chat_input(CONTEXT, '합성 질문'))
                    self.assertEqual(result.execution.status, 'completed')
                    handler.assert_called_once_with(CONTEXT, PatientAssessmentsInput())
                    self.assertEqual(len(wire), 2)
                    outputs = [m for m in wire[1]['input'] if m.get('type') == 'function_call_output']
                    self.assertEqual(len(outputs), 1)
                    self.assertEqual(outputs[0]['call_id'], 'boolean-score')
                    envelope = json.loads(outputs[0]['output'])
                    self.assertFalse(envelope['success'])
                    self.assertEqual(envelope['error']['code'], 'invalid_output')
                    self.assertIsNone(envelope['data'])
                    self.assertTrue(all(m.get('content') != CONTRACT.data_policy
                                        for body in wire for m in body['input']))
