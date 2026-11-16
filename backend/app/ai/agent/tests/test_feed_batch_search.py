"""Feed array fan-out and real SDK wire protocol; no live LLM/RAG/DB."""
import json
import unittest
from threading import Barrier, Event, Lock, Thread
from unittest.mock import patch

import httpx
from openai import OpenAI

from ..agent_loop_runner import AgentLoopRunner
from ..clients.openai_client import OpenAIClient
from ..feed_candidates import FeedCandidates
from ..feed_search_policy import FeedSearchInput, feed_tool_definitions
from ..schemas import ModelTurn, ToolCall
from ..testing.fake_llm import FakeLLMClient
from ..workflows.feed_execution import FeedExecutionV1
from ..workflows.feed_prompts import FeedPromptBuilder
from . import test_parallel_feed as parallel
from .test_feed_candidates import decisions, package, review_turn
from .test_openai_client import response_fixture
from .test_workflows import CONTEXT


def batch(queries, identifier='provider-real'):
    return ToolCall(id=identifier, name='search_evidence', arguments={'queries': queries})


class FeedBatchSearchTests(unittest.TestCase):
    setUp = parallel.ParallelFeedTests.setUp

    def run_turns(self, turns, handler, **options):
        client = FakeLLMClient(turns)
        result = FeedExecutionV1(client, parallel.registry(handler), **options)(CONTEXT, '{"patients":[]}')
        return result, client

    def test_one_two_three_queries_are_independent_parallel_searches_with_one_provider_call(self):
        for count in (1, 2, 3):
            with self.subTest(count=count):
                barrier, reached, lock = Barrier(count), [], Lock()
                def handler(context, args):
                    with lock: reached.append((args.query, args.top_k))
                    barrier.wait(2)
                    return package([f'{args.query}-{i}' for i in range(8)])
                result, client = self.run_turns([ModelTurn(tool_calls=[batch([f'topic {i}' for i in range(count)])]),
                    ModelTurn(text=json.dumps(decisions(min(count * 8, 10), range(5))))], handler)
                self.assertEqual(result.execution.status, 'completed')
                self.assertCountEqual(reached, [(f'topic {i}', 8) for i in range(count)])
                self.assertEqual(result.execution.total_tool_calls, count)
                self.assertEqual(sum(t.result_count for t in result.execution.called_tools), count * 8)
                self.assertEqual(len(client.requests), 2)
                self.assertEqual(len(result.output.items), 5)
                messages = client.requests[1][0]
                tools = [m for m in messages if m.type == "function_call_output"]
                self.assertEqual([m.call_id for m in tools], ['provider-real'])
                self.assertEqual(json.loads(tools[0].content)['data']['searches'],
                    [{'query_index': i, 'returned_evidence_count': 8} for i in range(count)])
                progress = json.loads(messages[-1].content)['search_progress']
                self.assertEqual(progress['model_tool_calls_used'], 1)
                self.assertEqual(progress['search_calls_used'], count)
                self.assertEqual(progress['search_calls_remaining'], 9-count)
                self.assertNotIn('feed-rag:', json.dumps([m.model_dump() for m in messages]))
                for source in result.sources:
                    index = int(source.source_ref.tool_call_id.split(':')[-1])
                    self.assertEqual(source.pmid, f'topic {index}-{source.source_ref.evidence_index}')

    def test_out_of_order_completion_preserves_topic_order_and_original_indices(self):
        barrier, release_a, release_b = Barrier(3), Event(), Event()
        finished = []
        def handler(context, args):
            i = int(args.query)
            barrier.wait(2)
            if i == 0: self.assertTrue(release_a.wait(2))
            if i == 1: self.assertTrue(release_b.wait(2))
            finished.append(i)
            if i == 2: release_a.set()
            if i == 0: release_b.set()
            return package([f'{i}-{j}' for j in range(8)])
        try:
            result, _ = self.run_turns([ModelTurn(tool_calls=[batch(['0','1','2'])]),
                ModelTurn(text=json.dumps(decisions(10, range(5))))], handler)
        finally:
            release_a.set(); release_b.set()
        self.assertEqual(finished, [2,0,1])
        self.assertEqual([s.pmid for s in result.sources], ['0-0','1-0','2-0','0-1','1-1'])
        self.assertEqual([s.source_ref.evidence_index for s in result.sources], [0,0,0,1,1])

    def test_batch_failure_returns_before_slow_peer_without_partial_or_late_publication(self):
        barrier, release, finished = Barrier(2), Event(), Event()
        def handler(context, args):
            barrier.wait(2)
            if args.query == 'fail': raise RuntimeError('PRIVATE')
            try: release.wait(4)
            finally: finished.set()
            return package(['late'])
        box = {}
        def run():
            box['result'], box['client'] = self.run_turns([ModelTurn(tool_calls=[batch(['slow','fail'])])],
                handler, tool_timeout_seconds=3, request_timeout_seconds=4)
        with patch.object(FeedCandidates, 'add') as publish:
            worker = Thread(target=run, daemon=True); worker.start()
            try:
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.assertFalse(finished.is_set())
                result = box['result']
                self.assertEqual(result.execution.status, 'failed')
                self.assertEqual(result.execution.total_tool_calls, 2)
                self.assertEqual(len(box['client'].requests), 1)
                self.assertEqual(result.sources, [])
                self.assertIsNone(result.output)
                snapshot = result.model_dump_json()
                self.assertNotIn('PRIVATE', snapshot)
                publish.assert_not_called()
            finally: release.set()
            self.assertTrue(finished.wait(1))
            self.assertEqual(box['result'].model_dump_json(), snapshot)
            publish.assert_not_called()

    def test_remaining_budget_is_actual_search_count_and_followup_has_one_real_ack(self):
        reached = []
        def handler(context, args):
            reached.append(args.query)
            return package([args.query])
        result, client = self.run_turns([ModelTurn(tool_calls=[batch(['a','b'], 'first')]),
            review_turn(ModelTurn(tool_calls=[batch(['c'], 'second')]), 2, (0, 1)),
            ModelTurn(text=json.dumps(decisions(3, range(3))))], handler)
        self.assertEqual(result.execution.status, 'completed')
        self.assertCountEqual(reached, ['a','b','c'])
        self.assertEqual((result.execution.tool_rounds, result.execution.total_tool_calls), (2,3))
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(client.options[-1].tool_choice, 'auto')
        last = client.requests[-1][0]
        self.assertEqual([m.call_id for m in last if m.type == "function_call_output"], ['first','second'])
        self.assertEqual(json.loads(last[-1].content)['search_progress']['model_tool_calls_used'], 2)

    def test_batch_timeout_and_deadline_do_not_join_or_accept_late_results(self):
        for options, code in (({'tool_timeout_seconds': .15}, 'tool_timeout'),
                              ({'tool_timeout_seconds': 3, 'request_timeout_seconds': .2}, 'request_timeout')):
            with self.subTest(code=code):
                release, entered, finished = Event(), Barrier(3), [Event() for _ in range(3)]
                def handler(context, args):
                    i = int(args.query)
                    try:
                        entered.wait(2)
                        release.wait(3)
                        return package(['late'])
                    finally: finished[i].set()
                try:
                    result, client = self.run_turns([ModelTurn(tool_calls=[batch(['0','1','2'])])], handler, **options)
                    self.assertEqual(result.execution.status, 'failed')
                    self.assertEqual(result.execution.errors[-1].code, code)
                    self.assertIsNone(result.output)
                    self.assertEqual(result.sources, [])
                    self.assertEqual(len(client.requests), 1)
                    snapshot = result.model_dump_json()
                finally: release.set()
                self.assertTrue(all(event.wait(1) for event in finished))
                self.assertEqual(result.model_dump_json(), snapshot)

    def test_excess_actual_search_budget_rejects_entire_turn_before_io(self):
        for turns, expected in (([ModelTurn(tool_calls=[batch(['a','b']), batch(['c','d'], 'second')])], 0),
                                 ([ModelTurn(tool_calls=[batch(['a','b'])]), review_turn(ModelTurn(tool_calls=[batch(['c','d'], 'next'), batch(['e','f'], 'another')]), 1)], 2)):
            reached = []
            result, _ = self.run_turns(turns, lambda c,a: reached.append(a.query) or package([a.query]))
            self.assertEqual(result.execution.status, 'limit_reached')
            self.assertEqual(result.execution.total_tool_calls, expected)
            self.assertEqual(len(reached), expected)

    def test_multiple_real_batch_calls_share_budget_and_get_exactly_two_acks(self):
        barrier = Barrier(3)
        def handler(context, args):
            barrier.wait(2)
            return package([args.query])
        result, client = self.run_turns([ModelTurn(tool_calls=[batch(['a','b'], 'first'), batch(['c'], 'second')]),
            ModelTurn(text=json.dumps(decisions(3, range(3))))], handler)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(result.execution.total_tool_calls, 3)
        self.assertEqual(len(client.requests), 2)
        messages = client.requests[-1][0]
        self.assertEqual([m.call_id for m in messages if m.type == "function_call_output"], ['first','second'])
        self.assertEqual(json.loads(messages[-1].content)['search_progress']['model_tool_calls_used'], 2)
        self.assertEqual([s.pmid for s in result.sources], ['a','b','c'])

    def test_bad_arrays_are_rejected_before_any_search(self):
        inputs = [[], [' '], ['a','a'], ['a',' A '], ['a']*4, ['x'*2001], [3], [None],
                  'a,b', ['a\nb'], ['a;b'], ('a','b')]
        for queries in inputs:
            with self.subTest(queries=repr(queries)[:50]):
                reached = []
                result, _ = self.run_turns([ModelTurn(tool_calls=[batch(queries, f"bad-{i}")]) for i in range(3)],
                    lambda c,a: reached.append(a.query))
                self.assertEqual(result.execution.status, 'failed')
                self.assertEqual(result.execution.errors[-1].code, 'invalid_arguments')
                self.assertEqual(reached, [])
        for arguments in ('[]', '{"queries":["a"],"queries":["b"]}', {'queries':['a'], 'top_k':20}):
            bad = batch(['a']).model_copy(update={'arguments':arguments})
            result, _ = self.run_turns([ModelTurn(tool_calls=[bad.model_copy(update={"id":f"bad-{i}"})]) for i in range(3)], lambda c,a: self.fail('Unexpected I/O'))
            self.assertEqual(result.execution.status, 'failed')

    def test_duplicate_followup_is_rejected_without_rewriting_valid_query(self):
        reached = []
        result, _ = self.run_turns([ModelTurn(tool_calls=[batch(['  Topic A  '])]),
            ModelTurn(tool_calls=[batch(['topic   a'], 'again')]), ModelTurn(text=json.dumps(decisions(1)))],
            lambda c,a: reached.append(a.query) or package(['a']))
        self.assertEqual(reached, ['  Topic A  '])
        self.assertEqual(result.execution.total_tool_calls, 1)
        self.assertEqual(result.execution.errors[-1].code, 'invalid_arguments')

    def test_commas_and_undelimited_compound_are_not_silently_split_into_topics(self):
        compound = 'dementia wandering nighttime agitation fall prevention day care programs'
        policy = FeedPromptBuilder().build_system_prompt(CONTEXT)
        self.assertIn('잘못된 예', policy)
        self.assertIn(compound, policy)
        for query in ('dementia, sleep management', compound):
            reached = []
            result, _ = self.run_turns([ModelTurn(tool_calls=[batch([query])]),
                ModelTurn(text=json.dumps(decisions(1)))], lambda c,a: reached.append(a.query) or package(['a']))
            self.assertEqual(reached, [query])  # Structural success is NOT semantic separation proof.
            self.assertEqual(result.execution.total_tool_calls, 1)

    def test_pmids_grades_and_snapshot_refs_survive_batch_fanout(self):
        from .test_feed_grade_priority import evidence
        pools = {'a':evidence([('X','other',.9),('a','rct',.9)]),
                 'b':evidence([('X','meta_analysis',.1),('b','qualitative',.1)])}
        result, client = self.run_turns([ModelTurn(tool_calls=[batch(['a','b'])]),
            ModelTurn(text=json.dumps(decisions(3, range(3))))], lambda c,a: pools[a.query])
        self.assertEqual([s.pmid for s in result.sources], ['a','X','b'])
        self.assertEqual([(s.source_ref.tool_call_id,s.source_ref.evidence_index) for s in result.sources],
            [('feed-rag:1:0:0',1),('feed-rag:1:0:1',0),('feed-rag:1:0:1',1)])
        self.assertEqual([r['derived_evidence_grade'] for r in json.loads(client.requests[-1][0][-1].content)['candidates']], ['B','A','C'])

    def test_internal_source_namespace_cannot_collide_with_real_provider_id(self):
        identifier = 'feed-rag:1:0:0'
        result, client = self.run_turns([ModelTurn(tool_calls=[batch(['a'], identifier)]),
            ModelTurn(text=json.dumps(decisions(1, [0])))], lambda c,a: package(['a']))
        self.assertNotEqual(result.sources[0].source_ref.tool_call_id, identifier)
        self.assertEqual([m.call_id for m in client.requests[-1][0] if m.type == "function_call_output"], [identifier])

    def test_chat_schema_and_single_query_execution_are_unchanged(self):
        reached = []
        tools = parallel.registry(lambda c,a: reached.append((a.query,a.top_k)) or package(['a']))
        before = tools.definitions()
        adapted = feed_tool_definitions(before)
        self.assertEqual(tools.definitions(), before)
        self.assertEqual(adapted[0]['parameters'], FeedSearchInput.model_json_schema())
        self.assertEqual(set(before[0]['parameters']['properties']), {'query','top_k'})
        client = FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id='chat',name='search_evidence',arguments={'query':'a'})]), ModelTurn(text='Chat')])
        result = AgentLoopRunner(client, tools).run('Synthetic', CONTEXT)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(reached, [('a',5)])
        self.assertEqual(client.requests[0][1], before)

    def test_real_sdk_mock_transport_preserves_single_batch_tool_protocol(self):
        wire, reached = [], []
        def transport(request):
            wire.append(json.loads(request.content))
            if len(wire) == 1:
                return httpx.Response(200,json=response_fixture({'text': None, 'calls': [{'type': 'function_call', 'id': 'item_' + 'sdk-real', 'call_id': 'sdk-real', 'name': 'search_evidence', 'arguments': json.dumps({'queries': ['a', 'b', 'c']}), 'status': 'completed'}]}))
            return httpx.Response(200,json=response_fixture({'text': json.dumps(decisions(3, range(3)))}))
        sdk = OpenAI(api_key='offline-placeholder',http_client=httpx.Client(transport=httpx.MockTransport(transport)))
        self.addCleanup(sdk.close)
        result = FeedExecutionV1(OpenAIClient(sdk,model='offline-model'),parallel.registry(
            lambda c,a: reached.append((a.query,a.top_k)) or package([a.query])))(CONTEXT,'{}')
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(len(wire), 2)
        self.assertCountEqual(reached, [('a',8),('b',8),('c',8)])
        schema = wire[0]['tools'][0]['parameters']
        self.assertEqual(set(schema['properties']), {'queries', 'review'})
        self.assertEqual((schema['properties']['queries']['minItems'],schema['properties']['queries']['maxItems']), (1,3))
        self.assertEqual([m['call_id'] for m in wire[1]['input'] if m.get("type") == "function_call_output"], ['sdk-real'])
        self.assertNotIn('feed-rag:', json.dumps(wire[1]['input']))
        self.assertEqual([wire[0]['tool_choice'],wire[1]['tool_choice']], ['required','auto'])

    def test_original_input_errors_recover_without_using_search_budget(self):
        for arguments in ({}, {'top_k':8}, {'queries':[]}, '{"queries":', {'queries':['PRIVATE','private']},
                          {'queries':['a'], 'review': {'decisions':[],'PRIVATE':'PRIVATE'}}):
            with self.subTest(arguments=arguments):
                bad = batch(['a'], 'bad').model_copy(update={'arguments':arguments})
                reached = []
                with self.assertLogs('app.ai.agent.agent_loop_runner', level='INFO') as logs:
                    result, client = self.run_turns([ModelTurn(tool_calls=[bad]),
                        ModelTurn(tool_calls=[batch(['valid'], 'fixed')]),
                        ModelTurn(text=json.dumps(decisions(1)))],
                        lambda c,a: reached.append(a.query) or package(['a']))
                self.assertEqual(result.execution.status, 'completed')
                self.assertEqual((result.execution.tool_rounds, result.execution.total_tool_calls), (1,1))
                self.assertEqual(reached, ['valid'])
                outputs = [m for m in client.requests[1][0] if m.type=='function_call_output']
                self.assertEqual([m.call_id for m in outputs], ['bad'])
                self.assertEqual(json.loads(outputs[0].content)['error']['code'], 'invalid_arguments')
                self.assertNotIn('PRIVATE', outputs[0].content + '\n'.join(logs.output))
                self.assertIn('recovery_action=llm_retry', '\n'.join(logs.output))
                self.assertEqual([o.tool_choice for o in client.options], ['required','required','auto'])

    def test_legacy_plan_validates_original_once_and_preserves_helper_contract(self):
        from ..feed_search_policy import bounded_feed_searches, prepare_feed_searches
        from ..tools.contracts import EvidenceSearchInput
        for top_k in (None, 1, 3, 8):
            for encoded in (False, True):
                with self.subTest(top_k=top_k, encoded=encoded):
                    raw = {'query':' synthetic '}
                    if top_k is not None:
                        raw['top_k'] = top_k
                    call = ToolCall(id='legacy', name='search_evidence',
                                    arguments=json.dumps(raw) if encoded else raw)
                    before = call.model_dump_json()
                    with patch.object(EvidenceSearchInput, 'model_validate',
                                      wraps=EvidenceSearchInput.model_validate) as validate:
                        plan = prepare_feed_searches([call], turn=1, remaining_calls=9,
                            remaining_budget=72, previous_queries=set(), reserved_ids=set())
                    self.assertEqual(validate.call_count, 1)
                    self.assertEqual(plan.searches, bounded_feed_searches([call]))
                    self.assertEqual(plan.searches[0].arguments['top_k'], 8 if top_k is None else top_k)
                    self.assertEqual(len(plan.query_keys), len(plan.searches))
                    self.assertEqual(call.model_dump_json(), before)

    def test_recovery_limit_and_duplicate_call_ids_remain_bounded(self):
        turns = [ModelTurn(tool_calls=[batch([], f'bad-{i}')]) for i in range(4)]
        result, client = self.run_turns(turns, lambda c,a: self.fail('Unexpected I/O'))
        self.assertEqual(result.execution.status, 'failed')
        self.assertEqual((len(client.requests), result.execution.tool_rounds, result.execution.total_tool_calls), (3,0,0))
        result, client = self.run_turns([turns[0], turns[0]], lambda c,a: self.fail('Unexpected I/O'))
        self.assertEqual(result.execution.errors[-1].code, 'duplicate_call_id')
        self.assertEqual(len(client.requests), 2)

    def test_rejected_multi_call_turn_has_one_error_per_original_provider_call(self):
        reached = []
        result, client = self.run_turns([ModelTurn(tool_calls=[batch(['a'],'one'),batch([], 'two')]),
            ModelTurn(tool_calls=[batch(['b'], 'fixed')]), ModelTurn(text=json.dumps(decisions(1)))],
            lambda c,a: reached.append(a.query) or package(['b']))
        self.assertEqual(result.execution.status, 'completed')
        outputs = [m for m in client.requests[1][0] if m.type=='function_call_output']
        self.assertEqual([m.call_id for m in outputs], ['one','two'])
        self.assertTrue(all(not json.loads(m.content)['success'] for m in outputs))
        self.assertEqual(json.loads(outputs[0].content)['error']['message'],
                         '같은 Turn의 다른 호출 검증 실패로 실행하지 않음')
        self.assertIn('queries', json.loads(outputs[1].content)['error']['message'])
        self.assertEqual(reached, ['b'])
        self.assertEqual(result.execution.total_tool_calls, 1)

    def test_first_invalid_call_does_not_assign_its_error_to_unvalidated_peers(self):
        rejected = [batch([], 'invalid'), batch(['a'], 'valid'), batch([], 'unvalidated')]
        reached = []
        result, client = self.run_turns([ModelTurn(tool_calls=rejected),
            ModelTurn(tool_calls=[batch(['fixed'], 'retry')]), ModelTurn(text=json.dumps(decisions(1)))],
            lambda c,a: reached.append(a.query) or package(['b']))
        outputs = [m for m in client.requests[1][0] if m.type == 'function_call_output']
        self.assertEqual([m.call_id for m in outputs], ['invalid','valid','unvalidated'])
        messages = [json.loads(m.content)['error']['message'] for m in outputs]
        self.assertIn('queries', messages[0])
        self.assertEqual(messages[1:], ['같은 Turn의 다른 호출 검증 실패로 실행하지 않음'] * 2)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual((reached, result.execution.total_tool_calls), (['fixed'],1))

    def test_turn_wide_review_conflict_does_not_blame_last_unrelated_call(self):
        from .test_feed_review_rounds import review
        from ..executor import diagnostic_call_id
        second, third, fourth = batch(['b'],'second'), batch(['c'],'third'), batch(['d'],'fourth')
        second.arguments['review'] = review(1,(0,))
        third.arguments['review'] = review(1)
        reached = []
        with self.assertLogs('app.ai.agent.agent_loop_runner', level='INFO') as logs:
            result, client = self.run_turns([ModelTurn(tool_calls=[batch(['initial'],'first')]),
                ModelTurn(tool_calls=[second,third,fourth]), ModelTurn(text=json.dumps(decisions(1,[0])))],
                lambda c,a: reached.append(a.query) or package(['original']))
        conflict = next(line for line in logs.output if 'conflicting_reviews' in line)
        self.assertIn('tool_call_id=' + repr([diagnostic_call_id(c.id) for c in (second,third,fourth)]), conflict)
        self.assertNotIn('tool_call_id=' + repr(diagnostic_call_id(fourth.id)), conflict)
        outputs = [m for m in client.requests[-1][0] if m.type == 'function_call_output' and m.call_id != 'first']
        self.assertEqual([m.call_id for m in outputs], ['second','third','fourth'])
        messages = [json.loads(m.content)['error']['message'] for m in outputs]
        self.assertEqual(messages, ['one consistent review for the current snapshot'] * 3)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual((reached, result.execution.total_tool_calls), (['initial'],1))

    def test_partial_success_stops_fast_discards_late_peer_and_keeps_source(self):
        from ..agent_loop_runner import _validate_tool_result
        barrier, observed, release, finished = Barrier(3), Event(), Event(), Event()
        def validate(call, raw):
            result = _validate_tool_result(call, raw)
            if result[0].success: observed.set()
            return result
        def handler(context, args):
            barrier.wait(2)
            if args.query == 'good': return package(['verified'])
            if args.query == 'fail':
                self.assertTrue(observed.wait(2))
                raise RuntimeError('PRIVATE')
            try: release.wait(3)
            finally: finished.set()
            return package(['late'])
        try:
            with patch('app.ai.agent.agent_loop_runner._validate_tool_result', side_effect=validate):
                result, client = self.run_turns([ModelTurn(tool_calls=[batch(['good','fail','slow'])]),
                    ModelTurn(text=json.dumps(decisions(1, [0])))], handler, tool_timeout_seconds=2)
            self.assertFalse(finished.is_set())
            self.assertEqual(result.execution.status, 'completed')
            self.assertEqual([s.pmid for s in result.sources], ['verified'])
            self.assertEqual(result.sources[0].source_ref.tool_call_id, 'feed-rag:1:0:0')
            self.assertEqual(result.execution.total_tool_calls, 3)
            self.assertEqual([o.tool_choice for o in client.options], ['required','none'])
            output = next(m for m in client.requests[-1][0] if m.type=='function_call_output')
            body = json.loads(output.content)
            self.assertEqual(output.call_id, 'provider-real')
            self.assertTrue(body['success'])
            self.assertEqual([r['status'] for r in body['data']['searches']], ['completed','failed','not_accepted'])
            progress = json.loads(client.requests[-1][0][-1].content)['search_progress']
            self.assertEqual((progress['search_calls_used'], progress['search_calls_reserved']), (3,3))
            self.assertEqual((progress['successful_searches'], progress['failed_searches'], progress['not_accepted']), (1,1,1))
            self.assertNotIn('PRIVATE', output.content + result.model_dump_json())
            snapshot = result.model_dump_json()
        finally: release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(result.model_dump_json(), snapshot)

    def test_cancelled_later_batch_is_not_counted_as_started_or_failed(self):
        from ..agent_loop_runner import _validate_tool_result
        observed, reached = Event(), []
        def validate(call, raw):
            result = _validate_tool_result(call, raw)
            if result[0].success: observed.set()
            return result
        def handler(context, args):
            reached.append(args.query)
            if args.query=='good': return package(['verified'])
            self.assertTrue(observed.wait(2))
            raise RuntimeError('PRIVATE')
        with patch('app.ai.agent.agent_loop_runner._validate_tool_result', side_effect=validate):
            result, client = self.run_turns([ModelTurn(tool_calls=[batch(['good','fail','never'])]),
                ModelTurn(text=json.dumps(decisions(1)))], handler, max_parallel_calls=2)
        self.assertEqual(result.execution.status, 'completed')
        self.assertCountEqual(reached, ['good','fail'])
        self.assertEqual(result.execution.total_tool_calls, 2)
        body = json.loads(next(m.content for m in client.requests[-1][0] if m.type=='function_call_output'))
        self.assertEqual([r['status'] for r in body['data']['searches']], ['completed','failed','cancelled'])
        progress = json.loads(client.requests[-1][0][-1].content)['search_progress']
        self.assertEqual((progress['search_calls_reserved'],progress['search_calls_used'],progress['cancelled']), (3,2,1))

    def test_partial_legacy_calls_acknowledge_success_and_failure_individually(self):
        from ..agent_loop_runner import _validate_tool_result
        observed = Event()
        def validate(call, raw):
            result = _validate_tool_result(call, raw)
            if result[0].success: observed.set()
            return result
        def handler(context, args):
            if args.query=='topic 0': return package(['verified'])
            self.assertTrue(observed.wait(2))
            raise RuntimeError('PRIVATE')
        with patch('app.ai.agent.agent_loop_runner._validate_tool_result', side_effect=validate):
            result, client = self.run_turns([ModelTurn(tool_calls=parallel.calls(2)),
                ModelTurn(text=json.dumps(decisions(1, [0])))], handler)
        self.assertEqual(result.execution.status, 'completed')
        outputs = [json.loads(m.content) for m in client.requests[-1][0] if m.type=='function_call_output']
        self.assertEqual([o['success'] for o in outputs], [True, False])
        self.assertEqual([m.call_id for m in client.requests[-1][0] if m.type=='function_call_output'], ['opaque_0','opaque_1'])
        self.assertEqual([s.pmid for s in result.sources], ['verified'])

    def test_permission_denial_remains_fatal_even_with_verified_peer(self):
        from ..agent_loop_runner import _validate_tool_result
        observed = Event()
        def validate(call, raw):
            result = _validate_tool_result(call, raw)
            if result[0].success: observed.set()
            return result
        def handler(context, args):
            if args.query=='good': return package(['verified'])
            self.assertTrue(observed.wait(2))
            raise PermissionError('PRIVATE')
        with patch('app.ai.agent.agent_loop_runner._validate_tool_result', side_effect=validate):
            result, client = self.run_turns([ModelTurn(tool_calls=[batch(['good','denied'])]),
                ModelTurn(text=json.dumps(decisions(1)))], handler)
        self.assertEqual(result.execution.status, 'failed')
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(result.sources, [])
        self.assertNotIn('PRIVATE', result.model_dump_json())

    def test_postgres_permission_failure_aborts_after_accepted_parallel_success(self):
        from psycopg import Error
        from psycopg.errors import InsufficientPrivilege
        from ..agent_loop_runner import _validate_tool_result
        errors = [InsufficientPrivilege('PRIVATE'), Error('PRIVATE', info={ord('C'):b'42501'})]
        for error in errors:
            with self.subTest(exception=type(error).__name__):
                observed = Event()
                def validate(call, raw):
                    result = _validate_tool_result(call, raw)
                    if result[0].success: observed.set()
                    return result
                def handler(context, args):
                    if args.query == 'good': return package(['verified'])
                    self.assertTrue(observed.wait(2))
                    raise error
                with patch('app.ai.agent.agent_loop_runner._validate_tool_result', side_effect=validate), \
                     self.assertLogs('app.ai.agent', level='INFO') as logs:
                    result, client = self.run_turns([ModelTurn(tool_calls=[batch(['good','denied'])]),
                        ModelTurn(text=json.dumps(decisions(1,[0])))], handler)
                self.assertEqual(result.execution.status, 'failed')
                self.assertEqual((len(client.requests), result.execution.total_tool_calls), (1,2))
                self.assertEqual([t.success for t in result.execution.called_tools], [True,False])
                self.assertEqual(result.execution.errors[-1].code, 'tool_exception')
                self.assertIsNone(result.output)
                self.assertEqual(result.sources, [])
                self.assertIn('recovery_action=abort', '\n'.join(logs.output))
                self.assertNotIn('PRIVATE', '\n'.join(logs.output) + result.model_dump_json())

    def test_adapter_and_executor_output_violations_abort_with_same_classification(self):
        from ..adapters.rag_evidence import RagEvidenceRetriever
        from ..composition import build_evidence_registry
        from ..agent_loop_runner import _validate_tool_result
        for adapter in (False, True):
            with self.subTest(adapter=adapter):
                observed = Event()
                def validate(call, raw):
                    result = _validate_tool_result(call, raw)
                    if result[0].success: observed.set()
                    return result
                def rag(*, query, k):
                    raw = package(['verified'])
                    if query == 'bad':
                        self.assertTrue(observed.wait(2))
                        raw.evidence[0].relevance_score = 2.0
                    return raw
                tools = (build_evidence_registry(retriever=RagEvidenceRetriever(search_fn=rag)) if adapter
                         else parallel.registry(lambda c,a: rag(query=a.query,k=a.top_k)))
                client = FakeLLMClient([ModelTurn(tool_calls=[batch(['good','bad'])]),
                                       ModelTurn(text=json.dumps(decisions(1,[0])))])
                with patch('app.ai.agent.agent_loop_runner._validate_tool_result', side_effect=validate), \
                     self.assertLogs('app.ai.agent', level='INFO') as logs:
                    result = FeedExecutionV1(client, tools)(CONTEXT, '{}')
                self.assertEqual(result.execution.status, 'failed')
                self.assertEqual((len(client.requests), result.execution.total_tool_calls), (1,2))
                self.assertEqual(result.execution.errors[-1].code, 'invalid_output')
                self.assertEqual(result.execution.called_tools[-1].error_type, 'invalid_output')
                self.assertIsNone(result.output)
                self.assertEqual(result.sources, [])
                self.assertIn('recovery_action=abort', '\n'.join(logs.output))

    def test_review_union_missing_fields_are_disclosed_and_repaired(self):
        malformed = batch(['followup'], 'bad-review')
        malformed.arguments['review'] = {'decisions':[{'include':True}]}
        corrected = batch(['followup'], 'fixed-review')
        corrected.arguments['review'] = {'decisions':[{'include':False}]}
        result, client = self.run_turns([ModelTurn(tool_calls=[batch(['initial'],'first')]),
            ModelTurn(tool_calls=[malformed]), ModelTurn(tool_calls=[corrected]),
            ModelTurn(text=json.dumps(decisions(1,[0])))], lambda c,a:package([a.query]))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(result.execution.total_tool_calls, 2)
        message = next(json.loads(m.content)['error']['message'] for m in client.requests[2][0]
                       if m.type=='function_call_output' and m.call_id=='bad-review')
        self.assertIn('review.decisions[0].personal_reason', message)
        self.assertIn('review.decisions[0].category', message)
        self.assertNotIn('<unknown>', message)

    def test_recovery_and_partial_batch_logs_never_use_original_provider_ids(self):
        from ..executor import diagnostic_call_id
        identifiers = ['PRIVATE-first','PRIVATE-bad','PRIVATE-last']
        def handler(context,args):
            if args.query=='broken': raise RuntimeError('PRIVATE')
            return package(['verified'])
        with self.assertLogs('app.ai.agent', level='INFO') as logs:
            result, client = self.run_turns([
                ModelTurn(tool_calls=[batch(['good'], identifiers[0])]),
                ModelTurn(tool_calls=[batch([], identifiers[1])]),
                review_turn(ModelTurn(tool_calls=[batch(['broken'], identifiers[2])]),1,(0,)),
                ModelTurn(text=json.dumps(decisions(1,[0])))], handler)
        self.assertEqual(result.execution.status, 'completed')
        text = '\n'.join(logs.output)
        self.assertNotIn('PRIVATE', text)
        for identifier in identifiers[1:]: self.assertIn(diagnostic_call_id(identifier), text)
        self.assertEqual([m.call_id for m in client.requests[-1][0] if m.type=='function_call_output'], identifiers)

    def test_policy_rejections_log_safe_reasons_without_changing_stop_policy(self):
        cases = [
            ([ModelTurn(tool_calls=[batch([], 'PRIVATE-ID')]),
              ModelTurn(tool_calls=[batch(['a'],'PRIVATE-ID')])],
             'duplicate_call_id', 'tool_call_linkage', 'invalid_response'),
            ([ModelTurn(tool_calls=[batch(['a'],'PRIVATE-ID').model_copy(update={'name':'PRIVATE_TOOL'})])],
             'unsupported_feed_tool', 'feed_search_admission', 'invalid_response'),
            ([ModelTurn(text='{}')], 'initial_search_required', 'feed_search_admission', 'invalid_response'),
            ([ModelTurn(tool_calls=[batch(['a','b'],'PRIVATE-ID'),batch(['c','d'],'other')])],
             'feed_round_search_limit', 'feed_search_plan', 'limit_reached'),
        ]
        for turns, reason, stage, status in cases:
            with self.subTest(reason=reason), self.assertLogs('app.ai.agent.agent_loop_runner',level='INFO') as logs:
                result, _ = self.run_turns(turns,lambda c,a:self.fail('No RAG before admission'))
            text = '\n'.join(logs.output)
            self.assertEqual(result.execution.status,status)
            self.assertEqual(result.execution.total_tool_calls,0)
            self.assertIn(reason,text)
            self.assertIn('stage='+stage,text)
            self.assertNotIn('PRIVATE',text)
            self.assertIn('recovery_action=abort',text)

    def test_review_missing_and_count_errors_reach_logs_and_llm(self):
        from .test_feed_review_rounds import review
        for payload, reason, expected in ((None,'review_required','all 1 current candidates'),
                                          (review(2),'review_count_mismatch','exactly 1 decisions')):
            with self.subTest(reason=reason):
                bad = batch(['b'],'PRIVATE-bad')
                if payload is not None: bad.arguments['review']=payload
                fixed = batch(['b'],'fixed'); fixed.arguments['review']=review(1)
                reached=[]
                with self.assertLogs('app.ai.agent.agent_loop_runner',level='INFO') as logs:
                    result,client=self.run_turns([ModelTurn(tool_calls=[batch(['a'],'first')]),
                        ModelTurn(tool_calls=[bad]),ModelTurn(tool_calls=[fixed]),
                        ModelTurn(text=json.dumps(decisions(1,[0])))],
                        lambda c,a:reached.append(a.query) or package([a.query]))
                self.assertEqual(result.execution.status,'completed')
                self.assertEqual(reached,['a','b'])
                message=next(json.loads(m.content)['error']['message'] for m in client.requests[2][0]
                             if m.type=='function_call_output' and m.call_id=='PRIVATE-bad')
                self.assertIn(reason,message)
                self.assertIn(expected,message)
                text='\n'.join(logs.output)
                self.assertIn(reason,text)
                self.assertIn('stage=feed_candidate_review',text)
                self.assertNotIn('PRIVATE',text+message)

    def test_normal_feed_executor_receives_server_input_origin_and_restores_context(self):
        from ..executor import tool_diagnostic_context
        client=FakeLLMClient([ModelTurn(tool_calls=[batch(['a'])]),ModelTurn(text=json.dumps(decisions(1)))])
        execution=FeedExecutionV1(client,parallel.registry(lambda c,a:package(['a'])))
        original=execution.runner.executor.execute
        observed=[]
        def capture(call,context):
            observed.append(tool_diagnostic_context.get()['input_origin'])
            return original(call,context)
        before=tool_diagnostic_context.get()
        with patch.object(execution.runner.executor,'execute',side_effect=capture):
            result=execution(CONTEXT,'{}')
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(observed,['feed_search_plan'])
        self.assertIs(tool_diagnostic_context.get(),before)

    def test_invalid_server_conversion_is_not_logged_as_llm_input_or_retried(self):
        from ..tools.contracts import EvidenceSearchInput
        client=FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id='legacy',name='search_evidence',arguments={'query':'a','top_k':3})])])
        execution=FeedExecutionV1(client,parallel.registry(lambda c,a:self.fail('Invalid conversion must not execute RAG')))
        with patch.object(EvidenceSearchInput, 'model_dump', return_value={'query':'a','top_k':0}), \
             self.assertLogs('app.ai.agent.executor',level='INFO') as logs:
            result=execution(CONTEXT,'{}')
        self.assertEqual(result.execution.status,'failed')
        self.assertEqual(len(client.requests),1)
        self.assertEqual(result.execution.errors[-1].code,'invalid_arguments')
        self.assertIn('input_origin=feed_search_plan','\n'.join(logs.output))
        self.assertNotIn('input_origin=llm_arguments','\n'.join(logs.output))

    def test_semantic_review_errors_give_specific_repair_conditions(self):
        decision={'include':True,'personal_reason':'synthetic','category':'care'}
        cases=[(1,{'decisions':[{**decision,'category':'PRIVATE'}]},'invalid_literal','review.decisions[0].category'),
               (1,{'decisions':[{'include':'PRIVATE'}]},'invalid_discriminator','review.decisions[0].include'),
               (6,{'decisions':[decision]*6},'too_many_retained','review.decisions')]
        for count,review,reason,field in cases:
            with self.subTest(reason=reason):
                bad=batch(['b'],'PRIVATE-bad');bad.arguments['review']=review
                reached=[]
                with self.assertLogs('app.ai.agent.agent_loop_runner',level='INFO') as logs:
                    result,client=self.run_turns([ModelTurn(tool_calls=[batch(['a'],'first')]),
                        ModelTurn(tool_calls=[bad]),ModelTurn(text=json.dumps(decisions(count,[0])))],
                        lambda c,a:reached.append(a.query) or package([str(i) for i in range(count)]))
                self.assertEqual(result.execution.status,'completed')
                self.assertEqual(reached,['a'])
                message=next(json.loads(m.content)['error']['message'] for m in client.requests[-1][0]
                             if m.type=='function_call_output' and m.call_id=='PRIVATE-bad')
                self.assertIn(reason,message)
                self.assertIn(field,message)
                self.assertNotIn('PRIVATE',message+'\n'.join(logs.output))
