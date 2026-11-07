"""Offline retrieval capacity, research filtering and exact original provenance."""
import json
import unittest
from threading import Barrier, Lock
from unittest.mock import patch

from ..agent_loop_runner import AgentLoopRunner
from ..feed_candidates import FeedCandidates
from ..feed_search_policy import bounded_feed_searches
from ..outputs import FeedAnswerV2
from ..schemas import ModelTurn, ToolCall
from ..testing.fake_llm import FakeLLMClient
from ..tools.contracts import EvidenceSearchInput
from ..workflows.feed_execution import FeedExecutionV1
from ..workflows.feed_prompts import FeedPromptBuilder
from .test_feed_candidates import decisions, package, review_turn
from . import test_parallel_feed as parallel
from .test_parallel_feed import calls, registry
from .test_workflows import CONTEXT


class FeedRetrievalBudgetTests(unittest.TestCase):
    setUp = parallel.ParallelFeedTests.setUp

    def resolve(self, packages):
        table = FeedCandidates()
        for call_id, source in packages.items():
            table.add(call_id, source)
        output, refs = table.finalize(json.dumps(decisions(table.count, range(min(table.count, 5)))), table.snapshot)
        return table, output, refs

    def test_one_two_three_queries_retrieve_eight_sixteen_twenty_four_in_parallel(self):
        for count in (1, 2, 3):
            with self.subTest(count=count):
                barrier, lock, searched = Barrier(count), Lock(), []
                def handler(context, args):
                    with lock:
                        searched.append((args.query, args.top_k))
                    barrier.wait(2)
                    return package([f'{args.query}-{i}' for i in range(args.top_k)])
                client = FakeLLMClient([ModelTurn(tool_calls=calls(count)),
                    ModelTurn(text=json.dumps(decisions(min(count*8, 10), range(5))))])
                result = FeedExecutionV1(client, registry(handler))(CONTEXT, '{}')
                self.assertEqual(result.execution.status, 'completed')
                self.assertCountEqual(searched, [(f'topic {i}', 8) for i in range(count)])
                self.assertEqual(sum(trace.result_count for trace in result.execution.called_tools), count * 8)
                self.assertEqual(result.execution.total_tool_calls, count)
                self.assertEqual(len(client.requests), 2)
                projection = json.loads(client.requests[1][0][-1].content)
                self.assertEqual(projection['candidate_count'], min(count*8, 10))
                self.assertEqual(len(result.output.items), 5)
                # All 8/16/24 originals stay internal; only 8/10/10 are projected.
                for source in result.sources:
                    query = f'topic {source.source_ref.tool_call_id[-1]}'
                    self.assertEqual(source.pmid, f'{query}-{source.source_ref.evidence_index}')
                serialized = json.dumps([m.model_dump(mode='json') for m in client.requests[1][0]])
                self.assertEqual(sum(len(json.loads(m.content)['candidates']) for m in client.requests[1][0] if m.kind == 'evidence'), min(count*8, 10))

    def test_default_is_feed_only_and_query_ids_arguments_are_not_mutated(self):
        original = calls(1)[0]
        original.arguments = json.dumps({'query': '  patient sleep research  '})
        before = original.model_dump_json()
        admitted = bounded_feed_searches([original])[0]
        self.assertEqual(admitted.arguments, {'query': '  patient sleep research  ', 'top_k': 8})
        self.assertEqual(admitted.id, original.id)
        self.assertEqual(original.model_dump_json(), before)
        self.assertEqual(EvidenceSearchInput(query='chat default').top_k, 5)
        self.assertEqual(EvidenceSearchInput(query='chat maximum', top_k=20).top_k, 20)
        original.arguments = {'query': 'explicit smaller search', 'top_k': 3}
        self.assertEqual(bounded_feed_searches([original])[0].arguments['top_k'], 3)

    def test_budget_is_explicit_for_entire_batch_and_remaining_request(self):
        self.assertEqual(sum(c.arguments['top_k'] for c in bounded_feed_searches(calls(3))), 24)
        for count, remaining in ((3, 23), (2, 15), (1, 7), (1, 0)):
            with self.subTest(count=count, remaining=remaining), self.assertRaises(ValueError):
                bounded_feed_searches(calls(count), remaining_budget=remaining)
        for value in (9, 20, 0, True, '8'):
            requested = calls(3)
            requested[-1].arguments['top_k'] = value
            touched = []
            client = FakeLLMClient([ModelTurn(tool_calls=requested)])
            result = FeedExecutionV1(client, registry(lambda c, a: touched.append(True)))(CONTEXT, '{}')
            self.assertEqual(result.execution.status, 'failed')
            self.assertEqual(result.execution.total_tool_calls, 0)
            self.assertEqual(touched, [])

    def test_three_rounds_retain_24_budget_and_tool_call_limit(self):
        turns = []
        for i in range(3):
            call = calls(1)[0].model_copy(update={'id': str(i), 'arguments': {'query': f'topic {i}'}})
            turn = ModelTurn(tool_calls=[call])
            turns.append(review_turn(turn, 8) if i else turn)
        turns.append(ModelTurn(text=json.dumps(decisions(8, range(5)))))
        client = FakeLLMClient(turns)
        result = FeedExecutionV1(client, registry(
            lambda c, a: package([f'{a.query}-{i}' for i in range(a.top_k)])))(CONTEXT, '{}')
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual((result.execution.tool_rounds, result.execution.total_tool_calls), (3, 3))
        self.assertEqual(sum(t.result_count for t in result.execution.called_tools), 24)
        self.assertEqual(client.options[-1].tool_choice, 'none')

    def test_retrieval_budget_cannot_reset_on_followup_even_with_larger_generic_runner_limits(self):
        # A generic Runner cannot exceed the Feed-specific three-round cap.
        turns = [ModelTurn(tool_calls=[ToolCall(id=str(i), name='search_evidence',
            arguments={'query': f'topic {i}'})]) for i in range(4)]
        turns = [review_turn(turn, 8) if i else turn for i, turn in enumerate(turns)]
        searched = []
        def handler(context, args):
            searched.append(args.top_k)
            return package([f'{args.query}-{i}' for i in range(args.top_k)])
        client = FakeLLMClient(turns)
        result = AgentLoopRunner(client, registry(handler), prompt_builder=FeedPromptBuilder()).run_structured('{}', CONTEXT, output_model=FeedAnswerV2)
        self.assertEqual(result.execution.status, 'limit_reached')
        self.assertEqual(result.execution.errors[-1].code, 'tool_limit')
        self.assertEqual(searched, [8, 8, 8])

    def test_oversized_retriever_return_fails_atomically_before_candidate_publish(self):
        client = FakeLLMClient([ModelTurn(tool_calls=calls(2))])
        with patch.object(FeedCandidates, 'add') as publish:
            result = FeedExecutionV1(client, registry(lambda c, a: package(list('123456789'))))(CONTEXT, '{}')
            publish.assert_not_called()
        self.assertEqual(result.execution.status, 'failed')
        self.assertEqual(result.execution.errors[-1].code, 'evidence_validation_error')
        self.assertEqual(result.sources, [])
        self.assertEqual(len(client.requests), 1)

    def test_duplicate_and_ineligible_types_backfill_from_remaining_original_results(self):
        source = package(['x', 'p', 'o', 'a', 'x', 'b', 'c', 'd'])
        for index, kind in ((1, 'protocol'), (2, 'expert_opinion'), (3, 'animal')):
            source.evidence[index].study_type = kind
        table, output, refs = self.resolve({'original': source})
        self.assertEqual([r.pmid for r in refs], ['x', 'b', 'c', 'd'])
        self.assertEqual([i.source_ref.evidence_index for i in output.items], [0, 5, 6, 7])
        stats = table.selection_stats
        self.assertEqual(stats['returned_count'], 8)
        self.assertEqual(stats['duplicate_pmid_count'], 1)
        self.assertEqual(stats['excluded_study_types'], {'animal': 1, 'expert_opinion': 1, 'protocol': 1})
        self.assertEqual(stats['eligible_unique_count'], 4)
        self.assertEqual(stats['llm_candidate_count'], 4)

    def test_type_filtering_fills_five_from_valid_tail_and_preserves_original_indices(self):
        source = package([str(i) for i in range(8)])
        for i, kind in enumerate(('protocol', 'expert_opinion', 'protocol')):
            source.evidence[i].study_type = kind
        table, output, refs = self.resolve({'original': source})
        self.assertEqual([r.pmid for r in refs], list('34567'))
        self.assertEqual([i.source_ref.evidence_index for i in output.items], [3, 4, 5, 6, 7])
        self.assertEqual(table.selection_stats['llm_candidate_count'], 5)
        self.assertEqual(source.evidence[0].study_type, 'protocol')  # No source mutation.

    def test_topic_coverage_uses_rag_rank_not_global_similarity_or_global_grade(self):
        source = {'sleep': package([f'sleep-{i}' for i in range(8)]),
                  'wandering': package(['wandering']), 'falls': package(['falls'])}
        for item in source['sleep'].evidence:
            item.relevance_score = 0.99
            item.study_type = 'meta_analysis'
        for key in ('wandering', 'falls'):
            source[key].evidence[0].relevance_score = 0.1
            source[key].evidence[0].study_type = 'observational'
        _, _, refs = self.resolve(source)
        self.assertEqual([r.pmid for r in refs], ['sleep-0', 'wandering', 'falls', 'sleep-1', 'sleep-2'])

    def test_derived_grade_prioritizes_equal_scores_without_discarding_unknowns(self):
        source = package(['case', 'trial', 'meta', 'unknown', 'review', 'qualitative', 'other', 'last'])
        kinds = ['case_report', ' RCT ', 'meta_analysis', None, 'review', 'qualitative', 'other', 'other']
        for item, kind in zip(source.evidence, kinds, strict=True):
            item.study_type = kind
        table, output, refs = self.resolve({'original': source})
        self.assertEqual([r.pmid for r in refs], ['meta', 'trial', 'qualitative', 'case', 'review'])
        self.assertEqual([i.source_ref.evidence_index for i in output.items], [2, 1, 5, 0, 4])
        self.assertEqual(table.eligible_count, 8)

    def test_unclassified_or_misclassified_labels_do_not_collapse_pool_to_zero(self):
        source = package(['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'])
        kinds = [None, 'other', '', 'unknown', 'future_study_type', 'protocol_like_label', 'rct', 'systematic_review']
        for item, kind in zip(source.evidence, kinds, strict=True):
            item.study_type = kind
        table, _, refs = self.resolve({'original': source})
        self.assertEqual(len(refs), 5)
        self.assertEqual(table.selection_stats['unclassified_count'], 6)
        self.assertEqual(table.selection_stats['eligible_unique_count'], 8)
        self.assertEqual(sum(table.selection_stats['excluded_study_types'].values()), 0)

    def test_all_ineligible_or_missing_pmids_allow_empty_feed_without_forced_fill(self):
        source = package(['p', 'e', 'a', None, '  '])
        for i, kind in enumerate(('protocol', 'expert_opinion', 'animal')):
            source.evidence[i].study_type = kind
        table, output, refs = self.resolve({'original': source})
        self.assertEqual((output.items, refs), ([], []))
        self.assertEqual(table.selection_stats['missing_pmid_count'], 2)
        self.assertEqual(table.selection_stats['returned_count'], 5)
        self.assertEqual(table.selection_stats['eligible_unique_count'], 0)
