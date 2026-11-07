"""Request-local review/retention and nine-search provenance; no live I/O."""
import json
import unittest
from copy import deepcopy
from threading import Barrier, Event, Lock, Thread
from unittest.mock import patch

import httpx
from openai import OpenAI

from ..clients.openai_client import OpenAIClient
from ..feed_candidates import FeedCandidates, FeedDecisionError, FeedReview
from ..feed_search_policy import FeedSearchBudgetExceeded, prepare_feed_searches
from ..schemas import ModelTurn
from ..testing.fake_llm import FakeLLMClient
from ..workflows.feed_execution import FeedExecutionV1
from . import test_parallel_feed as parallel
from .test_feed_batch_search import batch
from .test_feed_candidates import decisions, package
from .test_openai_client import response_fixture
from .test_workflows import CONTEXT


def review(candidate_count, retain=()):
    return {'decisions': [
        {'include': True, 'personal_reason': f'유지 이유 {i}', 'category': 'prevention'} if i in retain
        else {'include': False} for i in range(candidate_count)]}


def search(queries, identifier, candidate_count=None, retain=()):
    call = batch(queries, identifier)
    if candidate_count is not None:
        call.arguments['review'] = review(candidate_count, retain)
    return ModelTurn(tool_calls=[call])


class FeedReviewRoundsTests(unittest.TestCase):
    setUp = parallel.ParallelFeedTests.setUp

    def run_feed(self, turns, handler, **options):
        client = FakeLLMClient(turns)
        result = FeedExecutionV1(client, parallel.registry(handler), **options)(CONTEXT, '{"patients":[]}')
        return result, client

    def test_sufficient_first_search_stops_at_two_calls_without_forced_refill(self):
        for count, selected in ((5, (0,)), (2, (1,)), (0, ()), (5, ())):
            with self.subTest(count=count, selected=selected):
                reached = []
                result, client = self.run_feed([search(['a'], 'first'), ModelTurn(text=json.dumps(decisions(count, selected)))],
                    lambda c, a: reached.append(a.query) or package([str(i) for i in range(count)]))
                self.assertEqual(result.execution.status, 'completed')
                self.assertEqual((len(client.requests), len(reached), len(result.output.items)), (2, 1, len(selected)))
                self.assertEqual(client.options[-1].tool_choice, 'auto')

    def test_three_rounds_nine_parallel_searches_seventy_two_results(self):
        turns = [search([f'r{r}q{i}' for i in range(3)], f'provider-{r}', 10 if r else None) for r in range(3)]
        turns.append(ModelTurn(text=json.dumps(decisions(10, range(5)))))
        barriers, entered, lock = [Barrier(3) for _ in range(3)], [], Lock()
        def handler(context, args):
            with lock: entered.append((args.query, args.top_k))
            barriers[int(args.query[1])].wait(2)
            return package([f'{args.query}-{i}' for i in range(8)])
        result, client = self.run_feed(turns, handler)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual((result.execution.tool_rounds, result.execution.total_tool_calls), (3, 9))
        self.assertEqual(len(entered), 9)
        self.assertTrue(all(k == 8 for _, k in entered))
        self.assertEqual(sum(t.result_count for t in result.execution.called_tools), 72)
        self.assertEqual(len(client.requests), 4)
        self.assertEqual([o.tool_choice for o in client.options], ['required', 'auto', 'auto', 'none'])
        projections = [json.loads(messages[-1].content) for messages, _ in client.requests[1:]]
        for key, expected in (('candidate_count', [10, 10, 10]), ('reviewed_count', [10, 20, 30])):
            self.assertEqual([p[key] for p in projections], expected)
        for key, expected in (('search_calls_used', [3, 6, 9]), ('search_calls_remaining', [6, 3, 0]),
                              ('search_rounds_remaining', [2, 1, 0])):
            self.assertEqual([p['search_progress'][key] for p in projections], expected)
        self.assertEqual([m.call_id for m in client.requests[-1][0] if m.type == "function_call_output"], ['provider-0', 'provider-1', 'provider-2'])
        self.assertNotIn('feed-rag:', json.dumps([m.model_dump() for m in client.requests[-1][0]]))
        for source in result.sources:
            _, round_id, call_id, query_id = source.source_ref.tool_call_id.split(':')
            self.assertEqual(source.pmid, f'r{int(round_id)-1}q{query_id}-{source.source_ref.evidence_index}')

    def test_retention_and_reviewed_duplicate_exclusion_keep_original_sources(self):
        initial = package(list('abcdefgh'))
        original = initial.model_dump_json()
        result, client = self.run_feed([search(['a'], 'first'), search(['b'], 'next', 8, (1,)),
            ModelTurn(text=json.dumps(decisions(2, range(2))))], lambda c, a: initial if a.query == 'a' else package(['new', 'b', 'a']))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual([s.pmid for s in result.sources], ['b', 'new'])
        self.assertEqual([(s.source_ref.tool_call_id, s.source_ref.evidence_index) for s in result.sources],
            [('feed-rag:1:0:0', 1), ('feed-rag:2:0:0', 0)])
        latest = json.loads(client.requests[-1][0][-1].content)
        self.assertEqual((latest['candidate_count'], latest['retained_count'], latest['reviewed_count']), (2, 1, 9))
        self.assertEqual(latest['candidates'][0], {'status': 'retained', 'personal_reason': '유지 이유 1', 'category': 'prevention'})
        self.assertEqual(initial.model_dump_json(), original)
        self.assertEqual((result.output.items[0].personal_reason, result.output.items[0].category), ('Reason 0', 'care'))
        serialized = json.dumps([m.model_dump() for m in client.requests[-1][0]])
        self.assertNotIn('Evidence 1', serialized)
        self.assertNotIn('Synthetic paper 1', serialized)

    def test_sdk_same_length_replacement_binds_current_snapshot_and_original_sources(self):
        from .test_responses import function, response

        sources = {'initial': package(['A', 'B', 'C']), 'followup': package(['D', 'E'])}
        for data, labels, years in ((sources['initial'], ('A', 'B', 'C'), (2010, 2011, 2012)),
                                    (sources['followup'], ('D', 'E'), (2024, 2025))):
            for item, label, year in zip(data.evidence, labels, years, strict=True):
                item.title = f'Paper {label}'
                item.doi = f'10.synthetic/{label}'
                item.publication_year = year
                item.journal = f'Journal {label}'
                item.study_type = 'rct'
        before = deepcopy(sources)
        valid = decisions(3, (0, 2))
        invalid_structure = deepcopy(valid)
        invalid_structure['decisions'][1]['source_ref'] = {'tool_call_id': 'invented', 'evidence_index': 0}
        for mode, draft in (('valid', valid), ('short', decisions(2, (0,))),
                            ('long', decisions(4, (0,))), ('invented_ref', invalid_structure)):
            with self.subTest(mode=mode):
                wire, presented, applied, finalized, searches = [], [], [], [], []
                original_message = FeedCandidates.message
                original_review = FeedCandidates.apply_review
                original_finalize = FeedCandidates.finalize
                def capture_message(table, *args, **kwargs):
                    message = original_message(table, *args, **kwargs)
                    presented.append(table.snapshot)
                    return message
                def capture_review(table, judgment, snapshot):
                    if table._review_ready:
                        applied.append(snapshot)
                    return original_review(table, judgment, snapshot)
                def capture_finalize(table, text, snapshot):
                    finalized.append((table, snapshot))
                    return original_finalize(table, text, snapshot)
                def handler(context, args):
                    searches.append((args.query, args.top_k))
                    return sources[args.query]
                turns = [
                    response([function('provider-one', json.dumps({'queries': ['initial']}))]),
                    response([function('provider-two', json.dumps({
                        'queries': ['followup'], 'review': review(3, (2,))}))]),
                    response_fixture({'text': json.dumps(draft)}),
                ]
                def transport(request):
                    wire.append(json.loads(request.content))
                    return httpx.Response(200, json=turns[len(wire) - 1])
                with patch.object(FeedCandidates, 'message', capture_message), \
                     patch.object(FeedCandidates, 'apply_review', capture_review), \
                     patch.object(FeedCandidates, 'finalize', capture_finalize), \
                     OpenAI(api_key='offline', http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
                    result = FeedExecutionV1(OpenAIClient(sdk, model='offline'), parallel.registry(handler))(
                        CONTEXT, '{"patients":[]}')
                self.assertEqual(searches, [('initial', 8), ('followup', 8)])
                self.assertEqual((result.execution.tool_rounds, result.execution.total_tool_calls), (2, 2))
                self.assertEqual(len(wire), 3)
                projections = []
                for body in wire[1:]:
                    rows = [json.loads(item['content']) for item in body['input']
                            if item.get('role') == 'user' and isinstance(item.get('content'), str)
                            and item['content'].startswith('{')
                            and json.loads(item['content']).get('context_type') == 'untrusted_feed_candidates']
                    self.assertEqual(len(rows), 1)
                    projections.append(rows[0])
                self.assertEqual([p['candidate_count'] for p in projections], [3, 3])
                self.assertEqual([r['title'] for r in projections[0]['candidates']],
                                 ['Paper A', 'Paper B', 'Paper C'])
                self.assertEqual([r['status'] for r in projections[1]['candidates']],
                                 ['retained', 'new', 'new'])
                self.assertEqual(projections[1]['candidates'][0]['personal_reason'], '유지 이유 2')
                self.assertEqual([r['title'] for r in projections[1]['candidates'][1:]], ['Paper D', 'Paper E'])
                self.assertEqual(len(presented), 2)
                self.assertEqual([[(k.tool_call_id, k.evidence_index) for k in s.keys] for s in presented], [
                    [('feed-rag:1:0:0', 0), ('feed-rag:1:0:0', 1), ('feed-rag:1:0:0', 2)],
                    [('feed-rag:1:0:0', 2), ('feed-rag:2:0:0', 0), ('feed-rag:2:0:0', 1)]])
                self.assertEqual(len(applied), 1)
                self.assertIs(applied[0], presented[0])
                self.assertEqual(len(finalized), 1)
                table, snapshot = finalized[0]
                self.assertIs(snapshot, presented[1])
                self.assertGreater(snapshot.version, presented[0].version)
                with self.assertRaises(FeedDecisionError):
                    original_finalize(table, json.dumps(valid), presented[0])
                if mode == 'valid':
                    self.assertEqual(result.execution.status, 'completed')
                    self.assertIsNotNone(result.output)
                    self.assertEqual([item.personal_reason for item in result.output.items], ['Reason 0', 'Reason 2'])
                    self.assertEqual([(s.source_ref.tool_call_id, s.source_ref.evidence_index,
                                       s.pmid, s.doi, s.publication_year, s.journal, s.title)
                                      for s in result.sources], [
                        ('feed-rag:1:0:0', 2, 'C', '10.synthetic/C', 2012, 'Journal C', 'Paper C'),
                        ('feed-rag:2:0:0', 1, 'E', '10.synthetic/E', 2025, 'Journal E', 'Paper E')])
                    self.assertEqual([table.store.provider_call_id(k) for k in snapshot.keys],
                                     ['provider-one', 'provider-two', 'provider-two'])
                    self.assertEqual([item.source_ref for item in result.output.items],
                                     [s.source_ref for s in result.sources])
                else:
                    self.assertEqual(result.execution.status, 'invalid_response')
                    self.assertIsNone(result.output)
                    self.assertEqual(result.sources, [])
                    self.assertIsNone(result.execution.final_answer)
                self.assertEqual(sources, before)

    def test_retained_can_be_dropped_on_next_review_or_final_decision(self):
        for final_drop in (False, True):
            result, _ = self.run_feed([search(['one'], 'first'), search(['two'], 'second', 1, (0,)),
                search(['three'], 'third', 2, (0,) if final_drop else (1,)),
                ModelTurn(text=json.dumps(decisions(2, () if final_drop else (0, 1))))], lambda c, a: package([a.query]))
            self.assertEqual(result.execution.status, 'completed')
            self.assertEqual([s.pmid for s in result.sources], [] if final_drop else ['two', 'three'])

    def test_all_five_retained_are_bounded_ordered_and_finally_optional(self):
        result, client = self.run_feed([search(['a'], 'first'), search(['b'], 'next', 5, (4, 0, 2, 1, 3)),
            ModelTurn(text=json.dumps(decisions(10, (1, 3))))],
            lambda c, a: package(list('abcde')) if a.query == 'a' else package([f'new-{i}' for i in range(8)]))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual([s.pmid for s in result.sources], ['b', 'd'])
        latest = json.loads(client.requests[-1][0][-1].content)
        self.assertEqual((latest['retained_count'], latest['candidate_count']), (5, 10))
        self.assertTrue(all(row['status'] == 'retained' and 'evidence_excerpt' not in row for row in latest['candidates'][:5]))

    def test_empty_followup_does_not_reintroduce_reviewed_candidates(self):
        result, _ = self.run_feed([search(['a'], 'first'), search(['b'], 'next', 8, (1,)),
            ModelTurn(text=json.dumps(decisions(1, (0,))))],
            lambda c, a: package(list('abcdefgh')) if a.query == 'a' else package([]))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual([s.pmid for s in result.sources], ['b'])

    def test_review_history_keeps_exact_discarded_occurrence_not_just_pmid(self):
        table, first = FeedCandidates(), package(['a', 'b'])
        sources = {'first': first}
        table.add('first', first)
        table.finish_round()
        table.apply_review(FeedReview.model_validate(review(table.count)), table.snapshot)
        later = package(['a', 'c'])
        later.evidence[0].title = 'Different occurrence of a'
        table.add('later', later)
        table.finish_round()
        self.assertEqual(table._reviewed['a'].tool_call_id, 'first')
        self.assertEqual(table._reviewed['a'].evidence_index, 0)
        self.assertEqual(table._reviewed['a'].evidence, first.evidence[0])
        output, resolved = table.finalize(json.dumps(decisions(1, (0,))), table.snapshot)
        self.assertEqual([source.pmid for source in resolved], ['c'])
        self.assertEqual(output.items[0].source_ref.evidence_index, 1)

    def test_followup_review_validation_shares_request_deadline(self):
        release, entered, finished = Event(), Event(), Event()
        original = FeedCandidates.apply_review
        def blocked(table, review, sources):
            if table._review_ready:
                entered.set()
                try:
                    release.wait(3)
                    return original(table, review, sources)
                finally: finished.set()
            return original(table, review, sources)
        reached = []
        with patch.object(FeedCandidates, 'apply_review', blocked):
            try:
                result, client = self.run_feed([search(['a'], 'first'), search(['b'], 'next', 1)],
                    lambda c, a: reached.append(a.query) or package(['a']), request_timeout_seconds=.3)
                self.assertTrue(entered.is_set())
                self.assertEqual(result.execution.status, 'failed')
                self.assertEqual(result.execution.errors[-1].code, 'request_timeout')
                self.assertEqual((len(client.requests), reached), (2, ['a']))
                self.assertEqual(result.sources, [])
                snapshot = result.model_dump_json()
            finally: release.set()
            self.assertTrue(finished.wait(1))
            self.assertEqual(result.model_dump_json(), snapshot)

    def test_same_topic_refinement_allowed_exact_repeat_rejected(self):
        for query, accepted in (('sleep disturbances in people with dementia at night', True), ('  SLEEP   DISTURBANCES in people with dementia ', False)):
            reached = []
            result, _ = self.run_feed([search(['sleep disturbances in people with dementia'], 'one'), search([query], 'two', 1),
                ModelTurn(text=json.dumps(decisions(1, (0,))))], lambda c, a: reached.append(a.query) or package([a.query]))
            self.assertEqual(len(reached), 2 if accepted else 1)
            self.assertEqual(result.execution.status, 'completed' if accepted else 'failed')

    def test_invalid_review_cannot_start_followup_or_invent_source(self):
        good = review(1, (0,))
        bad_reviews = [None, review(2), review(0), {'decisions':[{'include':1}]},
            {'decisions':[{'include':True}]}, {'decisions':[{'include':True,'personal_reason':' ','category':'care'}]},
            {'decisions':[{'include':True,'personal_reason':'reason','category':'invalid'}]},
            {**good,'candidate_revision':1}, {'decisions':[{'include':False,'candidate_index':0}]},
            {'decisions':[{'include':False,'pmid':'invented'}]}]
        for payload in bad_reviews:
            with self.subTest(review=payload):
                next_turn = search(['b'], 'two')
                if payload is not None: next_turn.tool_calls[0].arguments['review'] = payload
                reached = []
                result, client = self.run_feed([search(['a'], 'one'), next_turn], lambda c, a: reached.append(a.query) or package(['a']))
                self.assertEqual((result.execution.total_tool_calls, reached, len(client.requests)), (1, ['a'], 2))
                self.assertIsNone(result.output)
                self.assertEqual(result.sources, [])
        result, _ = self.run_feed([search(['a'], 'one', 1)], lambda c, a: self.fail('Unexpected I/O'))
        self.assertEqual(result.execution.total_tool_calls, 0)

    def test_per_round_three_and_whole_request_limits_are_preflight(self):
        kwargs = dict(turn=3, remaining_calls=9, remaining_budget=72, previous_queries=set(), reserved_ids=set())
        for calls, options in (([batch(['a', 'b', 'c']), batch(['d'], 'other')], kwargs),
                               ([batch(['a', 'b'])], {**kwargs, 'remaining_budget': 15}),
                               ([batch(['a', 'b'])], {**kwargs, 'remaining_calls': 1})):
            with self.assertRaises(FeedSearchBudgetExceeded): prepare_feed_searches(calls, **options)
        reached = []
        result, client = self.run_feed([search([str(i)], str(i), 0 if i else None) for i in range(4)],
            lambda c, a: reached.append(a.query) or package([]))
        self.assertEqual(result.execution.status, 'limit_reached')
        self.assertEqual((len(reached), len(client.requests), result.execution.tool_rounds), (3, 4, 3))

    def test_multiple_real_calls_share_consistent_review_and_get_real_acks(self):
        one, two = batch(['b'], 'second'), batch(['c'], 'third')
        one.arguments['review'] = review(1, (0,))
        for conflicting in (False, True):
            second = deepcopy(two)
            if conflicting: second.arguments['review'] = review(1)
            reached = []
            result, client = self.run_feed([search(['a'], 'first'), ModelTurn(tool_calls=[one, second]),
                ModelTurn(text=json.dumps(decisions(3, range(3))))], lambda c, a: reached.append(a.query) or package([a.query]))
            self.assertEqual(result.execution.status, 'failed' if conflicting else 'completed')
            self.assertEqual(len(reached), 1 if conflicting else 3)
            if not conflicting:
                self.assertEqual([m.call_id for m in client.requests[-1][0] if m.type == "function_call_output"], ['first', 'second', 'third'])

    def test_same_pmid_new_grade_does_not_replace_retained_snapshot(self):
        initial, later = package(['same']), package(['same', 'new'])
        initial.evidence[0].study_type = 'other'
        later.evidence[0].study_type = 'systematic_review'
        result, _ = self.run_feed([search(['a'], 'first'), search(['b'], 'next', 1, (0,)), ModelTurn(text=json.dumps(decisions(2, range(2))))],
            lambda c, a: initial if a.query == 'a' else later)
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual((result.sources[0].source_ref.tool_call_id, result.sources[0].study_type), ('feed-rag:1:0:0', 'other'))
        self.assertEqual([s.pmid for s in result.sources], ['same', 'new'])

    def test_review_revalidates_even_excluded_original_snapshot(self):
        table, original = FeedCandidates(), package(['a', 'b'])
        table.add('source', original)
        table.finish_round()
        saved = table.snapshot
        original.evidence[1].title = 'modified outside the store'
        self.assertNotEqual(table._items[1].evidence.title, original.evidence[1].title)
        table._items.reverse()
        with self.assertRaises(FeedDecisionError) as error:
            table.apply_review(FeedReview.model_validate(review(2, (0,))), saved)
        self.assertEqual(error.exception.code, 'candidate_source_mismatch')

    def test_followup_failure_atomic_and_late_result_cannot_publish(self):
        release, finished, barrier = Event(), Event(), Barrier(2)
        def handler(context, args):
            if args.query == 'first': return package(['a'])
            barrier.wait(2)
            if args.query == 'fail': raise RuntimeError('PRIVATE')
            try: release.wait(4)
            finally: finished.set()
            return package(['late'])
        box = {}
        def run():
            box['result'], box['client'] = self.run_feed([search(['first'], 'first'), search(['slow', 'fail'], 'next', 1, (0,))],
                handler, tool_timeout_seconds=3, request_timeout_seconds=4)
        worker = Thread(target=run, daemon=True)
        worker.start()
        try:
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertFalse(finished.is_set())
            result = box['result']
            self.assertEqual(result.execution.status, 'failed')
            self.assertIsNone(result.output)
            self.assertEqual(result.sources, [])
            self.assertEqual(len(box['client'].requests), 2)
            snapshot = result.model_dump_json()
        finally: release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(box['result'].model_dump_json(), snapshot)

    def test_state_is_not_shared_with_later_request(self):
        client = FakeLLMClient([search(['a'], 'first'), search(['b'], 'next', 1), ModelTurn(text=json.dumps(decisions(1, (0,)))),
                               search(['a'], 'again'), ModelTurn(text=json.dumps(decisions(1, (0,))))])
        execution = FeedExecutionV1(client, parallel.registry(lambda c, a: package([a.query])))
        first, second = execution(CONTEXT, '{}'), execution(CONTEXT.model_copy(update={'request_id': 'another'}), '{}')
        self.assertEqual([s.pmid for s in first.sources], ['b'])
        self.assertEqual([s.pmid for s in second.sources], ['a'])

    def test_sdk_mock_transport_review_and_real_tool_results(self):
        wire = []
        def transport(request):
            wire.append(json.loads(request.content))
            if len(wire) <= 2:
                call = search(['a' if len(wire) == 1 else 'b'], f'sdk-{len(wire)}', 1 if len(wire) == 2 else None, (0,)).tool_calls[0]
                return httpx.Response(200, json=response_fixture({'text': None, 'calls': [{'type': 'function_call', 'id': 'item_' + call.id, 'call_id': call.id, 'name': call.name, 'arguments': json.dumps(call.arguments), 'status': 'completed'}]}))
            return httpx.Response(200, json=response_fixture({'text': json.dumps(decisions(2, (0, 1)))}))
        sdk = OpenAI(api_key='offline-placeholder', http_client=httpx.Client(transport=httpx.MockTransport(transport)))
        self.addCleanup(sdk.close)
        result = FeedExecutionV1(OpenAIClient(sdk, model='offline-model'), parallel.registry(lambda c, a: package([a.query])))(CONTEXT, '{}')
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(len(wire), 3)
        self.assertEqual([m['call_id'] for m in wire[-1]['input'] if m.get("type") == "function_call_output"], ['sdk-1', 'sdk-2'])
        self.assertEqual([p['tool_choice'] for p in wire], ['required', 'auto', 'auto'])
        self.assertTrue(all('reasoning' not in p for p in wire))
        self.assertNotIn('feed-rag:', json.dumps(wire))
