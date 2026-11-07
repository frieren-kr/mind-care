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
                result, _ = self.run_turns([ModelTurn(tool_calls=[batch(queries)])],
                    lambda c,a: reached.append(a.query))
                self.assertEqual(result.execution.status, 'failed')
                self.assertEqual(result.execution.errors[-1].code, 'invalid_arguments')
                self.assertEqual(reached, [])
        for arguments in ('[]', '{"queries":["a"],"queries":["b"]}', {'queries':['a'], 'top_k':20}):
            bad = batch(['a']).model_copy(update={'arguments':arguments})
            result, _ = self.run_turns([ModelTurn(tool_calls=[bad])], lambda c,a: self.fail('Unexpected I/O'))
            self.assertEqual(result.execution.status, 'failed')

    def test_duplicate_followup_is_rejected_without_rewriting_valid_query(self):
        reached = []
        result, _ = self.run_turns([ModelTurn(tool_calls=[batch(['  Topic A  '])]),
            ModelTurn(tool_calls=[batch(['topic   a'], 'again')])],
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
