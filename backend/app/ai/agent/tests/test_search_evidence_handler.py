"""Thin Handler/production composition with the unchanged ToolExecutor."""
import inspect
import json
import threading
from unittest import TestCase
from unittest.mock import Mock

from ..adapters.rag_evidence import RagEvidenceRetriever
from ..composition import build_evidence_registry
from ..executor import ToolExecutor
from ..registry import TOOL_CONTRACTS, ToolName, production_registry
from ..tools.contracts import EvidencePackage, EvidenceSearchInput
from ..tools.evidence_tools import EvidenceRetriever
from ..tools.search_evidence import SearchEvidenceHandler
from .test_search_evidence_adapter import package
from .test_workflows import CONTEXT, call


class SearchEvidenceHandlerTests(TestCase):
    def test_handler_forwards_exact_validated_values_and_context_and_returns_same_result(self):
        result = EvidencePackage.model_validate(package().model_dump(mode="json"))
        retriever = Mock(spec=EvidenceRetriever)
        retriever.search_evidence.return_value = result
        args = EvidenceSearchInput(query=" \tSynthetic query\n", top_k=20)
        context = CONTEXT.model_copy(update={"patient_id": None})
        returned = SearchEvidenceHandler(retriever)(context, args)
        retriever.search_evidence.assert_called_once_with(query=args.query, top_k=20, context=context)
        self.assertIs(retriever.search_evidence.call_args.kwargs["context"], context)
        self.assertIs(returned, result)
        self.assertIs(inspect.signature(SearchEvidenceHandler.__init__).parameters["retriever"].annotation,
                      EvidenceRetriever)

    def test_production_factory_uses_only_canonical_search_without_test_mode(self):
        registry = build_evidence_registry()
        self.assertEqual(registry.mode, "production")
        self.assertEqual(registry.definitions(), [TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE].definition()])
        spec = registry.get("search_evidence")
        self.assertIs(spec.contract, TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE])
        self.assertFalse(spec.test_only)
        self.assertIsInstance(spec.handler, SearchEvidenceHandler)
        self.assertIsInstance(spec.handler.retriever, RagEvidenceRetriever)
        self.assertIsNone(spec.handler.retriever.search_fn)
        self.assertEqual(production_registry().definitions(), [])

    def test_executor_success_and_trace_with_no_patient_scope(self):
        search = Mock(return_value=package())
        registry = build_evidence_registry(retriever=RagEvidenceRetriever(search_fn=search))
        outcome, trace = ToolExecutor(registry).execute(call(ToolName.SEARCH_EVIDENCE,
            {"query": "synthetic", "top_k": 1}), CONTEXT.model_copy(update={"patient_id": None}))
        self.assertTrue(outcome.success)
        self.assertIsNone(outcome.error)
        expected = package().model_dump(mode="json")
        for item in expected['evidence']:
            item.setdefault('evidence_grade', None)
        self.assertEqual(outcome.data, expected)
        self.assertEqual((outcome.call_id, outcome.tool_name), ("call_1", "search_evidence"))
        self.assertEqual((trace.success, trace.result_count, trace.error_type), (True, 2, None))
        search.assert_called_once_with(query="synthetic", k=1)

    def test_invalid_input_is_rejected_before_retriever(self):
        retriever = Mock(spec=EvidenceRetriever)
        executor = ToolExecutor(build_evidence_registry(retriever=retriever))
        invalid = ["{broken", {"query": " \t\n"}, {"query": "x" * 2001}, {},
                   *({"query": "synthetic", "top_k": value} for value in (0, 21, True, "5", 5.0)),
                   {"query": "synthetic", "patient_id": "PRIVATE"},
                   {"query": "synthetic", "exclude_animal": False}]
        for args in invalid:
            with self.subTest(args=args):
                outcome, _ = executor.execute(call(ToolName.SEARCH_EVIDENCE, args), CONTEXT)
                self.assertFalse(outcome.success)
                self.assertEqual(outcome.error.code, "invalid_arguments")
        retriever.search_evidence.assert_not_called()

    def test_rag_error_uses_existing_tool_exception_and_metadata_only_logs(self):
        search = Mock(side_effect=RuntimeError("PRIVATE query/evidence/DB detail"))
        executor = ToolExecutor(build_evidence_registry(retriever=RagEvidenceRetriever(search_fn=search)))
        with self.assertLogs("app.ai.agent.executor", level="INFO") as captured:
            outcome, trace = executor.execute(call(ToolName.SEARCH_EVIDENCE,
                {"query": "PRIVATE query"}), CONTEXT)
        self.assertFalse(outcome.success)
        self.assertIsNone(outcome.data)
        self.assertEqual(outcome.error.code, "tool_exception")
        self.assertEqual(trace.error_type, "tool_exception")
        self.assertNotIn("PRIVATE", json.dumps(outcome.model_dump(mode="json")))
        self.assertNotIn("PRIVATE", "\n".join(captured.output))
        search.assert_called_once_with(query="PRIVATE query", k=5)

    def test_adapter_conversion_failure_is_an_output_contract_failure(self):
        raw = package()
        raw.evidence[0].relevance_score = 2.0
        executor = ToolExecutor(build_evidence_registry(retriever=RagEvidenceRetriever(search_fn=Mock(return_value=raw))))
        outcome, _ = executor.execute(call(ToolName.SEARCH_EVIDENCE, {"query": "synthetic"}), CONTEXT)
        self.assertFalse(outcome.success)
        self.assertIsNone(outcome.data)
        self.assertEqual(outcome.error.code, "invalid_output")

    def test_executor_output_validation_rejects_invalid_retriever_return(self):
        for raw in ({"query": "synthetic", "evidence": "PRIVATE invalid"},
                    EvidencePackage.model_construct(query="synthetic", evidence="PRIVATE invalid")):
            retriever = Mock(spec=EvidenceRetriever)
            retriever.search_evidence.return_value = raw
            outcome, _ = ToolExecutor(build_evidence_registry(retriever=retriever)).execute(
                call(ToolName.SEARCH_EVIDENCE, {"query": "synthetic"}), CONTEXT)
            self.assertFalse(outcome.success)
            self.assertIsNone(outcome.data)
            self.assertEqual(outcome.error.code, "invalid_output")

    def test_explicit_rag_timeout_keeps_executor_timeout_classification(self):
        search = Mock(side_effect=TimeoutError("PRIVATE timeout"))
        outcome, trace = ToolExecutor(build_evidence_registry(
            retriever=RagEvidenceRetriever(search_fn=search))).execute(
                call(ToolName.SEARCH_EVIDENCE, {"query": "synthetic"}), CONTEXT)
        self.assertEqual(outcome.error.code, "tool_timeout")
        self.assertEqual(trace.error_type, "tool_timeout")
        self.assertIsNone(outcome.data)
        search.assert_called_once()

    def test_blocking_retrieval_uses_existing_executor_wait_timeout(self):
        entered, release = threading.Event(), threading.Event()
        def slow_search(**kwargs):
            entered.set()
            release.wait(2)
            return package()
        search = Mock(side_effect=slow_search)
        try:
            outcome, _ = ToolExecutor(build_evidence_registry(
                retriever=RagEvidenceRetriever(search_fn=search)), timeout_seconds=0.05).execute(
                    call(ToolName.SEARCH_EVIDENCE, {"query": "synthetic"}), CONTEXT)
            self.assertTrue(entered.is_set())
            self.assertEqual(outcome.error.code, "tool_timeout")
            self.assertIsNone(outcome.data)
            search.assert_called_once()
        finally:
            release.set()

    def test_diagnostics_redact_unknown_keys_values_and_validator_messages(self):
        executor = ToolExecutor(build_evidence_registry(retriever=Mock(spec=EvidenceRetriever)))
        cases = [({}, 'query', 'required_field_missing'),
                 ({'query':'PRIVATE', 'top_k':0}, 'top_k', 'ge=1'),
                 ({'query':'PRIVATE', 'PRIVATE_field':'PRIVATE'}, '<unknown>', 'unexpected_field'),
                 ('{"query":"PRIVATE"', None, 'invalid_json_syntax')]
        for args, field, condition in cases:
            with self.subTest(condition=condition), self.assertLogs('app.ai.agent.executor', level='INFO') as logs:
                outcome, _ = executor.execute(call(ToolName.SEARCH_EVIDENCE,args), CONTEXT)
            text = '\n'.join(logs.output)
            self.assertIn(condition, text)
            if field: self.assertIn(field, outcome.error.message)
            self.assertNotIn('PRIVATE', text + outcome.model_dump_json())
            self.assertIn('tool_call_id=', text)
            self.assertIn('input_origin=llm_arguments', text)

    def test_conversion_failure_has_own_stage_and_output_error_code(self):
        raw = package()
        raw.evidence[0].relevance_score = 2.0
        executor = ToolExecutor(build_evidence_registry(retriever=RagEvidenceRetriever(search_fn=Mock(return_value=raw))))
        with self.assertLogs('app.ai.agent.adapters.rag_evidence', level='INFO') as logs:
            outcome, _ = executor.execute(call(ToolName.SEARCH_EVIDENCE, {'query':'PRIVATE'}), CONTEXT)
        text = '\n'.join(logs.output)
        self.assertIn('stage=rag_result_conversion', text)
        self.assertIn('relevance_score', text)
        self.assertNotIn('PRIVATE', text)
        self.assertEqual(outcome.error.code, 'invalid_output')

    def test_executor_log_id_is_safe_stable_and_does_not_change_outcome_id(self):
        from ..executor import diagnostic_call_id
        identifier = 'PRIVATE patient identifier\n' + 'x' * 5000
        executor = ToolExecutor(build_evidence_registry(retriever=Mock(spec=EvidenceRetriever)))
        with self.assertLogs('app.ai.agent.executor', level='INFO') as logs:
            outcome, _ = executor.execute(call(ToolName.SEARCH_EVIDENCE, {}, identifier), CONTEXT)
        self.assertEqual(outcome.call_id, identifier)
        self.assertNotIn('PRIVATE', '\n'.join(logs.output))
        self.assertNotIn('x' * 100, '\n'.join(logs.output))
        self.assertIn(diagnostic_call_id(identifier), '\n'.join(logs.output))
        self.assertEqual(diagnostic_call_id(identifier), diagnostic_call_id(identifier))
        self.assertNotEqual(diagnostic_call_id(identifier), diagnostic_call_id(identifier + 'other'))

    def test_union_diagnostics_preserve_field_paths_but_hide_extra_keys(self):
        from pydantic import ValidationError
        from ..feed_search_policy import FeedSearchInput
        from ..error_diagnostics import summarize_validation_error
        try:
            FeedSearchInput.model_validate({'queries':['a'], 'review':{'decisions':[
                {'include':False}, {'include':True, 'PRIVATE':'PRIVATE'}]}})
        except ValidationError as exc:
            summary = summarize_validation_error(exc, FeedSearchInput)
        fields = [issue['field'] for issue in summary['issues']]
        self.assertIn('review.decisions[1].personal_reason', fields)
        self.assertIn('review.decisions[1].category', fields)
        self.assertIn('review.decisions[1].<unknown>', fields)
        self.assertNotIn('PRIVATE', json.dumps(summary))

    def test_chat_keeps_recovering_from_permission_and_output_failures(self):
        from psycopg import Error
        from psycopg.errors import InsufficientPrivilege
        from ..agent_loop_runner import AgentLoopRunner
        from ..outputs import ChatAnswerV1
        from ..testing.fake_llm import FakeLLMClient
        from ..testing.chat_workflow_fakes import chat_final
        from ..schemas import ModelTurn
        for mode in ('python_permission', 'driver_permission', 'sqlstate', 'adapter_output', 'executor_output', 'other_db'):
            with self.subTest(mode=mode):
                def search(**kwargs):
                    if mode == 'python_permission': raise PermissionError('PRIVATE')
                    if mode == 'driver_permission': raise InsufficientPrivilege('PRIVATE')
                    if mode == 'sqlstate': raise Error('PRIVATE', info={ord('C'):b'42501'})
                    if mode == 'other_db': raise Error('PRIVATE', info={ord('C'):b'42601'})
                    raw = package()
                    raw.evidence[0].relevance_score = 2.0
                    return raw
                if mode == 'executor_output':
                    retriever = Mock(spec=EvidenceRetriever)
                    retriever.search_evidence.side_effect = search
                else:
                    retriever = RagEvidenceRetriever(search_fn=search)
                client = FakeLLMClient([ModelTurn(tool_calls=[call(ToolName.SEARCH_EVIDENCE, {'query':'a'})]), chat_final()])
                result = AgentLoopRunner(client, build_evidence_registry(retriever=retriever)).run_structured(
                    'Synthetic', CONTEXT, output_model=ChatAnswerV1)
                self.assertEqual(result.execution.status, 'completed')
                self.assertEqual(len(client.requests), 2)
                self.assertEqual(result.execution.errors[-1].code,
                    'invalid_output' if mode.endswith('output') else 'tool_exception')
                self.assertNotIn('PRIVATE', result.model_dump_json())

    def test_search_and_conversion_timeouts_preserve_identity_and_chat_stop_policy(self):
        import httpx
        from openai import APITimeoutError
        from pydantic import BaseModel
        from .._execution import RequestTimeout
        from ..agent_loop_runner import AgentLoopRunner
        from ..outputs import ChatAnswerV1
        from ..schemas import ModelTurn
        from ..testing.fake_llm import FakeLLMClient
        from ..testing.chat_workflow_fakes import chat_final
        cases = [TimeoutError('PRIVATE'), RequestTimeout('PRIVATE'),
                 httpx.ReadTimeout('PRIVATE'),
                 APITimeoutError(request=httpx.Request('GET', 'https://example.invalid'))]
        for error in cases:
            for stage in ('search', 'conversion'):
                with self.subTest(error=type(error).__name__, stage=stage):
                    class TimedOutput(BaseModel):
                        def model_dump(self, *args, **kwargs):
                            raise error
                    search = (Mock(side_effect=error) if stage == 'search'
                              else Mock(return_value=TimedOutput()))
                    adapter = RagEvidenceRetriever(search_fn=search)
                    with self.assertNoLogs('app.ai.agent.adapters.rag_evidence', level='INFO'), \
                         self.assertRaises(type(error)) as caught:
                        adapter.search_evidence(query='synthetic', top_k=1, context=CONTEXT)
                    self.assertIs(caught.exception, error)
                    client = FakeLLMClient([ModelTurn(tool_calls=[call(ToolName.SEARCH_EVIDENCE,
                        {'query':'synthetic'})]), chat_final()])
                    result = AgentLoopRunner(client, build_evidence_registry(retriever=adapter)).run_structured(
                        'Synthetic', CONTEXT, output_model=ChatAnswerV1)
                    self.assertEqual(result.execution.status, 'failed')
                    self.assertEqual(result.execution.errors[-1].code,
                        'request_timeout' if isinstance(error, RequestTimeout) else 'tool_timeout')
                    self.assertEqual(len(client.requests), 1)
                    self.assertIsNone(result.output)
                    self.assertEqual(result.sources, [])
                    self.assertNotIn('PRIVATE', result.model_dump_json())

    def test_precise_validation_reasons_come_from_contracts_without_private_values(self):
        from pydantic import ValidationError
        from ..error_diagnostics import diagnostic_message, summarize_validation_error
        from ..feed_search_policy import FeedSearchInput
        from ..tools.contracts import GuidelineSearchInput
        decision = {'include':True, 'personal_reason':'synthetic', 'category':'care'}
        cases = [
            (EvidenceSearchInput, {'query':' '}, 'query', 'blank_query', 'non-whitespace'),
            (GuidelineSearchInput, {'query':' '}, 'query', 'blank_query', 'non-whitespace'),
            (FeedSearchInput, {'queries':['a'],'review':{'decisions':[{**decision,'category':'PRIVATE'}]}},
             'review.decisions[0].category', 'invalid_literal', '"care"'),
            (FeedSearchInput, {'queries':['a'],'review':{'decisions':[{'include':'PRIVATE'}]}},
             'review.decisions[0].include', 'invalid_discriminator', '[true, false]'),
            (FeedSearchInput, {'queries':['a'],'review':{'decisions':[{}]}},
             'review.decisions[0].include', 'missing_discriminator', '[true, false]'),
            (FeedSearchInput, {'queries':['a'],'review':{'decisions':[{'include':1}]}},
             'review.decisions[0].include', 'wrong_type', 'boolean include'),
            (FeedSearchInput, {'queries':['a'],'review':{'decisions':[decision]*6}},
             'review.decisions', 'too_many_retained', 'max_retained=5'),
            (FeedSearchInput, {'queries':['a'],'review':{'decisions':[{**decision,'personal_reason':' '}]}},
             'review.decisions[0].personal_reason', 'blank_content', 'visible non-whitespace'),
        ]
        for model, payload, field, reason, expected in cases:
            with self.subTest(reason=reason, model=model.__name__):
                with self.assertRaises(ValidationError) as caught:
                    model.model_validate(payload)
                summary = summarize_validation_error(caught.exception, model)
                message = diagnostic_message(summary)
                self.assertEqual(summary['issues'][0]['field'], field)
                self.assertEqual(summary['issues'][0]['reason'], reason)
                self.assertIn(expected, message)
                self.assertNotIn('PRIVATE', json.dumps(summary) + message)

    def test_chat_executor_origin_distinguishes_prepared_search_from_other_tools(self):
        from ..agent_loop_runner import AgentLoopRunner
        from ..executor import tool_diagnostic_context
        from ..outputs import ChatAnswerV1
        from ..registry import ToolRegistry, ToolSpec
        from ..schemas import ModelTurn
        from ..testing.fake_llm import FakeLLMClient
        from ..testing.chat_workflow_fakes import chat_final
        from ..tools.contracts import PatientProfileOutput
        tools = ToolRegistry((
            ToolSpec(TOOL_CONTRACTS[ToolName.PATIENT_PROFILE],lambda c,a:PatientProfileOutput()),
            ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE],lambda c,a:EvidencePackage(query='a',evidence=[])),
            ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_GUIDELINES],lambda c,a:EvidencePackage(query='a',evidence=[]))))
        client = FakeLLMClient([ModelTurn(tool_calls=[call(identifier='profile'),
            call(ToolName.SEARCH_EVIDENCE,{'query':'a'},'search'),
            call(ToolName.SEARCH_GUIDELINES,{'query':'a'},'guidelines')]), chat_final()])
        runner = AgentLoopRunner(client,tools)
        observed = []
        original = runner.executor.execute
        def capture(call,context):
            observed.append(tool_diagnostic_context.get()['input_origin'])
            return original(call,context)
        from unittest.mock import patch
        before = tool_diagnostic_context.get()
        with patch.object(runner.executor,'execute',side_effect=capture):
            result = runner.run_structured('Synthetic',CONTEXT,output_model=ChatAnswerV1)
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(observed,['llm_arguments','chat_search_plan','chat_search_plan'])
        self.assertIs(tool_diagnostic_context.get(),before)

    def test_unregistered_chat_search_keeps_llm_origin_and_unknown_tool_recovery(self):
        from ..agent_loop_runner import AgentLoopRunner
        from ..outputs import ChatAnswerV1
        from ..registry import ToolRegistry
        from ..schemas import ModelTurn
        from ..testing.fake_llm import FakeLLMClient
        from ..testing.chat_workflow_fakes import chat_final
        for name in (ToolName.SEARCH_EVIDENCE,ToolName.SEARCH_GUIDELINES):
            with self.subTest(tool=name):
                client=FakeLLMClient([ModelTurn(tool_calls=[call(name,{'query':'a'},'PRIVATE-call')]),chat_final()])
                with self.assertLogs('app.ai.agent.executor',level='INFO') as logs:
                    result=AgentLoopRunner(client,ToolRegistry()).run_structured(
                        'Synthetic',CONTEXT,output_model=ChatAnswerV1)
                self.assertEqual(result.execution.status,'completed')
                self.assertEqual([e.code for e in result.execution.errors],['unknown_tool'])
                self.assertEqual(len(client.requests),2)
                outputs=[m for m in client.requests[1][0] if m.type=='function_call_output']
                self.assertEqual([m.call_id for m in outputs],['PRIVATE-call'])
                envelope=json.loads(outputs[0].content)
                self.assertFalse(envelope['success'])
                self.assertEqual(envelope['error']['code'],'unknown_tool')
                text='\n'.join(logs.output)
                self.assertIn('input_origin=llm_arguments',text)
                self.assertNotIn('chat_search_plan',text)
                self.assertNotIn('PRIVATE',text)

    def test_blank_query_condition_reaches_existing_chat_recovery(self):
        from ..agent_loop_runner import AgentLoopRunner
        from ..outputs import ChatAnswerV1
        from ..schemas import ModelTurn
        from ..testing.fake_llm import FakeLLMClient
        from ..testing.chat_workflow_fakes import chat_final
        retriever=Mock(spec=EvidenceRetriever)
        retriever.search_evidence.return_value=EvidencePackage(query='a',evidence=[])
        client=FakeLLMClient([ModelTurn(tool_calls=[call(ToolName.SEARCH_EVIDENCE,{'query':' '},'bad')]),
            ModelTurn(tool_calls=[call(ToolName.SEARCH_EVIDENCE,{'query':'a'},'fixed')]),chat_final()])
        result=AgentLoopRunner(client,build_evidence_registry(retriever=retriever)).run_structured(
            'Synthetic',CONTEXT,output_model=ChatAnswerV1)
        self.assertEqual(result.execution.status,'completed')
        retriever.search_evidence.assert_called_once()
        envelope=json.loads(next(m.content for m in client.requests[1][0] if m.type=='function_call_output'))
        self.assertEqual(envelope['error']['code'],'invalid_arguments')
        self.assertIn('query: blank_query (non-whitespace string)',envelope['error']['message'])

    def test_chat_policy_refusals_log_reasons_and_preserve_existing_loop(self):
        from ..agent_loop_runner import AgentLoopRunner
        from ..outputs import ChatAnswerV1
        from ..schemas import ModelTurn
        from ..testing.fake_llm import FakeLLMClient
        from ..testing.chat_workflow_fakes import chat_final
        from .test_feed_candidates import package as candidates
        cases = [
            ([ModelTurn(tool_calls=[call('PRIVATE_TOOL', {}, 'PRIVATE-unknown')]), chat_final()],
             ['unregistered_tool'], 1, 0),
            ([ModelTurn(tool_calls=[call(ToolName.SEARCH_EVIDENCE, {'query':'a'}, 'PRIVATE-first'),
                                   call(ToolName.SEARCH_EVIDENCE, {'query':'b'}, 'PRIVATE-reserved')]),
              chat_final(selected=[False]*3)],
             ['reserved_budget_full'], 1, 1),
            ([ModelTurn(tool_calls=[call(ToolName.SEARCH_EVIDENCE, {'query':'a'}, 'first')]),
              ModelTurn(tool_calls=[call(ToolName.SEARCH_EVIDENCE, {'query':'b'}, 'PRIVATE-full'),
                                    call(ToolName.SEARCH_EVIDENCE, {'query':'c'}, 'PRIVATE-closed')]),
              chat_final(selected=[False]*3)],
             ['retained_budget_full','search_closed_within_turn'], 1, 1),
        ]
        for turns, reasons, executions, searches in cases:
            with self.subTest(reasons=reasons):
                retriever=Mock(spec=EvidenceRetriever)
                retriever.search_evidence.return_value=candidates(['a','b','c'])
                client=FakeLLMClient(turns)
                with self.assertLogs('app.ai.agent',level='INFO') as logs:
                    result=AgentLoopRunner(client,build_evidence_registry(retriever=retriever)).run_structured(
                        'Synthetic',CONTEXT,output_model=ChatAnswerV1)
                self.assertEqual(result.execution.status,'completed')
                self.assertEqual(result.execution.total_tool_calls,executions)
                self.assertEqual(retriever.search_evidence.call_count,searches)
                text='\n'.join(logs.output)
                for reason in reasons:
                    self.assertIn(reason,text)
                self.assertIn('stage=tool_admission' if not searches else 'stage=chat_search_plan',text)
                self.assertNotIn('PRIVATE',text)
                expected_ids=[c.id for t in turns for c in t.tool_calls]
                self.assertEqual([m.call_id for m in client.requests[-1][0] if m.type=='function_call_output'],
                                 expected_ids)
