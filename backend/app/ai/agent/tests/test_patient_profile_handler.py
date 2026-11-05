"""Production profile assembly over fake DB rows and SDK MockTransport."""
from functools import partial
import inspect
import json
from threading import Barrier
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import UUID

import httpx
from openai import OpenAI

from ..adapters.patient_profile import PatientProfileAdapter
from ..adapters.rag_evidence import RagEvidenceRetriever
from ..clients.openai_client import OpenAIClient
from ..composition import build_chat_registry, build_evidence_registry
from ..executor import ToolExecutor
from ..registry import TOOL_CONTRACTS, ToolName, production_registry
from ..schemas import ModelTurn
from ..testing.chat_workflow_fakes import chat_call, chat_final
from ..testing.fake_llm import FakeLLMClient
from ..tools.contracts import PatientProfileInput, PatientProfileOutput
from ..tools.evidence_tools import EvidenceRetriever
from ..tools.patient_profile import PatientProfileHandler, PatientProfileReader, PATIENT_PROFILE_DATA_POLICY
from ..workflows.chat_context_prepare import prepare_chat_input
from ..workflows.chat_execution import ChatExecutionV1
from .test_patient_profile_adapter import patient
from .test_feed_candidates import package
from .test_responses import function, response, response_fixture
from .test_workflows import CONTEXT


