"""Independent 10-review / 5-retained / 5-final boundaries; no live services."""
import json
import unittest

from pydantic import ValidationError

from ..feed_candidates import (FeedCandidates, FeedDecisionDraft, FeedDecisionError, FeedReview,
    MAX_FEED_REVIEW_CANDIDATES, MAX_FEED_RETAINED_CANDIDATES,
    MAX_FEED_RECOMMENDATIONS, build_feed_decision_policy)
from ..outputs import FeedAnswerV2
from ..schemas import ModelTurn
from ..testing.fake_llm import FakeLLMClient
from ..workflows.feed_execution import FeedExecutionV1
from ..workflows.feed_prompts import FeedPromptBuilder
from .test_feed_candidates import package, decisions
from .test_feed_review_rounds import review, search
from .test_parallel_feed import registry
from . import test_parallel_feed as parallel
from .test_workflows import CONTEXT


class FeedReviewCapacityTests(unittest.TestCase):
    setUp = parallel.ParallelFeedTests.setUp

    def table(self, count=24):
        table, sources = FeedCandidates(), {}
        # Realistic <=8 rows per original search, including original index ownership.
        for start in range(0, count, 8):
            call_id = f'original-{start//8}'
            sources[call_id] = package([f'p{i}' for i in range(start, min(start+8, count))])
            table.add(call_id, sources[call_id])
        table.finish_round()
        return table, sources

    def test_independent_constants_and_schemas(self):
        self.assertEqual((MAX_FEED_REVIEW_CANDIDATES, MAX_FEED_RETAINED_CANDIDATES, MAX_FEED_RECOMMENDATIONS), (10, 5, 5))
        self.assertEqual(FeedDecisionDraft.model_json_schema()['properties']['decisions']['maxItems'], 10)
        self.assertEqual(FeedReview.model_json_schema()['properties']['decisions']['maxItems'], 10)
        self.assertEqual(FeedAnswerV2.model_json_schema()['properties']['items']['maxItems'], 5)

    def test_zero_one_five_eight_nine_ten_and_overflow_review_counts(self):
        for count in (0, 1, 5, 8, 9, 10, 11, 24):
            with self.subTest(count=count):
                table, sources = self.table(count)
                projected = json.loads(table.message().content)
                self.assertEqual(projected['candidate_count'], min(count, 10))
                self.assertEqual(len(projected['candidates']), min(count, 10))
                self.assertEqual(len(projected['candidates']), min(count, 10))
                output, refs = table.finalize(json.dumps(decisions(table.count)), table.snapshot)
                self.assertEqual((output.items, refs), ([], []))

    def test_ten_decisions_accept_zero_through_five_and_reject_six(self):
        table, sources = self.table()
        positions = (0, 3, 7, 8, 9)
        for count in range(6):
            selected = positions[:count]
            output, refs = table.finalize(json.dumps(decisions(10, selected)), table.snapshot)
            self.assertEqual(len(refs), count)
            self.assertEqual([i.personal_reason for i in output.items], [f'Reason {i}' for i in selected])
            self.assertEqual([i.category for i in output.items], ['care'] * count)
            for pos, item, source in zip(selected, output.items, refs, strict=True):
                original = table._items[pos]
                self.assertEqual(item.source_ref, original.source_ref)
                self.assertEqual(source.pmid, original.evidence.pmid)
                self.assertEqual(sources[item.source_ref.tool_call_id].evidence[item.source_ref.evidence_index], original.evidence)
        with self.assertRaises(FeedDecisionError) as caught:
            table.finalize(json.dumps(decisions(10, range(6))), table.snapshot)
        self.assertEqual(caught.exception.code, 'too_many_selected')

    def test_missing_and_excess_decisions_rejected_without_repair(self):
        table, sources = self.table()
        for count in (0, 5, 9, 11):
            with self.subTest(count=count), self.assertRaises(FeedDecisionError) as caught:
                table.finalize(json.dumps(decisions(count)), table.snapshot)
            self.assertEqual(caught.exception.code, 'candidate_count_mismatch')

    def test_zero_through_five_retained_and_six_rejected(self):
        for count in range(6):
            table, sources = self.table()
            table.apply_review(FeedReview.model_validate(review(table.count, range(count))), table.snapshot)
            self.assertEqual(len(table._retained), count)
            self.assertLessEqual(table.count, 10)
        with self.assertRaises(ValidationError):
            FeedReview.model_validate(review(10, range(6)))

    def test_last_position_allowed_wrong_shape_and_stale_snapshot_rejected(self):
        table, _ = self.table()
        expected = table._items[9]
        original_snapshot = table.snapshot
        table.apply_review(FeedReview.model_validate(review(table.count, (9,))), original_snapshot)
        self.assertEqual(table._retained, [expected])
        with self.assertRaises(FeedDecisionError):
            table.apply_review(FeedReview.model_validate(review(table.count)), original_snapshot)
        for value in (0, 1, 'true', None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                FeedReview.model_validate({'decisions':[{'include':value}]})
        table, _ = self.table()
        for count in (0, 9):
            with self.assertRaises(FeedDecisionError) as caught:
                table.apply_review(FeedReview.model_validate(review(count)), table.snapshot)
            self.assertEqual(caught.exception.code, 'invalid_feed_review')

    def test_retained_two_plus_eight_and_five_plus_five_with_old_pool_reuse(self):
        for retained_indices in ((2, 9), (0, 3, 7, 8, 9)):
            with self.subTest(retained=retained_indices):
                table, sources = self.table()
                old_items = list(table._items)
                shown = {c.evidence.pmid for c in old_items}
                retained = [old_items[i] for i in retained_indices]
                table.apply_review(FeedReview.model_validate(review(table.count, retained_indices)), table.snapshot)
                for topic in range(2):
                    # Repeated old PMID is excluded, unreviewed originals stay reusable.
                    sources[f'next-{topic}'] = package([old_items[0].evidence.pmid] + [f'n{topic}-{i}' for i in range(7)])
                    table.add(f'next-{topic}', sources[f'next-{topic}'])
                table.finish_round()
                payload = json.loads(table.message().content)
                self.assertEqual(table.count, 10)
                self.assertEqual(table._items[:len(retained)], retained)
                self.assertEqual([r['status'] for r in payload['candidates']], ['retained']*len(retained) + ['new']*(10-len(retained)))
                self.assertTrue(all(set(row) == {'status', 'personal_reason', 'category'} for row in payload['candidates'][:len(retained)]))
                fresh = table._items[len(retained):]
                self.assertFalse(shown & {c.evidence.pmid for c in fresh})
                self.assertTrue(any(c.tool_call_id.startswith('original-') for c in fresh))
                pmids = [c.evidence.pmid for c in table._items]
                self.assertEqual(len(pmids), len(set(pmids)))
                # Previously retained ones need not be selected in the final answer.
                chosen = tuple(range(len(retained), len(retained)+5))
                output, refs = table.finalize(json.dumps(decisions(10, chosen)), table.snapshot)
                self.assertTrue(all(s.pmid not in {c.evidence.pmid for c in retained} for s in refs))
                for index, item, source in zip(chosen, output.items, refs, strict=True):
                    self.assertEqual(item.source_ref, table._items[index].source_ref)
                    self.assertEqual(source.pmid, table._items[index].evidence.pmid)

    def test_end_to_end_two_rounds_accept_index_nine_and_keep_final_contract(self):
        pools = {f'q{i}': package([f'{i}-{j}' for j in range(8)]) for i in range(5)}
        client = FakeLLMClient([search(['q0', 'q1', 'q2'], 'first'), search(['q3', 'q4'], 'second', 10, (2, 9)),
            ModelTurn(text=json.dumps(decisions(10, (0, 1, 2, 7, 9))))])
        result = FeedExecutionV1(client, registry(lambda c, a: pools[a.query]))(CONTEXT, '{}')
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual((result.execution.tool_rounds, result.execution.total_tool_calls, len(client.requests)), (2, 5, 3))
        self.assertEqual(len(result.output.items), 5)
        self.assertEqual([s.pmid for s in result.sources[:2]], ['2-0', '0-3'])
        self.assertEqual([(s.source_ref.tool_call_id, s.source_ref.evidence_index) for s in result.sources[:2]], [('feed-rag:1:0:2', 0), ('feed-rag:1:0:0', 3)])
        latest = json.loads(client.requests[-1][0][-1].content)
        self.assertEqual((latest['candidate_count'], latest['retained_count']), (10, 2))
        self.assertEqual([m.call_id for m in client.requests[-1][0] if m.type == "function_call_output"], ['first', 'second'])
        self.assertEqual(result.output.schema_version, '2')

    def test_end_to_end_six_selections_or_retained_are_not_silently_truncated(self):
        for excessive_retain in (False, True):
            reached = []
            second = search(['next'], 'second', 10, range(6)) if excessive_retain else ModelTurn(text=json.dumps(decisions(10, range(6))))
            client = FakeLLMClient([search(['a', 'b', 'c'], 'first'), second])
            result = FeedExecutionV1(client, registry(lambda c, a: reached.append(a.query) or package([f'{a.query}-{i}' for i in range(8)])))(CONTEXT, '{}')
            self.assertIsNone(result.output)
            self.assertEqual(result.sources, [])
            self.assertEqual(len(reached), 3)
            self.assertEqual(result.execution.errors[-1].code, 'invalid_arguments' if excessive_retain else 'too_many_selected')

    def test_excluded_last_candidate_snapshot_still_checked_and_requests_are_isolated(self):
        table, sources = self.table()
        original = table._items[9].evidence.title
        last = table._items[9]
        sources[last.tool_call_id].evidence[last.evidence_index].title = 'tampered'
        self.assertEqual(table._items[9].evidence.title, original)
        snapshot = table.snapshot
        table._items.reverse()
        with self.assertRaises(FeedDecisionError) as caught:
            table.finalize(json.dumps(decisions(10, (0,))), snapshot)
        self.assertEqual(caught.exception.code, 'candidate_source_mismatch')
        self.assertEqual(FeedCandidates().count, 0)

    def test_prompts_distinguish_review_retention_and_final_caps(self):
        policy = build_feed_decision_policy().content
        self.assertIn('최대 10편', policy)
        self.assertIn('각각 최대 5편', policy)
        self.assertIn('최대 5편', policy)
        initial = FeedPromptBuilder().build_system_prompt(CONTEXT)
        self.assertNotIn('검토 후보 최대 10편', initial)
        self.assertNotIn('최종 추천은 최대 5편', initial)
        self.assertIn('편수만으로 자동 종료/재검색하지 않는다', policy)
        self.assertIn('첫 검색을 반드시', initial)
        self.assertNotIn('검토 후보 최대 5편', initial)
