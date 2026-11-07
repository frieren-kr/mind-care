"""Labeled synthetic cohorts, NOT measured RAG or real LLM recommendation quality."""
import json
import unittest

from ..feed_candidates import FeedCandidates
from ..schemas import ModelTurn, ToolCall
from ..testing.fake_llm import FakeLLMClient
from ..workflows.feed_execution import FeedExecutionV1
from . import test_parallel_feed as parallel
from .test_feed_candidates import decisions
from .test_feed_grade_priority import evidence
from .test_workflows import CONTEXT

QUERIES = {'sleep': 'sleep disturbances in people with dementia',
           'wandering': 'wandering behavior management in dementia',
           'falls': 'fall prevention in people with dementia',
           'support': 'caregiver stress support interventions'}
SCENARIOS = {'sleep': ('sleep',), 'wandering': ('wandering',), 'falls': ('falls',),
             'sleep_wandering': ('sleep', 'wandering'), 'wandering_falls': ('wandering', 'falls'),
             'three_symptoms': ('sleep', 'wandering', 'falls'),
             'multiple_patients': ('sleep', 'wandering', 'falls'),
             'patient_and_caregiver': ('wandering', 'support')}


def topic_package(topic):
    # Explicit synthetic ground truth: first 7 match the requested target;
    # last is an indirect general-health paper despite its stronger grade.
    source = evidence([(f'{topic}:{i}', 'observational' if topic != 'support' else 'rct', .95 - i * .01)
                       for i in range(7)] + [(f'indirect:{topic}', 'meta_analysis', .1)])
    source.query = QUERIES[topic]
    for i, item in enumerate(source.evidence):
        item.title = (f'Patient dementia {topic} management study' if topic != 'support'
                      else 'Family caregiver support study') if i < 7 else 'Indirect general caregiver health review'
    return source


def cohort():
    result = {name: {str(i): topic_package(t) for i, t in enumerate(topics)} for name, topics in SCENARIOS.items()}
    result['equal_unknowns'] = {'0': evidence([(str(i), 'other', .9) for i in range(6)] + [('B', 'rct', .9), ('A', 'systematic_review', .9)])}
    result['other_only'] = {'0': evidence([(str(i), 'other', .9 - i * .01) for i in range(8)])}
    result['mixed_equal'] = {'0': evidence([(str(i), s, .8) for i, s in enumerate(['other'] * 5 + ['qualitative', 'rct', 'meta_analysis'])])}
    result['unknown_strong'] = {'0': evidence([('direct', 'other', .99), ('indirect', 'meta_analysis', .1)])}
    result['excluded'] = {'0': evidence([(str(i), s, .8) for i, s in enumerate(['protocol', 'expert_opinion', 'animal', 'rct', 'observational', 'review', None, 'unregistered'])])}
    result['duplicates'] = {'0': evidence([('X', 'other', .9), ('a', 'rct', .9)]),
                            '1': evidence([('X', 'meta_analysis', .1), ('b', 'qualitative', .1)])}
    result['empty'] = {'0': evidence([])}
    result['all_excluded'] = {'0': evidence([(str(i), s, .8) for i, s in enumerate(['protocol', 'expert_opinion', 'animal'])])}
    result['few'] = {'0': evidence([('one', None, .9), ('two', 'other', .8)])}
    result['gap'] = {'0': evidence([(str(i), 'rct', .95) for i in range(4)] + [('direct', 'observational', .94), ('indirect', 'meta_analysis', .1)])}
    result['order_only'] = {'0': evidence([(str(i), s, .8) for i, s in enumerate(['review', 'observational', 'rct', 'meta_analysis'])])}
    result['near_boundary'] = {'0': evidence([(str(i), 'rct', .95) for i in range(4)] + [('C', 'observational', .9), ('A', 'meta_analysis', .8999)])}
    result['missing_identity'] = {'0': evidence([(None, 'rct', .9), (' ', 'meta_analysis', .8), ('valid', 'qualitative', .7)])}
    return result


def relevance_baseline(packages):
    """Independent rank-only comparison: same exclusions, selection-time dedup and cap."""
    pools = [[(call_id, i) for i, e in enumerate(p.evidence)
              if e.pmid and e.pmid.strip() and (e.study_type or '').strip().lower()
              not in {'protocol', 'expert_opinion', 'animal'}] for call_id, p in packages.items()]
    selected, seen = [], set()
    for rank in range(max(map(len, pools), default=0)):
        for pool in pools:
            if rank >= len(pool): continue
            call, index = pool[rank]
            pmid = packages[call].evidence[index].pmid
            if pmid in seen: continue
            selected.append((call, index))
            seen.add(pmid)
            if len(selected) == 5: return selected
    return selected