class PatientProfileHandlerTests(TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'psycopg.connect', 'app.ai.retrieval.embedding.get_model'):
            guard = patch(target, side_effect=AssertionError('Live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_handler_only_forwards_the_same_context_and_returns_reader_result(self):
        value = PatientProfileOutput()
        reader = Mock(spec=PatientProfileReader)
        reader.get_patient_profile.return_value = value
        result = PatientProfileHandler(reader)(CONTEXT, PatientProfileInput())
        reader.get_patient_profile.assert_called_once_with(context=CONTEXT)
        self.assertIs(reader.get_patient_profile.call_args.kwargs['context'], CONTEXT)
        self.assertIs(result, value)
        self.assertIs(inspect.signature(PatientProfileHandler.__init__).parameters['reader'].annotation,
                      PatientProfileReader)

    def test_chat_factory_uses_canonical_production_specs_and_keeps_search_only_factory(self):
        tools = build_chat_registry(CONTEXT)
        self.assertEqual(tools.mode, 'production')
        self.assertEqual(set(tools.names()), {'search_evidence', 'get_patient_profile', 'get_caregiver_profile'})
        for name in (ToolName.PATIENT_PROFILE, ToolName.SEARCH_EVIDENCE):
            spec = tools.get(name.value)
            self.assertIs(spec.contract, TOOL_CONTRACTS[name])
            self.assertTrue(spec.parallel_safe)
            self.assertFalse(spec.test_only)
        spec = tools.get('get_patient_profile')
        self.assertIsInstance(spec.handler, PatientProfileHandler)
        self.assertIsInstance(spec.handler.reader, PatientProfileAdapter)
        self.assertIsNone(spec.handler.reader.get_patient_fn)
        self.assertIsInstance(tools.get('search_evidence').handler.retriever, RagEvidenceRetriever)
        self.assertEqual(build_evidence_registry().names(), ('search_evidence',))
        self.assertEqual(production_registry().definitions(), [])
        self.assertIsNot(build_chat_registry(CONTEXT), tools)
        sequential = build_chat_registry(CONTEXT, parallel_safe=False)
        self.assertTrue(all(not sequential.get(name).parallel_safe for name in sequential.names()))

    def test_no_patient_registry_omits_profile_and_direct_answer_never_queries(self):
        context = CONTEXT.model_copy(update={'patient_id': None})
        lookup = Mock(side_effect=AssertionError('No patient lookup'))
        reader = PatientProfileAdapter(get_patient_fn=lookup)
        retriever = Mock(spec=EvidenceRetriever)
        factory = partial(build_chat_registry, retriever=retriever, patient_reader=reader)
        tools = factory(context)
        self.assertEqual(tools.names(), ('search_evidence', 'get_caregiver_profile'))
        client = FakeLLMClient([chat_final('일반 설명')])
        result = ChatExecutionV1(client, factory)(context, prepare_chat_input(context, '일반 질문'))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual([d['name'] for d in client.requests[0][1]], ['search_evidence','get_caregiver_profile'])
        lookup.assert_not_called()
        retriever.search_evidence.assert_not_called()

    def test_extra_arguments_and_none_scope_guard_precede_db_lookup(self):
        lookup = Mock(side_effect=AssertionError('Invalid input must not query'))
        reader = PatientProfileAdapter(get_patient_fn=lookup)
        executor = ToolExecutor(build_chat_registry(CONTEXT, patient_reader=reader))
        for name in ('patient_id', 'user_id', 'caregiver_id', 'include_care_logs'):
            with self.subTest(name=name):
                outcome, _ = executor.execute(chat_call(args={name: 'untrusted'}), CONTEXT)
                self.assertEqual(outcome.error.code, 'invalid_arguments')
        outcome, _ = executor.execute(chat_call(), CONTEXT.model_copy(update={'patient_id': None}))
        self.assertEqual(outcome.error.code, 'patient_context_mismatch')
        lookup.assert_not_called()

    def test_db_boundary_failures_use_executor_errors_and_do_not_leak_data_or_logs(self):
        invalid = patient(); invalid.symptoms = 'PRIVATE_PATIENT_CONTENT'
        cases = [(None, 'tool_exception'), (patient(id=UUID(int=987)), 'tool_exception'),
                 (patient(caregiver_id=UUID(int=988)), 'tool_exception'), (invalid, 'tool_exception'),
                 (RuntimeError('PRIVATE_PATIENT_CONTENT'), 'tool_exception'),
                 (TimeoutError('PRIVATE_PATIENT_CONTENT'), 'tool_timeout')]
        for value, code in cases:
            with self.subTest(code=code, kind=type(value)):
                lookup = Mock(side_effect=value) if isinstance(value, Exception) else Mock(return_value=value)
                tools = build_chat_registry(CONTEXT, patient_reader=PatientProfileAdapter(get_patient_fn=lookup))
                with self.assertLogs('app.ai.agent.executor', level='INFO') as logs:
                    outcome, trace = ToolExecutor(tools).execute(chat_call(), CONTEXT)
                self.assertFalse(outcome.success)
                self.assertIsNone(outcome.data)
                self.assertEqual(outcome.error.code, code)
                self.assertEqual(trace.error_type, code)
                self.assertNotIn('PRIVATE_PATIENT_CONTENT', outcome.model_dump_json())
                self.assertNotIn('PRIVATE_PATIENT_CONTENT', '\n'.join(logs.output))
                lookup.assert_called_once_with(CONTEXT.patient_id)

    def execute_sdk(self, lookup, *, context=CONTEXT):
        wire = []
        call = {**function('profile', '{}'), 'name': 'get_patient_profile'}
        turns = [response([call]), response_fixture({'text': chat_final('저장된 정보 범위의 합성 설명').text})]
        def transport(request):
            wire.append(json.loads(request.content))
            self.assertLessEqual(len(wire), 2)
            return httpx.Response(200, json=turns[len(wire)-1])
        retriever = Mock(spec=EvidenceRetriever)
        factory = partial(build_chat_registry, retriever=retriever,
                          patient_reader=PatientProfileAdapter(get_patient_fn=lookup))
        with OpenAI(api_key='offline-placeholder', max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
            result = ChatExecutionV1(OpenAIClient(sdk, model='offline'), factory)(
                context, prepare_chat_input(context, '등록된 프로필을 알려 주세요.'))
        retriever.search_evidence.assert_not_called()
        return result, wire

    def test_sdk_success_projects_exact_fields_and_only_then_delivers_canonical_policy(self):
        for sparse in (False, True):
            with self.subTest(sparse=sparse):
                raw = patient(name=None, dementia_stage=None, diagnosis_date=None, symptoms=[], interests=[]) if sparse else patient()
                before = raw.model_dump(mode='json')
                lookup = Mock(return_value=raw)
                result, wire = self.execute_sdk(lookup)
                self.assertEqual(result.execution.status, 'completed')
                self.assertEqual(result.execution.errors, [])
                self.assertEqual(result.sources, [])
                self.assertEqual({d['name'] for d in wire[0]['tools']}, {'search_evidence','get_patient_profile','get_caregiver_profile'})
                self.assertFalse(any(m.get('content') == PATIENT_PROFILE_DATA_POLICY for m in wire[0]['input']))
                messages = wire[1]['input']
                output = next(m for m in messages if m.get('type') == 'function_call_output' and m['call_id'] == 'profile')
                value = json.loads(output['output'])
                self.assertTrue(value['success'])
                self.assertEqual(value['data'], {name: before[name] for name in PatientProfileOutput.model_fields})
                policies = [m for m in messages if m.get('content') == PATIENT_PROFILE_DATA_POLICY]
                self.assertEqual(len(policies), 1)
                self.assertEqual(policies[0]['role'], 'system')
                self.assertGreater(messages.index(policies[0]), messages.index(output))
                for body in wire:
                    serialized = json.dumps(body, ensure_ascii=False)
                    for private in (str(raw.id), str(raw.caregiver_id), 'created_at', 'updated_at'):
                        self.assertNotIn(private, serialized)
                if not sparse:
                    self.assertFalse(any(raw.name in m.get('content','') for m in messages if m.get('role') == 'system'))
                lookup.assert_called_once_with(CONTEXT.patient_id)
                self.assertEqual(raw.model_dump(mode='json'), before)

    def test_sdk_absent_wrong_owner_db_error_and_unexposed_call_never_deliver_policy(self):
        for kind in ('absent','owner','db','no_patient'):
            with self.subTest(kind=kind):
                context = CONTEXT.model_copy(update={'patient_id':None}) if kind=='no_patient' else CONTEXT
                lookup = (Mock(side_effect=RuntimeError('PRIVATE_PATIENT_CONTENT')) if kind=='db' else
                          Mock(return_value=None if kind=='absent' else patient(caregiver_id=UUID(int=988))))
                result, wire = self.execute_sdk(lookup, context=context)
                self.assertEqual(result.execution.status, 'completed')
                output = next(m for m in wire[1]['input'] if m.get('type')=='function_call_output')
                value = json.loads(output['output'])
                self.assertFalse(value['success'])
                self.assertIsNone(value['data'])
                self.assertEqual(value['error']['code'], 'unknown_tool' if kind=='no_patient' else 'tool_exception')
                self.assertFalse(any(m.get('content')==PATIENT_PROFILE_DATA_POLICY for body in wire for m in body['input']))
                self.assertNotIn('PRIVATE_PATIENT_CONTENT', json.dumps(wire))
                if kind=='no_patient': lookup.assert_not_called()
                else: lookup.assert_called_once_with(CONTEXT.patient_id)

    def test_production_search_and_patient_lookup_can_run_in_parallel_without_changing_call_order(self):
        barrier = Barrier(2)
        def lookup(key):
            barrier.wait(2)
            self.assertEqual(key, CONTEXT.patient_id)
            return patient()
        retriever = Mock(spec=EvidenceRetriever)
        def retrieve(**kwargs):
            barrier.wait(2)
            return package(['Synthetic PMID'])
        retriever.search_evidence.side_effect = retrieve
        factory = partial(build_chat_registry, retriever=retriever,
                          patient_reader=PatientProfileAdapter(get_patient_fn=lookup))
        calls = [chat_call(identifier='profile'), chat_call(ToolName.SEARCH_EVIDENCE, identifier='search', args={'query':'Synthetic','top_k':1})]
        client = FakeLLMClient([ModelTurn(tool_calls=calls), chat_final(selected=[True])])
        result = ChatExecutionV1(client, factory)(CONTEXT, prepare_chat_input(CONTEXT,'프로필과 연구를 확인해 주세요.'))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(result.execution.errors, [])
        self.assertEqual([m.call_id for m in client.requests[1][0] if m.type=='function_call_output'], ['profile','search'])
        self.assertEqual(sum(m.content==PATIENT_PROFILE_DATA_POLICY for m in client.requests[1][0]),1)
        self.assertEqual(result.sources[0].source_ref.tool_call_id,'search')
