"""Query-local score groups, derived grades and original-source regressions."""
import json
import unittest

from ..feed_candidates import FeedCandidates, FeedDecisionError
from ..schemas import ModelTurn
from ..testing.fake_llm import FakeLLMClient
from ..workflows.feed_execution import FeedExecutionV1
from . import test_parallel_feed as parallel
from .test_feed_candidates import package, decisions
from .test_workflows import CONTEXT


def evidence(rows):
    result = package([row[0] for row in rows])
    for item, (_, kind, score) in zip(result.evidence, rows, strict=True):
        item.study_type, item.relevance_score = kind, score
    return result


class FeedGradePriorityTests(unittest.TestCase):
    setUp = parallel.ParallelFeedTests.setUp

    def select(self, rows):
        source = evidence(rows)
        table = FeedCandidates()
        table.add('original', source)
        output, refs = table.finalize(json.dumps(decisions(table.count, range(min(table.count, 5)))), table.snapshot)
        self.assertEqual([r.source_ref for r in refs], [i.source_ref for i in output.items])
        return [r.pmid for r in refs], table, source

    def test_direct_c_at_094_cannot_be_displaced_by_indirect_a_at_010(self):
        rows = [(str(i), 'rct', .95) for i in range(4)] + [('direct', 'observational', .94), ('indirect', 'meta_analysis', .10)]
        selected, _, _ = self.select(rows)
        self.assertEqual(selected, ['0', '1', '2', '3', 'direct'])

    def test_six_unknowns_do_not_hide_equal_score_a_and_b(self):
        rows = [(str(i), 'other', .9) for i in range(6)] + [('B', 'rct', .9), ('A', 'systematic_review', .9)]
        selected, table, _ = self.select(rows)
        self.assertEqual(selected, ['A', 'B', '0', '1', '2'])
        self.assertEqual(table.selection_stats['eligible_unique_count'], 8)

    def test_score_group_boundary_is_one_rag_rounding_unit_inclusive(self):
        for score, expected in ((.8999, ['A', 'C']), (.89989999, ['C', 'A']), (.1, ['C', 'A'])):
            with self.subTest(score=score):
                selected, _, _ = self.select([('C', 'observational', .9), ('A', 'meta_analysis', score)])
                self.assertEqual(selected, expected)

    def test_close_pairs_cannot_chain_into_a_large_group(self):
        selected, _, _ = self.select([('D', 'review', .9), ('C', 'observational', .8999), ('A', 'meta_analysis', .8998)])
        self.assertEqual(selected, ['C', 'D', 'A'])

    def test_negative_scores_and_nonmonotonic_input_keep_rank_between_groups(self):
        for rows, expected in (([('C', 'observational', -.2), ('A', 'meta_analysis', -.2001)], ['A', 'C']),
                               ([('C', 'observational', .9), ('D', 'review', .1), ('A', 'meta_analysis', .9)], ['C', 'D', 'A'])):
            selected, _, _ = self.select(rows)
            self.assertEqual(selected, expected)

    def test_grade_order_unknown_null_and_unknown_labels_do_not_get_promoted(self):
        selected, table, _ = self.select([('u', 'other', .8), ('D', 'review', .8), ('C', 'qualitative', .8),
            ('B', ' RCT ', .8), ('A', 'guideline', .8), ('null', None, .8), ('future', 'unregistered', .8)])
        self.assertEqual(selected, ['A', 'B', 'C', 'D', 'u'])
        rows = json.loads(table.message().content)['candidates']
        self.assertEqual([r['derived_evidence_grade'] for r in rows], ['A', 'B', 'C', 'D', None, None, None])

    def test_actual_provider_payload_has_derived_grades_without_identity_or_db_grade(self):
        client = FakeLLMClient([ModelTurn(tool_calls=parallel.calls(1)),
            ModelTurn(text=json.dumps(decisions(3, range(3))))])
        result = FeedExecutionV1(client, parallel.registry(lambda c, a: evidence([
            ('unknown', 'other', .9), ('trial', 'rct', .9), ('review', 'systematic_review', .9)])))(CONTEXT, '{}')
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(len(client.requests), 2)
        payload = json.loads(client.requests[1][0][-1].content)
        self.assertEqual([r['derived_evidence_grade'] for r in payload['candidates']], ['A', 'B', None])
        self.assertTrue(all(set(row) == {'status', 'title', 'study_type', 'derived_evidence_grade', 'evidence_excerpt'}
                            for row in payload['candidates']))
        self.assertEqual([s.pmid for s in result.sources], ['review', 'trial', 'unknown'])
        self.assertTrue(all('derived_evidence_grade' not in i.model_dump() for i in result.output.items))

    def test_multi_query_grading_round_robin_and_duplicate_metadata_use_exact_selected_source(self):
        sources = {'A': evidence([('X', 'other', .9), ('a', 'rct', .9)]),
                   'B': evidence([('X', 'meta_analysis', .1), ('b', 'observational', .1)]),
                   'C': evidence([('c', 'qualitative', .01)])}
        originals = {key: value.model_dump_json() for key, value in sources.items()}
        outputs = []
        for _ in range(3):
            table = FeedCandidates()
            for key, source in sources.items(): table.add(key, source)
            output, refs = table.finalize(json.dumps(decisions(4, range(4))), table.snapshot)
            self.assertEqual([r.pmid for r in refs], ['a', 'X', 'c', 'b'])
            self.assertEqual([(r.source_ref.tool_call_id, r.source_ref.evidence_index) for r in refs],
                             [('A', 1), ('B', 0), ('C', 0), ('B', 1)])
            self.assertEqual(refs[1].study_type, 'meta_analysis')
            outputs.append(output.model_dump_json())
        self.assertEqual(len(set(outputs)), 1)
        self.assertEqual({k: v.model_dump_json() for k, v in sources.items()}, originals)
        sources['B'].evidence[0].study_type = 'other'
        _, refs = table.finalize(json.dumps(decisions(4, range(4))), table.snapshot)
        self.assertEqual(refs[1].study_type, 'meta_analysis')