class FeedQualityValidationTests(unittest.TestCase):
    setUp = parallel.ParallelFeedTests.setUp

    def test_eight_labeled_patient_scenarios_keep_topics_in_actual_provider_payload(self):
        for name, topics in SCENARIOS.items():
            with self.subTest(name=name):
                packages = {QUERIES[t]: topic_package(t) for t in topics}
                client = FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id=str(i), name='search_evidence',
                    arguments={'query': QUERIES[t]}) for i, t in enumerate(topics)]),
                    ModelTurn(text=json.dumps(decisions(min(len(topics)*8, 10), range(5))))])
                symptoms = [t for t in topics if t != 'support']
                patients = ([{'patient_ref': f'p{i+1}', 'symptoms': [t]} for i, t in enumerate(symptoms)]
                            if name == 'multiple_patients' else [{'patient_ref': 'p1', 'symptoms': symptoms}])
                prepared = json.dumps({'feed_scope': 'caregiver_all', 'patients': patients,
                    'caregiver_profile': {'interests': ['stress support'] if 'support' in topics else []}})
                result = FeedExecutionV1(client, parallel.registry(lambda c, a: packages[a.query]))(CONTEXT, prepared)
                self.assertEqual(result.execution.status, 'completed')
                self.assertEqual(len(client.requests), 2)
                self.assertEqual({s.pmid.split(':')[0] for s in result.sources}, set(topics))
                self.assertFalse(any(s.pmid.startswith('indirect:') for s in result.sources))
                self.assertEqual(len(result.output.items), 5)
                projection = json.loads(client.requests[1][0][-1].content)
                self.assertEqual(projection['candidate_count'], min(len(topics)*8, 10))
                self.assertEqual(next(m.content for m in client.requests[0][0] if m.role == 'user'), prepared)
                for t in topics:
                    if t != 'support': self.assertNotIn('caregiver', QUERIES[t])

    def test_same_corpus_comparison_distinguishes_set_changes_from_order_only(self):
        changes = {}
        for name, packages in cohort().items():
            table = FeedCandidates()
            for call_id, source in packages.items(): table.add(call_id, source)
            output, refs = table.finalize(json.dumps(decisions(table.count, range(min(table.count, 5)))), table.snapshot)
            actual = [r.pmid for r in refs]
            baseline = [packages[c].evidence[i].pmid for c, i in relevance_baseline(packages)]
            changes[name] = (actual, baseline)
            self.assertLessEqual(len(actual), 5)
            self.assertEqual(len(actual), len(set(actual)))
            self.assertEqual([r.source_ref for r in refs], [i.source_ref for i in output.items])
        self.assertNotEqual(set(changes['equal_unknowns'][0]), set(changes['equal_unknowns'][1]))
        self.assertEqual(set(changes['excluded'][0]), set(changes['excluded'][1]))
        self.assertEqual(set(changes['order_only'][0]), set(changes['order_only'][1]))
        self.assertNotEqual(changes['order_only'][0], changes['order_only'][1])
        self.assertEqual(changes['gap'][0], changes['gap'][1])
        self.assertEqual(changes['unknown_strong'][0][0], 'direct')

    def test_scripted_selection_counts_and_empty_candidates_do_not_force_fill(self):
        for candidates, selected in ((5, 5), (5, 3), (5, 1), (5, 0), (2, 2), (0, 0)):
            with self.subTest(candidates=candidates, selected=selected):
                source = evidence([(str(i), 'rct', .9) for i in range(candidates)])
                client = FakeLLMClient([ModelTurn(tool_calls=parallel.calls(1)),
                    ModelTurn(text=json.dumps(decisions(candidates, range(selected))))])
                result = FeedExecutionV1(client, parallel.registry(lambda c, a: source))(CONTEXT, '{}')
                self.assertEqual(result.execution.status, 'completed')
                self.assertEqual(len(result.output.items), selected)
                self.assertEqual(len(result.sources), selected)

    def test_model_cannot_invent_candidates_or_supply_source_refs(self):
        payloads = [decisions(6, range(6)), {'decisions': [{'include': True, 'personal_reason': 'Synthetic',
            'category': 'care', 'source_ref': {'tool_call_id': 'invented', 'evidence_index': 99}}]}]
        for payload in payloads:
            client = FakeLLMClient([ModelTurn(tool_calls=parallel.calls(1)), ModelTurn(text=json.dumps(payload))])
            result = FeedExecutionV1(client, parallel.registry(lambda c, a: evidence([('one', 'rct', .9)])))(CONTEXT, '{}')
            self.assertEqual(result.execution.status, 'invalid_response')
            self.assertIsNone(result.output)
            self.assertEqual(result.sources, [])
