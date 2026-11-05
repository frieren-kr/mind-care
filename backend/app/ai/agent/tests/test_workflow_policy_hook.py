"""Request-local Workflow policies over validated outcomes, never live Provider I/O."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from threading import Barrier, Event, Lock
import unittest
from unittest.mock import Mock, patch

from ..agent_loop_runner import AgentLoopRunner
from ..conversation import Conversation
from ..feed_candidates import build_feed_decision_policy
from ..outputs import ChatAnswerV1, FeedAnswerV2
from ..prompts import DefaultPromptBuilder, build_structured_output_policy
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..schemas import Message, ModelTurn, ToolCall
from ..testing.fake_llm import FakeLLMClient
from ..tools.search_evidence import SEARCH_EVIDENCE_DATA_POLICY
from ..workflows.chat_prompts import CHAT_SEARCH_EVIDENCE_SUCCESS_POLICY, ChatPromptBuilder
from ..workflows.feed_execution import FeedExecutionV1
from ..workflows.feed_prompts import FeedPromptBuilder
from .test_feed_candidates import decisions, package
from .test_feed_review_rounds import review
from .test_responses import function, response
from . import test_search_evidence_policy as search_policy_tests
from .test_workflows import CONTEXT


class WorkflowPolicyHookTests(unittest.TestCase):
    setUp = search_policy_tests.SearchEvidencePolicyTests.setUp
    execute = search_policy_tests.SearchEvidencePolicyTests.execute
    final = staticmethod(search_policy_tests.SearchEvidencePolicyTests.final)
    projection = search_policy_tests.SearchEvidencePolicyTests.projection

    def assert_hook(self, wire, channel, active_from=1):
        policy = (CHAT_SEARCH_EVIDENCE_SUCCESS_POLICY if channel == 'chat'
                  else build_feed_decision_policy().content)
        other = (build_feed_decision_policy().content if channel == 'chat'
                 else CHAT_SEARCH_EVIDENCE_SUCCESS_POLICY)
        for index, body in enumerate(wire):
            messages = body['input']
            policies = [m for m in messages if m.get('content') == policy]
            self.assertEqual(len(policies), int(index >= active_from))
            self.assertFalse(any(m.get('content') == other for m in messages))
            if policies:
                self.assertEqual(policies[0]['role'], 'system')
                self.assertLess(messages.index(policies[0]), next(i for i, m in enumerate(messages) if m.get('role') == 'user'))
                self.assertEqual(sum(m.get('content') == SEARCH_EVIDENCE_DATA_POLICY for m in messages), 1)
                results = [m for m in messages if m.get('type') == 'function_call_output']
                data_policy = next(m for m in messages if m.get('content') == SEARCH_EVIDENCE_DATA_POLICY)
                self.assertGreater(messages.index(data_policy), messages.index(results[0]))
                self.assertEqual([m['call_id'] for m in results],
                                 [m['call_id'] for m in messages if m.get('type') == 'function_call'])

    def test_default_builder_is_noop_and_feed_hook_is_exact_canonical_policy(self):
        for names in (frozenset(), frozenset({'search_evidence'}), frozenset({'get_patient_profile'})):
            self.assertEqual(DefaultPromptBuilder().build_post_tool_success_policies(names), [])
        self.assertEqual(FeedPromptBuilder().build_post_tool_success_policies(frozenset()), [])
        self.assertEqual(FeedPromptBuilder().build_post_tool_success_policies(frozenset({'search_evidence'})),
                         [build_feed_decision_policy()])
        self.assertEqual(ChatPromptBuilder().build_post_tool_success_policies(frozenset({'get_patient_profile'})), [])

    def test_unsupported_feed_builders_raise_before_provider_and_tools(self):
        class LegacyBuilder:
            build_initial_messages = DefaultPromptBuilder().build_initial_messages
            build_final_response_context = DefaultPromptBuilder().build_final_response_context
        for builder in (None, DefaultPromptBuilder(), LegacyBuilder(), ChatPromptBuilder()):
            with self.subTest(builder=type(builder).__name__):
                client, handler = Mock(), Mock()
                registry = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], handler),))
                runner = AgentLoopRunner(client, registry, prompt_builder=builder)
                with patch.object(runner, '_run') as loop, patch.object(runner.executor, 'execute') as execute:
                    with self.assertRaisesRegex(ValueError, 'FeedAnswerV2 requires FeedPromptBuilder'):
                        runner.run_structured('synthetic', CONTEXT, output_model=FeedAnswerV2)
                    loop.assert_not_called()
                    execute.assert_not_called()
                client.generate.assert_not_called()
                handler.assert_not_called()

    def test_default_and_legacy_builders_still_allow_general_and_chat_execution(self):
        class LegacyBuilder:
            build_initial_messages = DefaultPromptBuilder().build_initial_messages
            build_final_response_context = DefaultPromptBuilder().build_final_response_context
        for builder in (DefaultPromptBuilder(), LegacyBuilder()):
            with self.subTest(builder=type(builder).__name__):
                client = FakeLLMClient([ModelTurn(text='done'), ModelTurn(text='{"answer":"done","selected":[]}')])
                runner = AgentLoopRunner(client, ToolRegistry(), prompt_builder=builder)
                self.assertEqual(runner.run('synthetic', CONTEXT).status, 'completed')
                self.assertEqual(runner.run_structured('synthetic', CONTEXT, output_model=ChatAnswerV1).execution.status, 'completed')

    def test_feed_builder_configuration_is_checked_on_every_structured_run(self):
        client = FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id='one', name='search_evidence', arguments={'queries':['one']})]),
                                ModelTurn(text=json.dumps(decisions(0)))])
        registry = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], lambda c,a: package([])),))
        runner = AgentLoopRunner(client, registry, prompt_builder=FeedPromptBuilder())
        self.assertEqual(runner.run_structured('synthetic', CONTEXT, output_model=FeedAnswerV2).execution.status, 'completed')
        runner.prompt_builder = DefaultPromptBuilder()
        with self.assertRaisesRegex(ValueError, 'FeedAnswerV2 requires FeedPromptBuilder'):
            runner.run_structured('synthetic', CONTEXT, output_model=FeedAnswerV2)
        self.assertEqual(len(client.requests), 2)

    def test_no_search_direct_answer_keeps_schema_and_has_no_workflow_policy(self):
        result, wire = self.execute('chat', [self.final('chat', 0)], Mock(side_effect=AssertionError('No Tool')))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(result.output.citation_refs, [])
        self.assert_hook(wire, 'chat')
        self.assertIn('"title": "ChatDraft"', '\n'.join(m.get('content', '') for m in wire[0]['input']))
        self.assertIn('selected=[]', build_structured_output_policy(ChatAnswerV1).content)

    def test_other_tool_success_does_not_activate_search_workflow_policy(self):
        profile = TOOL_CONTRACTS[ToolName.PATIENT_PROFILE]
        specs = (ToolSpec(profile, lambda c, a: {'name': None, 'dementia_stage': None,
                 'diagnosis_date': None, 'symptoms': [], 'interests': []}, test_only=True),)
        native = {**function('profile', '{}'), 'name': profile.name.value}
        result, wire = self.execute('chat', [response([native]), self.final('chat', 0)], None, specs=specs)
        self.assertEqual(result.execution.status, 'completed')
        self.assert_hook(wire, 'chat', active_from=len(wire))
        self.assertEqual(sum(m.get('content') == profile.data_policy for m in wire[1]['input']), 1)

    def test_success_and_normal_zero_activate_both_workflows(self):
        for channel in ('chat', 'feed'):
            for count in (0, 1):
                with self.subTest(channel=channel, count=count):
                    result, wire = self.execute(channel, [response([function()]), self.final(channel, count, range(count))],
                                                lambda c, a: package(['A'] if count else []))
                    self.assertEqual(result.execution.status, 'completed')
                    self.assert_hook(wire, channel)
                    self.assertEqual(self.projection(wire[1], channel)['candidate_count'], count)
                    if channel == 'feed':
                        self.assertEqual(wire[0]['tool_choice'], 'required')

    def test_repeated_searches_use_one_hook_and_current_snapshot(self):
        for channel in ('chat', 'feed'):
            with self.subTest(channel=channel):
                second = {'query': 'two'} if channel == 'chat' else {'queries': ['two'], 'review': review(1, (0,))}
                result, wire = self.execute(channel, [response([function('one')]),
                    response([function('two', json.dumps(second))]), self.final(channel, 2, (0, 1))],
                    lambda c, a: package([a.query]))
                self.assertEqual(result.execution.status, 'completed')
                self.assert_hook(wire, channel)
                self.assertEqual(self.projection(wire[2], channel)['candidate_count'], 2)
                self.assertEqual([s.source_ref.tool_call_id for s in result.sources],
                                 ['one', 'two'] if channel == 'chat' else ['one', 'feed-rag:2:0:0'])

    def test_feed_batch_preserves_one_provider_result_and_policy(self):
        result, wire = self.execute('feed', [response([function('batch', '{"queries":["one","two"]}')]),
            self.final('feed', 2, (0, 1))], lambda c, a: package([a.query]))
        self.assertEqual(result.execution.status, 'completed')
        self.assert_hook(wire, 'feed')
        self.assertEqual([s.source_ref.tool_call_id for s in result.sources], ['feed-rag:1:0:0', 'feed-rag:1:0:1'])
        self.assertEqual([m['call_id'] for m in wire[1]['input'] if m.get('type') == 'function_call_output'], ['batch'])

    def test_failed_input_handler_and_output_do_not_activate_hook(self):
        for channel in ('chat', 'feed'):
            for failure in ('input', 'handler', 'output'):
                with self.subTest(channel=channel, failure=failure):
                    handler = Mock(side_effect=RuntimeError('private')) if failure == 'handler' else Mock(return_value={})
                    args = '{"query":""}' if failure == 'input' else '{"query":"synthetic"}'
                    result, wire = self.execute(channel, [response([function('bad', args)]), self.final(channel, 0)], handler)
                    self.assert_hook(wire, channel, active_from=len(wire))
                    self.assertEqual(result.sources, [])
                    self.assertTrue(result.execution.errors)
                    if failure == 'input':
                        handler.assert_not_called()

    def test_timeout_never_activates_hook(self):
        for channel in ('chat', 'feed'):
            with self.subTest(channel=channel):
                release = Event()
                def handler(c, a):
                    release.wait(1)
                    return package(['late'])
                try:
                    result, wire = self.execute(channel, [response([function()])], handler, tool_timeout_seconds=0.02)
                finally:
                    release.set()
                self.assertEqual(result.execution.status, 'failed')
                self.assertEqual(result.execution.errors[-1].code, 'tool_timeout')
                self.assert_hook(wire, channel)

    def test_chat_partial_failures_and_later_failure_keep_success_policy_and_source(self):
        for same_turn in (True, False):
            with self.subTest(same_turn=same_turn):
                def handler(c, a):
                    if a.query == 'bad': raise RuntimeError('private')
                    return package(['A'])
                good, bad = function('good', '{"query":"good","top_k":2}'), function('bad', '{"query":"bad","top_k":1}')
                turns = [response([bad, good])] if same_turn else [response([good]), response([function('bad', '{"query":"bad","top_k":2}')])]
                result, wire = self.execute('chat', turns + [self.final('chat', 1, (0,))], handler)
                self.assertEqual(result.execution.status, 'completed')
                self.assert_hook(wire, 'chat')
                self.assertEqual([s.source_ref.tool_call_id for s in result.sources], ['good'])
                self.assertEqual(self.projection(wire[-1], 'chat')['candidate_count'], 1)

    def test_feed_batch_failure_never_publishes_sibling_success_policy(self):
        result, wire = self.execute('feed', [response([function('batch', '{"queries":["good","bad"]}')])],
            lambda c, a: package(['A']) if a.query == 'good' else {})
        self.assertEqual(result.execution.status, 'failed')
        self.assertEqual(result.sources, [])
        self.assert_hook(wire, 'feed')

    def test_hook_success_state_is_independent_of_contract_data_policy_and_history(self):
        contract = replace(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], data_policy='')
        seen, history = [], []
        class Builder(DefaultPromptBuilder):
            def build_post_tool_success_policies(self, names):
                seen.append(names)
                return [Message(role='system', kind='policy', content=f'Workflow hook {len(seen)}')]
        append_turn = Conversation.append_turn
        def capture(conversation, *args, **kwargs):
            append_turn(conversation, *args, **kwargs)
            history.extend(conversation.snapshot())
        client = FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id=i, name='search_evidence', arguments={'query': i})])
                                for i in ('one', 'two')] + [ModelTurn(text='done')])
        with patch.dict(TOOL_CONTRACTS, {ToolName.SEARCH_EVIDENCE: contract}), patch.object(Conversation, 'append_turn', capture):
            registry = ToolRegistry((ToolSpec(contract, lambda c, a: package([]), test_only=True),), mode='test')
            result = AgentLoopRunner(client, registry, prompt_builder=Builder()).run('synthetic', CONTEXT)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(seen, [frozenset({'search_evidence'}), frozenset({'search_evidence'})])
        self.assertTrue(all(type(names) is frozenset for names in seen))
        self.assertFalse(any((m.content or '').startswith('Workflow hook') for m in history))
        for index, (messages, _) in enumerate(client.requests):
            self.assertEqual([m.content for m in messages if (m.content or '').startswith('Workflow hook')],
                             [f'Workflow hook {index}'] if index else [])

    def test_legacy_builder_without_new_method_still_runs(self):
        class LegacyBuilder:
            build_initial_messages = DefaultPromptBuilder().build_initial_messages
            build_final_response_context = DefaultPromptBuilder().build_final_response_context
        client = FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id='one', name='search_evidence', arguments={'query':'one'})]),
                                ModelTurn(text='done')])
        registry = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], lambda c,a: package([])),))
        result = AgentLoopRunner(client, registry, prompt_builder=LegacyBuilder()).run('synthetic', CONTEXT)
        self.assertEqual(result.status, 'completed')
        self.assertFalse(any(m.content == CHAT_SEARCH_EVIDENCE_SUCCESS_POLICY for m in client.requests[-1][0]))

    def test_reused_feed_runner_resets_hook_activation_after_failed_request(self):
        client = FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id='bad', name='search_evidence', arguments={'queries':['bad']})]),
                                ModelTurn(tool_calls=[ToolCall(id='good', name='search_evidence', arguments={'queries':['good']})]),
                                ModelTurn(text=json.dumps(decisions(0)))])
        registry = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE],
            lambda c,a: {} if a.query == 'bad' else package([])),))
        execution = FeedExecutionV1(client, registry)
        self.assertEqual(execution(CONTEXT, '{"patients":[]}').execution.status, 'failed')
        self.assertEqual(execution(CONTEXT, '{"patients":[]}').execution.status, 'completed')
        self.assertEqual([sum(m.content == build_feed_decision_policy().content for m in messages)
                          for messages, _ in client.requests], [0, 0, 1])

    def test_parallel_requests_on_same_runner_do_not_share_success_flags(self):
        barrier, lock, requests = Barrier(2), Lock(), []
        class Client:
            def generate(self, request):
                label = next(m.content for m in request.inputs if m.kind == 'user_input')
                searched = any(m.type == 'function_call_output' for m in request.inputs)
                with lock: requests.append((label, request.inputs))
                if not searched:
                    barrier.wait(2)
                    if label == 'search':
                        return ModelTurn(tool_calls=[ToolCall(id='one', name='search_evidence', arguments={'query':'one'})],
                                         output_items=[function('one', '{"query":"one"}')])
                return ModelTurn(text=json.dumps({'answer':'synthetic', 'selected':[True] if searched else []}))
        registry = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], lambda c,a: package(['A'])),))
        runner = AgentLoopRunner(Client(), registry, prompt_builder=ChatPromptBuilder())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda label: runner.run_structured(label, CONTEXT, output_model=ChatAnswerV1), ('search','direct')))
        self.assertTrue(all(r.execution.status == 'completed' for r in results))
        self.assertEqual([s.pmid for s in results[0].sources], ['A'])
        self.assertEqual(results[1].sources, [])
        for label, messages in requests:
            self.assertEqual(sum(m.content == CHAT_SEARCH_EVIDENCE_SUCCESS_POLICY for m in messages),
                             int(label == 'search' and any(m.type == 'function_call_output' for m in messages)))

    def test_invalid_hook_message_fails_before_next_provider_call(self):
        class Builder(DefaultPromptBuilder):
            def build_post_tool_success_policies(self, names):
                return [Message(role='user', kind='user_input', content='not policy')]
        client = FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id='one', name='search_evidence', arguments={'query':'one'})])])
        registry = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], lambda c,a: package([])),))
        result = AgentLoopRunner(client, registry, prompt_builder=Builder()).run('synthetic', CONTEXT)
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.errors[-1].code, 'prompt_builder_error')
        self.assertEqual(len(client.requests), 1)
