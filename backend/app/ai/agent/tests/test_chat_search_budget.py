"""Chat retrieval reservations, unique exposure and original citations; offline only."""
from copy import deepcopy
import json
from threading import Barrier, Event
import unittest
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI

from ..clients.openai_client import OpenAIClient
from ..evidence import EvidenceStore
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..schemas import ModelTurn, ToolCall
from ..testing.chat_workflow_fakes import chat_final
from ..testing.fake_llm import FakeLLMClient
from ..tools.contracts import EvidenceSearchInput
from ..tools.search_evidence import SEARCH_EVIDENCE_DATA_POLICY
from ..workflows.chat_context_prepare import prepare_chat_input
from ..workflows.chat_execution import ChatExecutionV1
from ..workflows.chat_prompts import CHAT_SEARCH_EVIDENCE_SUCCESS_POLICY
from .test_feed_candidates import package
from .test_responses import function, response, response_fixture
from .test_workflows import CONTEXT


def search(identifier, top_k=None):
    args = {'query': identifier}
    if top_k is not None:
        args['top_k'] = top_k
    return ToolCall(id=identifier, name='search_evidence', arguments=args)


def projection(messages):
    return next(json.loads(m.content) for m in messages if m.kind == 'evidence')


def budget(messages):
    return next(json.loads(m.content)['papers']['remaining_candidate_budget'] for m in messages
                if m.kind == 'agent_context' and 'chat_search_budget' in m.content)


class ReviewLLM(FakeLLMClient):
    """Scripted model copies the version it actually received, without live I/O."""
    def generate(self, request):
        for call in self.turns[0].tool_calls:
            if isinstance(call.arguments, dict) and call.arguments.get('candidate_version') == 'latest':
                call.arguments['candidate_version'] = projection(request.inputs)['candidate_version']
        return super().generate(request)


class ChatSearchBudgetTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'psycopg.connect', 'app.ai.retrieval.embedding.get_model'):
            guard = patch(target, side_effect=AssertionError('Live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def execute(self, turns, handler, *, specs=None, client=None):
        client = client or FakeLLMClient(turns)
        specs = specs or (ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], handler, parallel_safe=True),)
        execution = ChatExecutionV1(client, lambda context: ToolRegistry(specs))
        result = execution(CONTEXT, prepare_chat_input(CONTEXT, 'Synthetic research question'))
        return result, client

    @staticmethod
    def fresh(context, args):
        return package([args.query + str(i) for i in range(args.top_k)])

    def test_chat_definition_and_runtime_default_do_not_modify_common_contract(self):
        handler = Mock(side_effect=self.fresh)
        original = deepcopy(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE].definition())
        result, client = self.execute([ModelTurn(tool_calls=[search('first')]), chat_final(selected=[True]*3)], handler)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(handler.call_args.args[1].top_k, 3)
        self.assertEqual(budget(client.requests[0][0]), 3)
        self.assertEqual(budget(client.requests[1][0]), 0)
        schema = client.requests[0][1][0]['parameters']['properties']['top_k']
        self.assertEqual((schema['default'], schema['minimum'], schema['maximum']), (3, 1, 7))
        self.assertEqual(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE].definition(), original)
        self.assertEqual(EvidenceSearchInput(query='q').top_k, 5)
        self.assertEqual(EvidenceSearchInput(query='q', top_k=20).top_k, 20)

    def test_maximum_is_clamped_before_executor_instead_of_rejected(self):
        for requested in (10, 11, 20, 100):
            with self.subTest(requested=requested):
                handler = Mock(side_effect=self.fresh)
                result, client = self.execute([ModelTurn(tool_calls=[search('first', requested)]),
                    chat_final(selected=[True]*7)], handler)
                self.assertEqual(result.execution.status, 'completed')
                self.assertEqual(result.execution.errors, [])
                self.assertEqual(handler.call_args.args[1].top_k, 7)
                self.assertEqual(projection(client.requests[1][0])['candidate_count'], 7)

    def test_invalid_types_and_arguments_do_not_reach_handler_or_deliver_policy(self):
        arguments = [{'query':'q', 'top_k':v} for v in ('3', 3.0, True, False, 0, -1)]
        arguments += ['[1]', '{bad', {'query':' '}, {'query':'q', 'extra':True}]
        for args in arguments:
            with self.subTest(args=args):
                handler = Mock(side_effect=AssertionError('Invalid call reached handler'))
                call = ToolCall(id='bad', name='search_evidence', arguments=args)
                result, client = self.execute([ModelTurn(tool_calls=[call]), chat_final()], handler)
                self.assertEqual(result.execution.status, 'completed')
                self.assertEqual([e.code for e in result.execution.errors], ['invalid_arguments'])
                handler.assert_not_called()
                self.assertFalse(any(m.content == SEARCH_EVIDENCE_DATA_POLICY for m in client.requests[1][0]))

    def test_accumulation_remaining_clamp_and_exhausted_calls_keep_chat_running(self):
        handler = Mock(side_effect=self.fresh)
        turns = [ModelTurn(tool_calls=[search('a',3)]), ModelTurn(tool_calls=[search('b',5)]),
                 ModelTurn(tool_calls=[search('c',20)]), ModelTurn(tool_calls=[search('skipped')]),
                 chat_final(selected=[True]*7)]
        result, client = self.execute(turns, handler)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(result.execution.errors, [])
        self.assertEqual([c.args[1].top_k for c in handler.call_args_list], [3,2,2])
        self.assertEqual(result.execution.total_tool_calls, 3)
        self.assertEqual(len(result.execution.called_tools), 3)
        self.assertEqual([budget(r[0]) for r in client.requests], [3,0,0,0,0])
        self.assertEqual([projection(r[0])['candidate_count'] for r in client.requests[1:]], [3,5,7,7])
        outputs = [m for m in client.requests[-1][0] if m.type == 'function_call_output']
        self.assertEqual([m.call_id for m in outputs], ['a','b','c','skipped'])
        skipped = json.loads(outputs[-1].content)
        self.assertFalse(skipped['success'])
        self.assertEqual(skipped['error']['code'], 'chat_search_budget_exhausted')
        self.assertEqual(len(result.sources), 7)

    def test_parallel_reservations_and_commit_order_ignore_completion_order(self):
        barrier, second_done = Barrier(2), Event()
        def handler(context, args):
            barrier.wait(2)
            if args.query == 'a':
                self.assertTrue(second_done.wait(2))
            else:
                second_done.set()
            return self.fresh(context, args)
        result, client = self.execute([ModelTurn(tool_calls=[search('a',4), search('b',7), search('skip',7)]),
            chat_final(selected=[True]*7)], handler)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(result.execution.errors, [])
        self.assertEqual([s.pmid for s in result.sources], ['a'+str(i) for i in range(4)]+['b'+str(i) for i in range(3)])
        self.assertEqual(result.execution.total_tool_calls, 2)
        self.assertEqual([m.call_id for m in client.requests[1][0] if m.type == 'function_call_output'], ['a','b','skip'])

    def test_exhaustion_after_ten_executions_does_not_trigger_generic_call_limit(self):
        handler = Mock(side_effect=self.fresh)
        profile = TOOL_CONTRACTS[ToolName.PATIENT_PROFILE]
        profile_handler = Mock(return_value={'name':None,'dementia_stage':None,'diagnosis_date':None,'symptoms':[],'interests':[]})
        specs = (ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE],handler), ToolSpec(profile,profile_handler))
        calls = [search(str(i),i+1) for i in range(10)]
        calls += [ToolCall(id='profile'+str(i),name=profile.name.value,arguments={}) for i in range(3)]
        result, client = self.execute([ModelTurn(tool_calls=calls),
            ModelTurn(tool_calls=[search('skip')]), chat_final(selected=[False]*7)], handler,specs=specs)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(result.execution.errors, [])
        self.assertEqual(result.execution.total_tool_calls, 10)
        self.assertEqual(handler.call_count, 7)
        self.assertEqual(profile_handler.call_count, 3)
        last = [m for m in client.requests[-1][0] if m.type == 'function_call_output'][-1]
        self.assertEqual(last.call_id, 'skip')
        self.assertEqual(json.loads(last.content)['error']['code'], 'chat_search_budget_exhausted')

    def test_failed_reservation_and_skipped_call_are_not_successes_and_refund_next_turn(self):
        def handler(context, args):
            if args.query == 'bad':
                raise RuntimeError('private')
            return package(['A'])
        result, client = self.execute([ModelTurn(tool_calls=[search('bad',10), search('skip')]),
            ModelTurn(tool_calls=[search('good',1)]), chat_final(selected=[True])], handler)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual([e.code for e in result.execution.errors], ['tool_exception'])
        self.assertEqual(budget(client.requests[1][0]), 7)
        self.assertFalse(any(m.content in (SEARCH_EVIDENCE_DATA_POLICY, CHAT_SEARCH_EVIDENCE_SUCCESS_POLICY)
                             for m in client.requests[1][0]))
        self.assertEqual([s.source_ref.tool_call_id for s in result.sources], ['good'])

    def test_unused_reservation_is_available_next_turn_without_automatic_search(self):
        returned = {'a':package(['A','B','C']), 'b':package(['B','C','D','E']), 'c':package(['F','G'])}
        handler = Mock(side_effect=lambda c,a: returned[a.query])
        result, client = self.execute([ModelTurn(tool_calls=[search('a',3)]), ModelTurn(tool_calls=[search('b',7)]),
            ModelTurn(tool_calls=[search('c',10)]), chat_final(selected=[True]*7)], handler)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual([budget(r[0]) for r in client.requests], [3,0,2,0])
        self.assertEqual([c.args[1].top_k for c in handler.call_args_list], [3,4,2])
        self.assertEqual(len(handler.call_args_list), 3)

    def test_originals_survive_dedup_and_citations_keep_first_original_positions(self):
        store = EvidenceStore()
        first = package(['A', None, None]); first.evidence[1].doi = '10.123/ABC'
        second = package(['A', 'B', None]); second.evidence[0].title = 'Duplicate changed'
        second.evidence[1].doi = 'https://doi.org/10.123/abc'
        # Missing identifiers with identical titles must remain separate candidates.
        second.evidence[2].title = first.evidence[2].title
        before = [first.model_dump_json(), second.model_dump_json()]
        with patch('app.ai.agent.agent_loop_runner.EvidenceStore', return_value=store):
            result, client = self.execute([ModelTurn(tool_calls=[search('a',3), search('b',6)]),
                chat_final(selected=[True]*4)], lambda c,a: first if a.query=='a' else second)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual([p.model_dump_json() for p in store.packages()], before)
        self.assertEqual([(s.source_ref.tool_call_id,s.source_ref.evidence_index) for s in result.sources],
                         [('a',0),('a',1),('a',2),('b',2)])
        self.assertEqual(projection(client.requests[1][0])['candidate_count'], 4)
        for message in client.requests[1][0]:
            if message.type == 'function_call_output':
                self.assertNotIn('pmid', message.content)
                self.assertNotIn('Duplicate changed', message.content)

    def test_distinct_pmids_take_precedence_over_same_doi(self):
        data = package(['A','B']); data.evidence[0].doi = data.evidence[1].doi = '10.123/shared'
        result, _ = self.execute([ModelTurn(tool_calls=[search('a',2)]), chat_final(selected=[True,True])], lambda c,a:data)
        self.assertEqual([s.pmid for s in result.sources], ['A','B'])

    def test_empty_search_keeps_previous_snapshot_candidates_and_unused_budget(self):
        result, client = self.execute([ModelTurn(tool_calls=[search('a',1)]), ModelTurn(tool_calls=[search('empty',5)]),
            chat_final(selected=[True])], lambda c,a: package(['A'] if a.query=='a' else []))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(projection(client.requests[2][0])['candidate_count'], 1)
        self.assertEqual(budget(client.requests[2][0]), 4)
        self.assertEqual(result.sources[0].source_ref.tool_call_id, 'a')

    def test_failed_and_overreturned_searches_do_not_register_or_deliver_success_policy(self):
        for overreturn in (False, True):
            store = EvidenceStore()
            def handler(context, args):
                if overreturn:
                    return package(['A','B'])
                raise RuntimeError('private')
            with self.subTest(overreturn=overreturn), patch('app.ai.agent.agent_loop_runner.EvidenceStore', return_value=store):
                result, client = self.execute([ModelTurn(tool_calls=[search('a',1)]), chat_final()], handler)
                self.assertEqual(result.execution.status, 'completed')
                self.assertEqual(result.execution.errors[0].code, 'invalid_output' if overreturn else 'tool_exception')
                self.assertEqual(store.call_ids, set())
                self.assertEqual(budget(client.requests[1][0]), 1)
                self.assertFalse(any(m.content in (SEARCH_EVIDENCE_DATA_POLICY, CHAT_SEARCH_EVIDENCE_SUCCESS_POLICY)
                                     for m in client.requests[1][0]))

    def test_partial_overreturn_keeps_successful_other_search(self):
        result, client = self.execute([ModelTurn(tool_calls=[search('good',1), search('bad',2)]), chat_final(selected=[True])],
            lambda c,a:package(['A'] if a.query=='good' else ['B','C']))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual([s.pmid for s in result.sources], ['A'])
        self.assertEqual([t.success for t in result.execution.called_tools], [True,False])

    def test_selected_must_match_active_unique_snapshot_not_raw_occurrences(self):
        for mask, status in (([True], 'completed'), ([True,True], 'invalid_response'), ([], 'invalid_response')):
            with self.subTest(mask=mask):
                result, _ = self.execute([ModelTurn(tool_calls=[search('a',1),search('b',2)]), chat_final(selected=mask)],
                    lambda c,a: package(['same']))
                self.assertEqual(result.execution.status, status)

    def test_other_tool_is_executable_when_search_budget_is_exhausted(self):
        profile = TOOL_CONTRACTS[ToolName.PATIENT_PROFILE]
        profile_handler = Mock(return_value={'name':None,'dementia_stage':None,'diagnosis_date':None,'symptoms':[],'interests':[]})
        specs = (ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], self.fresh, parallel_safe=True),
                 ToolSpec(profile, profile_handler, parallel_safe=True))
        result, client = self.execute([ModelTurn(tool_calls=[search('a',10)]),
            ModelTurn(tool_calls=[search('skip'), ToolCall(id='profile',name=profile.name.value,arguments={})]),
            chat_final(selected=[False]*7)], self.fresh, specs=specs)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(result.execution.errors, [])
        profile_handler.assert_called_once()
        self.assertEqual(result.execution.total_tool_calls, 2)
        self.assertEqual(sum(m.content==profile.data_policy for m in client.requests[-1][0]), 1)

    def test_reused_execution_resets_candidates_and_budget(self):
        client = FakeLLMClient([ModelTurn(tool_calls=[search('a',10)]), chat_final(selected=[False]*7),
                               ModelTurn(tool_calls=[search('b')]), chat_final(selected=[True]*3)])
        registry = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE],self.fresh),))
        execution = ChatExecutionV1(client, lambda c:registry)
        for _ in range(2):
            result = execution(CONTEXT, prepare_chat_input(CONTEXT, 'Synthetic'))
            self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(budget(client.requests[2][0]), 3)
        self.assertEqual([s.pmid for s in result.sources], ['b0','b1','b2'])

    def test_skipped_only_turn_does_not_consume_other_tools_round_budget(self):
        profile = TOOL_CONTRACTS[ToolName.PATIENT_PROFILE]
        handler = Mock(return_value={'name':None,'dementia_stage':None,'diagnosis_date':None,'symptoms':[],'interests':[]})
        specs = (ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], self.fresh), ToolSpec(profile, handler))
        turns = [ModelTurn(tool_calls=[search(str(i), k)]) for i, k in enumerate((2,4,6,7))]
        turns += [ModelTurn(tool_calls=[search('skip')]),
                  ModelTurn(tool_calls=[ToolCall(id='profile',name=profile.name.value,arguments={})]),
                  chat_final(selected=[False]*7)]
        result, _ = self.execute(turns, self.fresh, specs=specs)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(result.execution.errors, [])
        self.assertEqual(result.execution.tool_rounds, 5)
        handler.assert_called_once()

    def test_excluded_candidates_free_slots_with_same_target_and_keep_user_history(self):
        review = search('replace',3)
        review.arguments.update(exclude_candidates=[1,2], candidate_version='latest')
        client = ReviewLLM([ModelTurn(tool_calls=[search('first',3)]), ModelTurn(tool_calls=[review]),
                            chat_final(selected=[True]*3)])
        handler = Mock(side_effect=self.fresh)
        result, _ = self.execute([], handler, client=client)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(result.execution.errors, [])
        self.assertEqual([c.args[1].top_k for c in handler.call_args_list], [3,2])
        self.assertEqual([s.pmid for s in result.sources], ['first0','replace0','replace1'])
        self.assertEqual(projection(client.requests[-1][0])['candidate_count'], 3)
        self.assertEqual(len({next(m.content for m in messages if m.kind=='user_input')
                              for messages,_ in client.requests}), 1)
        self.assertEqual([m.call_id for m in client.requests[-1][0] if m.type=='function_call_output'], ['first','replace'])
        for call in handler.call_args_list:
            self.assertEqual(set(call.args[1].model_dump()), {'query','top_k'})

    def test_all_seven_can_be_replaced_while_originals_and_stale_snapshot_rejection_remain(self):
        store = EvidenceStore()
        review = search('replace',7)
        review.arguments.update(exclude_candidates=list(range(7)), candidate_version='latest')
        old = []
        def handler(context, args):
            if args.query == 'replace':
                old.append(store.current('chat'))
            return self.fresh(context,args)
        client = ReviewLLM([ModelTurn(tool_calls=[search('first',7)]), ModelTurn(tool_calls=[review]),
                           chat_final(selected=[True]*7)])
        with patch('app.ai.agent.agent_loop_runner.EvidenceStore',return_value=store):
            result, _ = self.execute([],handler,client=client)
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(sum(len(p.evidence) for p in store.packages()),14)
        self.assertEqual(len(store.current('chat').keys),7)
        self.assertTrue(all(s.source_ref.tool_call_id=='replace' for s in result.sources))
        with self.assertRaises(ValueError):
            store.finalize_chat(chat_final(selected=[]).text,old[0])

    def test_excluded_paper_cannot_return_and_unused_slot_is_available_next_round(self):
        review = search('replace',3)
        review.arguments.update(exclude_candidates=[1,2], candidate_version='latest')
        data = {'first':package(['A','B','C']), 'replace':package(['B','D']), 'last':package(['E'])}
        client = ReviewLLM([ModelTurn(tool_calls=[search('first',3)]), ModelTurn(tool_calls=[review]),
                           ModelTurn(tool_calls=[search('last')]), chat_final(selected=[True]*3)])
        handler = Mock(side_effect=lambda c,a:data[a.query])
        result, _ = self.execute([],handler,client=client)
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual([s.pmid for s in result.sources],['A','D','E'])
        self.assertEqual([c.args[1].top_k for c in handler.call_args_list],[3,2,1])
        self.assertEqual(projection(client.requests[2][0])['candidate_count'],2)
        self.assertEqual(budget(client.requests[2][0]),1)

    def test_followup_expansion_is_total_target_and_server_caps_it_at_seven(self):
        for target, new_count in ((5,2),(7,4),(8,4),(20,4)):
            with self.subTest(target=target):
                handler = Mock(side_effect=self.fresh)
                result,client = self.execute([ModelTurn(tool_calls=[search('first',3)]),
                    ModelTurn(tool_calls=[search('expand',target)]),chat_final(selected=[True]*(3+new_count))],handler)
                self.assertEqual(result.execution.status,'completed')
                self.assertEqual([c.args[1].top_k for c in handler.call_args_list],[3,new_count])
                self.assertEqual(client.requests[-1][1][0]['parameters']['properties']['top_k']['default'],3+new_count)

    def test_bad_reviews_do_not_change_candidates_or_execute_handler(self):
        cases = [{'exclude_candidates':[1]}, {'exclude_candidates':[1], 'candidate_version':1},
                 {'exclude_candidates':[3], 'candidate_version':'latest'},
                 {'exclude_candidates':[-1], 'candidate_version':'latest'},
                 {'exclude_candidates':[True], 'candidate_version':'latest'},
                 {'exclude_candidates':[1,1], 'candidate_version':'latest'},
                 {'exclude_candidates':'1', 'candidate_version':'latest'},
                 {'exclude_candidates':[], 'candidate_version':None}]
        for fields in cases:
            with self.subTest(fields=fields):
                review=search('invalid',3);review.arguments.update(fields)
                handler=Mock(side_effect=self.fresh)
                client=ReviewLLM([ModelTurn(tool_calls=[search('first',3)]),ModelTurn(tool_calls=[review]),
                                  chat_final(selected=[True]*3)])
                result,_=self.execute([],handler,client=client)
                self.assertEqual(result.execution.status,'completed')
                self.assertEqual([e.code for e in result.execution.errors],['invalid_arguments'])
                handler.assert_called_once()
                self.assertEqual([s.pmid for s in result.sources],['first0','first1','first2'])

    def test_smaller_target_requires_explicit_exclusion_and_no_search_is_needed_if_full(self):
        for explicit in (False,True):
            review=search('shrink',2)
            if explicit:review.arguments.update(exclude_candidates=[1],candidate_version='latest')
            client=ReviewLLM([ModelTurn(tool_calls=[search('first',3)]),ModelTurn(tool_calls=[review]),
                              chat_final(selected=[True]*(2 if explicit else 3))])
            handler=Mock(side_effect=self.fresh)
            result,_=self.execute([],handler,client=client)
            self.assertEqual(result.execution.status,'completed')
            self.assertEqual([e.code for e in result.execution.errors],[] if explicit else ['invalid_arguments'])
            handler.assert_called_once()
            self.assertEqual([s.pmid for s in result.sources],['first0','first2'] if explicit else ['first0','first1','first2'])

    def test_second_search_cannot_repeat_or_override_first_search_review(self):
        first=search('a',3);first.arguments.update(exclude_candidates=[1,2],candidate_version='latest')
        second=search('bad',3);second.arguments.update(exclude_candidates=[0],candidate_version='latest')
        client=ReviewLLM([ModelTurn(tool_calls=[search('first',3)]),ModelTurn(tool_calls=[first,second]),
                          chat_final(selected=[True]*3)])
        handler=Mock(side_effect=self.fresh)
        result,_=self.execute([],handler,client=client)
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual([e.code for e in result.execution.errors],['invalid_arguments'])
        self.assertEqual([c.args[1].top_k for c in handler.call_args_list],[3,2])
        self.assertEqual([s.pmid for s in result.sources],['first0','a0','a1'])

    def test_full_retained_candidates_close_search_without_consuming_execution_budget(self):
        for initial, requested in ((1,1),(7,8)):
            with self.subTest(initial=initial,requested=requested):
                handler=Mock(side_effect=self.fresh)
                result,client=self.execute([ModelTurn(tool_calls=[search('first',initial)]),
                    ModelTurn(tool_calls=[search('skip',requested)]),chat_final(selected=[True]*initial)],handler)
                self.assertEqual(result.execution.status,'completed')
                self.assertEqual(result.execution.errors,[])
                self.assertEqual((result.execution.total_tool_calls,result.execution.tool_rounds),(1,1))
                self.assertEqual(len(result.execution.called_tools),1)
                handler.assert_called_once()
                self.assertIn('search_evidence',[d['name'] for d in client.requests[1][1]])
                self.assertNotIn('search_evidence',[d['name'] for d in client.requests[2][1]])
                output=next(m for m in client.requests[-1][0] if m.call_id=='skip')
                self.assertEqual(json.loads(output.content)['error']['code'],'chat_search_budget_exhausted')
                self.assertIn('검색을 종료',json.loads(output.content)['error']['message'])

    def test_repeated_closed_search_is_rejected_even_if_model_expands_or_reviews(self):
        for change in ('none','expand','review'):
            bad=search('bad',5 if change=='expand' else 1)
            if change=='review':bad.arguments.update(exclude_candidates=[0],candidate_version='latest')
            client=ReviewLLM([ModelTurn(tool_calls=[search('first',1)]),ModelTurn(tool_calls=[search('skip',1)]),
                              ModelTurn(tool_calls=[bad])]+[ModelTurn(tool_calls=[search(str(i),1)]) for i in range(20)])
            handler=Mock(side_effect=self.fresh)
            result,_=self.execute([],handler,client=client)
            self.assertEqual(result.execution.status,'invalid_response')
            self.assertEqual(result.execution.errors[-1].code,'chat_search_disabled')
            self.assertEqual(len(client.requests),3)
            self.assertEqual(result.execution.total_tool_calls,1)
            handler.assert_called_once()

    def test_same_turn_reservation_skip_does_not_close_followup_search(self):
        result,client=self.execute([ModelTurn(tool_calls=[search('first',3),search('reserved',3)]),
            ModelTurn(tool_calls=[search('expand',5)]),chat_final(selected=[True]*5)],self.fresh)
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(result.execution.errors,[])
        self.assertEqual(result.execution.total_tool_calls,2)
        self.assertIn('search_evidence',[d['name'] for d in client.requests[1][1]])
        self.assertEqual([s.pmid for s in result.sources],['first0','first1','first2','expand0','expand1'])

    def test_closed_search_preserves_other_tools_and_is_reset_for_next_request(self):
        profile=TOOL_CONTRACTS[ToolName.PATIENT_PROFILE]
        profile_handler=Mock(return_value={'name':None,'dementia_stage':None,'diagnosis_date':None,'symptoms':[],'interests':[]})
        search_handler=Mock(side_effect=self.fresh)
        registry=ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE],search_handler),ToolSpec(profile,profile_handler)))
        client=FakeLLMClient([ModelTurn(tool_calls=[search('first',1)]),ModelTurn(tool_calls=[search('skip',1)]),
            ModelTurn(tool_calls=[ToolCall(id='profile',name=profile.name.value,arguments={})]),chat_final(selected=[True]),
            ModelTurn(tool_calls=[search('second',1)]),chat_final(selected=[True])])
        execution=ChatExecutionV1(client,lambda c:registry)
        for _ in range(2):
            result=execution(CONTEXT,prepare_chat_input(CONTEXT,'Synthetic'))
            self.assertEqual(result.execution.status,'completed')
            self.assertEqual(result.execution.errors,[])
        self.assertEqual([d['name'] for d in client.requests[2][1]],[profile.name.value])
        self.assertIn('search_evidence',[d['name'] for d in client.requests[4][1]])
        self.assertEqual(set(registry.names()),{'search_evidence',profile.name.value})
        profile_handler.assert_called_once()
        self.assertEqual(search_handler.call_count,2)
        self.assertEqual(result.sources[0].pmid,'second0')

    def test_invalid_request_or_empty_result_does_not_close_search(self):
        for mode in ('invalid','empty','failure'):
            with self.subTest(mode=mode):
                first=search('first',3)
                if mode=='invalid':first.arguments['top_k']=True
                def handler(context,args):
                    if args.query=='first':
                        if mode=='failure':raise RuntimeError('synthetic')
                        return package([])
                    return self.fresh(context,args)
                result,client=self.execute([ModelTurn(tool_calls=[first]),ModelTurn(tool_calls=[search('next',3)]),
                    chat_final(selected=[True]*3)],handler)
                self.assertEqual(result.execution.status,'completed')
                self.assertIn('search_evidence',[d['name'] for d in client.requests[1][1]])
                self.assertEqual([s.pmid for s in result.sources],['next0','next1','next2'])

    def test_explicit_exclusion_without_new_slot_does_not_close_search(self):
        shrink=search('shrink',2);shrink.arguments.update(exclude_candidates=[1],candidate_version='latest')
        client=ReviewLLM([ModelTurn(tool_calls=[search('first',3)]),ModelTurn(tool_calls=[shrink]),
                          ModelTurn(tool_calls=[search('expand',3)]),chat_final(selected=[True]*3)])
        result,_=self.execute([],self.fresh,client=client)
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(result.execution.errors,[])
        self.assertIn('search_evidence',[d['name'] for d in client.requests[2][1]])
        self.assertEqual([s.pmid for s in result.sources],['first0','first2','expand0'])

    def test_closing_search_skips_later_search_in_same_turn_but_keeps_call_ids(self):
        handler=Mock(side_effect=self.fresh)
        result,client=self.execute([ModelTurn(tool_calls=[search('first',1)]),
            ModelTurn(tool_calls=[search('skip',1),search('expand',5)]),chat_final(selected=[True])],handler)
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(result.execution.errors,[])
        handler.assert_called_once()
        self.assertEqual(result.execution.total_tool_calls,1)
        self.assertEqual([m.call_id for m in client.requests[-1][0] if m.type=='function_call_output'],['first','skip','expand'])
        self.assertNotIn('search_evidence',[d['name'] for d in client.requests[-1][1]])

    def test_sdk_replay_keeps_requested_arguments_and_skipped_result_call_id(self):
        wire = []
        turns = [response([{**function('a','{"query":"a","top_k":20}'),'name':'search_evidence'},
                           {**function('b','{"query":"b"}'),'name':'search_evidence'}]),
                 response_fixture({'text':chat_final(selected=[True]*7).text})]
        def transport(request):
            wire.append(json.loads(request.content))
            return httpx.Response(200,json=turns[len(wire)-1])
        with OpenAI(api_key='offline-placeholder',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
            result, _ = self.execute([],self.fresh,client=OpenAIClient(sdk,model='offline'))
        self.assertEqual(result.execution.status,'completed')
        calls = [m for m in wire[1]['input'] if m.get('type')=='function_call']
        self.assertEqual([m['arguments'] for m in calls], ['{"query":"a","top_k":20}','{"query":"b"}'])
        self.assertEqual([m['call_id'] for m in wire[1]['input'] if m.get('type')=='function_call_output'],['a','b'])
