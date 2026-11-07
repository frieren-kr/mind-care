"""Medication declaration and offline Provider/Executor boundaries; no DB adapter."""
from dataclasses import replace
from datetime import date
import json
import unittest
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI
from pydantic import ValidationError

from ..clients.openai_client import OpenAIClient
from ..agent_loop_runner import AgentLoopRunner
from ..outputs import ChatAnswerV1
from ..executor import ToolExecutor
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec, production_registry
from ..schemas import ModelTurn, ToolCall
from ..tools.medications import (
    MedicationItem, PatientMedicationsInput, PatientMedicationsOutput,
    PATIENT_MEDICATIONS_DATA_POLICY,
)
from ..testing.chat_workflow_fakes import chat_final
from ..testing.fake_llm import FakeLLMClient
from ..workflows.chat_context_prepare import prepare_chat_input
from ..workflows.chat_execution import ChatExecutionError, ChatExecutionV1
from .test_responses import function, response, response_fixture
from .test_workflows import CONTEXT

CONTRACT = TOOL_CONTRACTS[ToolName.PATIENT_MEDICATIONS]


class MedicationContractTests(unittest.TestCase):
    def test_exact_fields_required_values_and_null_defaults(self):
        item = MedicationItem(drug_name='등록 약품 5mg', is_taking=True)
        self.assertEqual(set(MedicationItem.model_fields), {'drug_name', 'dosage', 'frequency', 'is_taking',
                                                'start_date', 'end_date', 'note'})
        self.assertEqual(MedicationItem.model_json_schema()['required'], ['drug_name', 'is_taking'])
        self.assertTrue(all(field.description for field in MedicationItem.model_fields.values()))
        for field in ('dosage', 'frequency', 'start_date', 'end_date', 'note'):
            self.assertIsNone(getattr(item, field))
        for payload in ({'drug_name':'약'}, {'is_taking':True}, {'drug_name':None,'is_taking':True},
                        {'drug_name':'약','is_taking':True,'patient_id':str(CONTEXT.patient_id)}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                MedicationItem.model_validate(payload)

    def test_boolean_is_strict(self):
        for value in (None, 0, 1, 'true', 'false', [], {}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                MedicationItem(drug_name='약', is_taking=value)
        self.assertFalse(MedicationItem(drug_name='약', is_taking=False).is_taking)

    def test_future_dates_and_free_note_are_preserved(self):
        note = '  어제 복용을 빠뜨렸다고 보호자가 기록함.\n처방 관련 자유 메모  '
        item = MedicationItem(drug_name='상품명 5mg', is_taking=True, dosage='1정', frequency='취침 전',
                              start_date='2099-01-01', end_date='2099-02-01', note=note)
        self.assertEqual(item.start_date, date(2099, 1, 1))
        self.assertEqual(item.end_date, date(2099, 2, 1))
        self.assertEqual(item.note, note)
        self.assertEqual(MedicationItem.model_validate_json(item.model_dump_json()), item)

    def test_input_status_limit_and_patient_identity_rejection(self):
        self.assertEqual(PatientMedicationsInput().model_dump(), {'status':'taking', 'limit':20})
        for status in ('taking', 'stopped', 'all'):
            for limit in (1, 30):
                PatientMedicationsInput(status=status, limit=limit)
        for payload in ({'status':'unknown'}, {'status':None}, {'patient_id':str(CONTEXT.patient_id)},
                        *({'limit':value} for value in (0, 31, True, 1.5, '20', None))):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                PatientMedicationsInput.model_validate(payload)

    def test_output_count_and_absolute_list_limit(self):
        row = {'drug_name':'약', 'is_taking':True}
        PatientMedicationsOutput(medications=[], total_count=0)
        PatientMedicationsOutput(medications=[row]*30, total_count=40)
        for rows,count in (([row],0), ([], -1), ([], True), ([], '1'), ([row]*31,31)):
            with self.subTest(count=count, length=len(rows)), self.assertRaises(ValidationError):
                PatientMedicationsOutput(medications=rows, total_count=count)

    def test_canonical_declaration_and_no_production_handler(self):
        self.assertIs(CONTRACT.input_model, PatientMedicationsInput)
        self.assertIs(CONTRACT.output_model, PatientMedicationsOutput)
        self.assertTrue(CONTRACT.patient_scoped)
        self.assertEqual(CONTRACT.data_policy, PATIENT_MEDICATIONS_DATA_POLICY)
        definition = CONTRACT.definition()
        self.assertEqual(definition['name'], 'get_patient_medications')
        self.assertEqual(definition['parameters'], PatientMedicationsInput.model_json_schema())
        self.assertEqual(definition['description'], CONTRACT.description)
        self.assertEqual(production_registry().definitions(), [])
        for altered in (replace(CONTRACT, data_policy='wrong'), replace(CONTRACT, patient_scoped=False)):
            with self.assertRaises(ValueError):
                ToolRegistry((ToolSpec(altered, lambda c,a:None),))

    def test_policy_preserves_registered_state_note_and_data_limits(self):
        for rule in ('is_taking만으로 특정 날짜의 복용 완료 또는 누락을 판단하지 않는다',
                     '미래 예정일도 가능', '무기한 복용을 확정하지 않는다', '자유 메모 원문',
                     '실제 복용·누락·부작용·처방', '비신뢰 데이터', '기록자의 서술',
                     '없는 날짜·횟수·사건을 생성하지 않는다', '일부만 확인', '다른 데이터 출처'):
            self.assertIn(rule, CONTRACT.data_policy)

    def test_actual_sdk_body_does_not_receive_policy_before_execution(self):
        for enabled in (False, True):
            wire = []
            def transport(request):
                wire.append(json.loads(request.content))
                return httpx.Response(200, json=response_fixture({'text':chat_final().text}))
            specs = (ToolSpec(CONTRACT, Mock(side_effect=AssertionError('Not called'))),) if enabled else ()
            registry = ToolRegistry(specs)
            with OpenAI(api_key='offline', http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
                result = ChatExecutionV1(OpenAIClient(sdk, model='offline'), lambda c:registry)(
                    CONTEXT, prepare_chat_input(CONTEXT, '합성 질문'))
            self.assertEqual(result.execution.status, 'completed')
            self.assertEqual(len(wire), 1)
            self.assertFalse(any(CONTRACT.data_policy in tool['description'] for tool in wire[0].get('tools', [])))
            if enabled:
                self.assertEqual(wire[0]['tools'][0]['description'], CONTRACT.description)
                self.assertNotIn('[복약 기록 데이터 해석 정책]', json.dumps(wire[0]['input'], ensure_ascii=False))
            else:
                self.assertEqual(wire[0].get('tools', []), [])

    def test_chat_loop_passes_original_medication_data_without_citations(self):
        data = {'medications':[{'drug_name':'원문 약품명', 'is_taking':True,
                                'start_date':'2099-01-01', 'note':'어제 누락했다고 기록함'}], 'total_count':3}
        handler = Mock(return_value=data)
        registry = ToolRegistry((ToolSpec(CONTRACT, handler, test_only=True),), mode='test')
        client = FakeLLMClient([
            ModelTurn(tool_calls=[ToolCall(id='med_call', name=CONTRACT.name.value, arguments={})]),
            chat_final('등록된 기록 범위의 설명')])
        result = ChatExecutionV1(client, lambda c:registry)(CONTEXT, prepare_chat_input(CONTEXT, '등록된 약'))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(result.sources, [])
        self.assertEqual(result.output.citation_refs, [])
        self.assertEqual(result.execution.total_tool_calls, 1)
        returned = next(m for m in client.requests[1][0] if m.type=='function_call_output')
        self.assertEqual(returned.call_id, 'med_call')
        envelope = json.loads(returned.content)
        self.assertTrue(envelope['success'])
        self.assertNotIn('source_reference', envelope)
        self.assertEqual(envelope['data'], PatientMedicationsOutput.model_validate(data).model_dump(mode='json'))
        handler.assert_called_once_with(CONTEXT, PatientMedicationsInput())

    def test_executor_uses_existing_validation_and_selected_patient(self):
        handler = Mock(return_value={'medications':[{'drug_name':'원문 약','is_taking':False,
                                                     'note':'보호자의 복용 누락 기록'}], 'total_count':2})
        registry = ToolRegistry((ToolSpec(CONTRACT, handler),))
        executor = ToolExecutor(registry)
        call = ToolCall(id='med1', name=CONTRACT.name.value, arguments={'status':'all','limit':1})
        outcome, _ = executor.execute(call, CONTEXT)
        self.assertTrue(outcome.success)
        handler.assert_called_once_with(CONTEXT, PatientMedicationsInput(status='all',limit=1))
        self.assertEqual(outcome.data['medications'][0]['note'], '보호자의 복용 누락 기록')
        self.assertEqual(outcome.data['total_count'], 2)
        handler.reset_mock()
        outcome, _ = executor.execute(call, CONTEXT.model_copy(update={'patient_id':None}))
        self.assertEqual(outcome.error.code, 'patient_context_mismatch')
        handler.assert_not_called()
        bad = call.model_copy(update={'arguments':{'limit':True}})
        self.assertEqual(executor.execute(bad, CONTEXT)[0].error.code, 'invalid_arguments')
        handler.assert_not_called()

    def test_chat_missing_patient_fails_before_provider(self):
        registry = ToolRegistry((ToolSpec(CONTRACT, Mock()),))
        client = FakeLLMClient([chat_final()])
        context = CONTEXT.model_copy(update={'patient_id':None})
        with self.assertRaises(ChatExecutionError):
            ChatExecutionV1(client, lambda c:registry)(context, prepare_chat_input(context, '질문'))
        self.assertEqual(client.requests, [])

    def test_existing_tool_definitions_have_no_medication_policy(self):
        for name,contract in TOOL_CONTRACTS.items():
            if name is ToolName.PATIENT_MEDICATIONS:
                continue
            self.assertEqual(contract.definition()['description'], contract.description)
            self.assertNotIn(CONTRACT.data_policy, json.dumps(contract.definition(), ensure_ascii=False))

    def test_sdk_policy_after_success_once_with_original_call_result_order(self):
        wire = []
        native = []
        for identifier in ('med1', 'med2'):
            item = function(identifier, '{}')
            item['name'] = CONTRACT.name.value
            native.append(item)
        def transport(request):
            wire.append(json.loads(request.content))
            if len(wire) <= 2:
                return httpx.Response(200, json=response([native[len(wire)-1]]))
            return httpx.Response(200, json=response_fixture({'text':chat_final().text}))
        registry = ToolRegistry((ToolSpec(CONTRACT, lambda c,a:{'medications':[], 'total_count':0}),))
        with OpenAI(api_key='offline', http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
            result = ChatExecutionV1(OpenAIClient(sdk, model='offline'), lambda c:registry)(
                CONTEXT, prepare_chat_input(CONTEXT, '합성 질문'))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(len(wire), 3)
        for index,body in enumerate(wire):
            policies = [m for m in body['input'] if m.get('content')==CONTRACT.data_policy]
            self.assertEqual(len(policies), int(index > 0))
            if policies:
                self.assertEqual(policies[0]['role'], 'system')
                first_result = next(i for i,m in enumerate(body['input']) if m.get('type')=='function_call_output')
                self.assertGreater(body['input'].index(policies[0]), first_result)
            protocol = [m for m in body['input'] if m.get('type') in ('function_call','function_call_output')]
            self.assertEqual([m['type'] for m in protocol], ['function_call','function_call_output']*index)
            self.assertEqual([m['call_id'] for m in protocol], [n for n in ('med1','med2')[:index] for _ in range(2)])
            self.assertEqual([m for m in protocol if m['type']=='function_call'], native[:index])
            self.assertEqual(body['tools'][0]['description'], CONTRACT.description)

    def test_failed_invalid_and_unregistered_calls_do_not_deliver_policy(self):
        for mode in ('exception','invalid_output','invalid_arguments','unregistered'):
            with self.subTest(mode=mode):
                handler = Mock(side_effect=RuntimeError('private')) if mode=='exception' else Mock(return_value={'bad':'data'})
                registry = ToolRegistry((ToolSpec(CONTRACT, handler),)) if mode!='unregistered' else ToolRegistry()
                args = {'limit':True} if mode=='invalid_arguments' else {}
                client = FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id='bad',name=CONTRACT.name.value,arguments=args)]), chat_final()])
                result = ChatExecutionV1(client, lambda c:registry)(CONTEXT, prepare_chat_input(CONTEXT, '질문'))
                self.assertEqual(result.execution.status, 'completed')
                self.assertFalse(next(json.loads(m.content)['success'] for m in client.requests[1][0] if m.type=='function_call_output'))
                self.assertTrue(all(m.content!=CONTRACT.data_policy for messages,_ in client.requests for m in messages))
                if mode in ('invalid_arguments','unregistered'):
                    handler.assert_not_called()

    def test_parallel_batch_delivers_only_successful_policies_after_all_results(self):
        profile = replace(TOOL_CONTRACTS[ToolName.PATIENT_PROFILE], data_policy='PROFILE_POLICY')
        logs = replace(TOOL_CONTRACTS[ToolName.RECENT_CARE_LOGS], data_policy='LOGS_POLICY')
        def failed(c,a):
            raise RuntimeError('private')
        from ..testing.fake_tools import fake_get_recent_care_logs
        with patch.dict(TOOL_CONTRACTS, {profile.name:profile, logs.name:logs}):
            registry = ToolRegistry((
                ToolSpec(CONTRACT, lambda c,a:{'medications':[], 'total_count':0}, parallel_safe=True),
                ToolSpec(profile, failed, parallel_safe=True),
                ToolSpec(logs, fake_get_recent_care_logs, parallel_safe=True)))
            client = FakeLLMClient([ModelTurn(tool_calls=[
                ToolCall(id=str(i), name=contract.name.value, arguments={})
                for i,contract in enumerate((CONTRACT, profile, logs, CONTRACT))]), chat_final()])
            result = ChatExecutionV1(client, lambda c:registry)(CONTEXT, prepare_chat_input(CONTEXT, '질문'))
        self.assertEqual(result.execution.status, 'completed')
        messages = client.requests[1][0]
        candidates = {CONTRACT.data_policy, profile.data_policy, logs.data_policy}
        policies = [m for m in messages if m.content in candidates]
        self.assertEqual([m.content for m in policies], [CONTRACT.data_policy, logs.data_policy])
        self.assertTrue(all(m.role=='system' and m.kind=='policy' for m in policies))
        result_indices = [i for i,m in enumerate(messages) if m.type=='function_call_output']
        self.assertEqual([messages[i].call_id for i in result_indices], ['0','1','2','3'])
        self.assertTrue(all(messages.index(m)>max(result_indices) for m in policies))

    def test_reused_runner_does_not_share_delivered_policies_between_requests(self):
        call = ModelTurn(tool_calls=[ToolCall(id='med',name=CONTRACT.name.value,arguments={})])
        client = FakeLLMClient([call, chat_final(), chat_final(), call, chat_final()])
        runner = AgentLoopRunner(client, ToolRegistry((ToolSpec(CONTRACT, lambda c,a:{'medications':[], 'total_count':0}),)))
        for _ in range(3):
            result = runner.run_structured('합성 질문', CONTEXT, output_model=ChatAnswerV1)
            self.assertEqual(result.execution.status, 'completed')
        counts = [sum(m.content==CONTRACT.data_policy for m in messages) for messages,_ in client.requests]
        self.assertEqual(counts, [0,1,0,0,1])

    def test_failed_call_followed_by_success_delivers_policy_only_after_success(self):
        handler = Mock(side_effect=[{'invalid':'data'}, {'medications':[], 'total_count':0}])
        registry = ToolRegistry((ToolSpec(CONTRACT, handler),))
        client = FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id=i,name=CONTRACT.name.value,arguments={})])
                                for i in ('bad','good')] + [chat_final()])
        result = ChatExecutionV1(client, lambda c:registry)(CONTEXT, prepare_chat_input(CONTEXT, '질문'))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual([sum(m.content==CONTRACT.data_policy for m in messages)
                          for messages,_ in client.requests], [0,0,1])
