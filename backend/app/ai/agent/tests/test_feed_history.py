"""Recent Feed history stays request-local: titles to Provider, PMIDs to selection."""
from ..testing.fake_llm import fake_output
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from datetime import timedelta, timezone
import json
from threading import Barrier
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from pydantic import ValidationError

from ..agent_loop_runner import AgentLoopRunner
from ..feed_candidates import FeedCandidates, FeedReview
from ..outputs import ChatAnswerV1
from ..schemas import ModelTurn
from ..testing.fake_llm import FakeLLMClient
from ..workflows.feed import FeedWorkflow
from ..workflows.feed_context import FeedPersonalizationContextV1
from ..workflows.feed_context_backend_source import ProductionFeedContextSource
from ..workflows.feed_context_loader import DefaultFeedContextLoader, FeedContextLoadError
from ..workflows.feed_context_prepare import (
    MAX_FEED_HISTORY_TITLES, PreparedFeedInput, build_production_feed_provider_input,
    prepare_production_feed_input,
)
from ..workflows.feed_context_source import FeedContextSourceSnapshot
from ..workflows.feed_execution import FeedExecutionV1
from ..workflows.feed_prompts import FeedPromptBuilder
from .test_feed_candidates import decisions, package
from .test_feed_context_loader import empty_snapshot
from .test_feed_context_production_source import CONTEXT, NOW, ARGS, snapshot
from .test_feed_review_rounds import review, search
from .test_parallel_feed import registry
from app.user_schemas import APP_TIMEZONE


def history(count):
    return [{"paper_id": UUID(int=1000+i), "pmid": f"history-pmid-{i}",
             "title": f"Stored research title {i}", "last_recommended_at": NOW-timedelta(minutes=i),
             "times_recommended": i+1} for i in range(count)]


def load_history(rows):
    raw = snapshot()
    # Backend DTO validates the supplied rows before the actual Source/Loader path.
    raw = type(raw).model_validate({**raw.model_dump(), "recently_recommended": rows})
    read = Mock(return_value=raw)
    loader = DefaultFeedContextLoader(source=ProductionFeedContextSource(read_snapshot=read),
                                     clock=lambda: NOW, service_timezone=APP_TIMEZONE)
    return loader, read


class FeedHistoryContextTests(unittest.TestCase):
    def project_titles(self, titles):
        rows = history(len(titles))
        for row, title in zip(rows, titles):
            row['title'] = title
        loader, _ = load_history(rows)
        value = loader(CONTEXT, recent_days=30)
        before = value.model_dump()
        plain = build_production_feed_provider_input(value, service_timezone=APP_TIMEZONE)
        self.assertEqual(value.model_dump(), before)
        self.assertEqual([r.title for r in value.recently_recommended], titles)
        prepared = prepare_production_feed_input(CONTEXT, loader)
        self.assertEqual(plain, prepared)
        self.assertEqual(prepared.previously_recommended_pmids, frozenset(r['pmid'] for r in rows))
        projected = json.loads(prepared)['recent_feed_titles']
        self.assertLessEqual(len(json.dumps(projected, ensure_ascii=False, allow_nan=False).encode('utf-8')), 8192)
        return projected, prepared

    def test_title_character_boundary_skips_without_truncation_or_backfill(self):
        titles = ['a'*300, 'b'*301] + [f'Original {i}' for i in range(18)] + ['Not a replacement']
        projected, _ = self.project_titles(titles)
        self.assertEqual(projected, [titles[0], *titles[2:20]])

    def test_abnormally_long_titles_do_not_fail_prepare(self):
        projected, prepared = self.project_titles(['x'*10007]*20)
        self.assertEqual(projected, [])
        self.assertEqual(len(prepared.previously_recommended_pmids), 20)

    def test_utf8_array_budget_accepts_exact_boundary_and_stops_on_overflow(self):
        titles = ['한'*300]*9 + ['x'*52, '', 'Must not be added']
        projected, _ = self.project_titles(titles)
        self.assertEqual(projected, titles[:10])
        self.assertEqual(len(json.dumps(projected, ensure_ascii=False, allow_nan=False).encode('utf-8')), 8192)

    def test_multilingual_overflow_stops_instead_of_skipping_to_shorter_title(self):
        titles = ['한日語'*100]*10 + ['Short tail']
        projected, _ = self.project_titles(titles)
        self.assertEqual(projected, titles[:9])

    def test_json_escaping_counts_quotes_backslashes_and_control_characters(self):
        for title, expected in [('"\\'*150, 13), ('\x00'*300, 4)]:
            with self.subTest(expected=expected):
                projected, _ = self.project_titles([title]*20)
                self.assertEqual(projected, [title]*expected)
                self.assertGreater(len(json.dumps([*projected, title], ensure_ascii=False,
                                                  allow_nan=False).encode('utf-8')), 8192)

    def test_title_restrictions_do_not_reduce_fifty_pmid_exclusions(self):
        titles = ['Too long '*100] + ['한'*300]*19 + [f'Outside top twenty {i}' for i in range(30)]
        projected, prepared = self.project_titles(titles)
        self.assertEqual(projected, titles[1:10])
        self.assertEqual(len(prepared.previously_recommended_pmids), 50)
        # Index 0: long title; 10: byte overflow; 49: outside top twenty.
        client = FakeLLMClient([search(['one'], 'first'), ModelTurn(text=json.dumps(decisions(1, (0,))))])
        execute = FeedExecutionV1(client, registry(lambda c, a: package(
            ['history-pmid-0', 'history-pmid-10', 'history-pmid-49', 'NEW'])))
        result = execute(CONTEXT, deepcopy(prepared))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual([s.pmid for s in result.sources], ['NEW'])
        self.assertEqual(result.sources[0].source_ref.evidence_index, 3)

    def test_immutable_prepared_input_copy_and_deepcopy_preserve_identity_and_metadata(self):
        for pmids in (frozenset(), frozenset({'123', '456'})):
            prepared = PreparedFeedInput('{}', pmids)
            memo = {}
            self.assertIs(copy(prepared), prepared)
            self.assertIs(deepcopy(prepared, memo), prepared)
            self.assertIs(memo[id(prepared)], prepared)
            self.assertIsInstance(prepared, str)
            self.assertEqual(json.loads(prepared), {})
            self.assertEqual(json.dumps(prepared), '"{}"')
            self.assertEqual(prepared.previously_recommended_pmids, pmids)
            with self.assertRaises(AttributeError):
                prepared.previously_recommended_pmids = frozenset()
        candidates = FeedCandidates(previously_recommended_pmids=frozenset({'A'}))
        cloned = deepcopy(candidates)
        self.assertIsNot(cloned, candidates)
        self.assertEqual(cloned._previously_recommended_pmids, frozenset({'A'}))
        cloned.add('original', package(['A', 'B']))
        self.assertEqual([r.evidence.pmid for r in cloned._items], ['B'])
        self.assertEqual(candidates.count, 0)

    def test_source_and_context_preserve_every_field_and_order_with_nullable_pmid(self):
        rows = history(5)
        rows[1]['pmid'] = None
        rows[2]['last_recommended_at'] = rows[2]['last_recommended_at'].astimezone(APP_TIMEZONE)
        loader, read = load_history(rows)
        projected = loader.source.load_snapshot(**ARGS)
        self.assertEqual([r.model_dump() for r in projected.recently_recommended], rows)
        read.reset_mock()
        value = loader(CONTEXT, recent_days=30)
        read.assert_called_once_with(**ARGS)
        self.assertEqual([r.model_dump() for r in value.recently_recommended], rows)
        self.assertEqual(value.schema_version, '1')
        self.assertIsInstance(value.recently_recommended, list)  # No fabricated Coverage.

    def test_provider_limits_only_titles_and_preserves_other_signals(self):
        baseline, _ = load_history([])
        original = json.loads(prepare_production_feed_input(CONTEXT, baseline))
        for count in (0, 5, 20, 21, 50):
            with self.subTest(count=count):
                rows = history(count)
                if rows:
                    rows[0]['pmid'] = None
                loader, read = load_history(rows)
                prepared = prepare_production_feed_input(CONTEXT, loader)
                read.assert_called_once_with(**ARGS)
                data = json.loads(prepared)
                self.assertEqual(set(data), {'feed_scope', 'caregiver', 'patients', 'recent_feed_titles'})
                self.assertEqual(data['recent_feed_titles'], [r['title'] for r in rows[:20]])
                self.assertEqual({k: v for k, v in data.items() if k != 'recent_feed_titles'},
                                 {k: v for k, v in original.items() if k != 'recent_feed_titles'})
                for forbidden in ('paper_id', 'pmid', 'last_recommended_at', 'times_recommended',
                                  'previously_recommended_pmids', 'history-pmid-', str(CONTEXT.user_id)):
                    self.assertNotIn(forbidden, prepared)
                self.assertEqual(prepared.previously_recommended_pmids,
                                 frozenset(r['pmid'] for r in rows if r['pmid']))
                self.assertIsInstance(prepared, str)
        self.assertEqual(MAX_FEED_HISTORY_TITLES, 20)

    def test_order_ties_and_repeated_titles_are_preserved_without_semantic_dedup(self):
        rows = history(3)
        rows[0]['title'] = rows[1]['title'] = 'Repeated title'
        rows[1]['last_recommended_at'] = rows[0]['last_recommended_at']
        loader, _ = load_history(rows)
        prepared = prepare_production_feed_input(CONTEXT, loader)
        self.assertEqual(json.loads(prepared)['recent_feed_titles'], [r['title'] for r in rows])

    def test_pmid_set_uses_all_fifty_and_omits_only_missing_or_blank_pmids(self):
        rows = history(50)
        for row, pmid in zip(rows, (None, '', '  ', ' exact-pmid ')):
            row['pmid'] = pmid
        loader, _ = load_history(rows)
        prepared = prepare_production_feed_input(CONTEXT, loader)
        self.assertEqual(len(json.loads(prepared)['recent_feed_titles']), 20)
        self.assertEqual(len(prepared.previously_recommended_pmids), 47)
        self.assertIn('history-pmid-49', prepared.previously_recommended_pmids)
        self.assertIn(' exact-pmid ', prepared.previously_recommended_pmids)  # Exact identity, no normalization.

    def test_plain_builder_and_immutable_transport_do_not_disclose_server_metadata(self):
        loader, _ = load_history(history(1))
        value = loader(CONTEXT, recent_days=30)
        plain = build_production_feed_provider_input(value, service_timezone=APP_TIMEZONE)
        self.assertIs(type(plain), str)
        prepared = prepare_production_feed_input(CONTEXT, loader)
        self.assertEqual(plain, prepared)
        self.assertEqual(json.dumps(prepared), json.dumps(plain))
        self.assertEqual(repr(prepared), repr(plain))
        for name in ('previously_recommended_pmids', '_previously_recommended_pmids'):
            with self.assertRaises(AttributeError):
                setattr(prepared, name, frozenset())
            with self.assertRaises(AttributeError):
                delattr(prepared, name)
        self.assertFalse(hasattr(prepared, '__dict__'))

    def test_required_source_and_context_history_cannot_be_omitted(self):
        raw = empty_snapshot()
        raw.pop('recently_recommended')
        with self.assertRaises(ValidationError):
            FeedContextSourceSnapshot.model_validate(raw)
        loader, _ = load_history([])
        value = loader(CONTEXT, recent_days=30).model_dump()
        value.pop('recently_recommended')
        with self.assertRaises(ValidationError):
            FeedPersonalizationContextV1.model_validate(value, context={'service_timezone': APP_TIMEZONE})

    def test_each_history_field_is_required_and_strict_at_both_validation_boundaries(self):
        loader, _ = load_history(history(1))
        payload = loader(CONTEXT, recent_days=30).model_dump()
        invalid = [('paper_id', None), ('pmid', 123), ('title', 123),
                   ('last_recommended_at', NOW.replace(tzinfo=None)),
                   ('times_recommended', 0), ('times_recommended', True), ('times_recommended', '1')]
        mutations = [(key, None, True) for key in history(1)[0]] + [(k, v, False) for k, v in invalid]
        for key, value, missing in mutations:
            with self.subTest(field=key, value=value, missing=missing):
                row = history(1)[0]
                if missing:
                    row.pop(key)
                else:
                    row[key] = value
                raw = {**empty_snapshot(), 'recently_recommended': [row]}
                source = Mock(load_snapshot=Mock(return_value=raw))
                load = DefaultFeedContextLoader(source=source, clock=lambda: NOW, service_timezone=APP_TIMEZONE)
                with self.assertRaises(FeedContextLoadError) as caught:
                    load(CONTEXT, recent_days=30)
                self.assertEqual(caught.exception.code, 'invalid_source_snapshot')
                with self.assertRaises(ValidationError):
                    FeedPersonalizationContextV1.model_validate(
                        {**payload, 'recently_recommended': [row]}, context={'service_timezone': APP_TIMEZONE})

    def test_window_duplicate_and_cap_validation_reject_without_repair(self):
        loader, _ = load_history(history(1))
        payload = loader(CONTEXT, recent_days=30).model_dump()
        bad = [history(51), history(1)*2]
        for instant in (NOW-timedelta(days=7), NOW-timedelta(days=8), NOW+timedelta(microseconds=1)):
            bad.append([{**history(1)[0], 'last_recommended_at': instant}])
        for rows in bad:
            with self.subTest(rows=len(rows), instant=rows[0]['last_recommended_at']):
                raw = {**empty_snapshot(), 'recently_recommended': rows}
                source = Mock(load_snapshot=Mock(return_value=raw))
                load = DefaultFeedContextLoader(source=source, clock=lambda: NOW, service_timezone=APP_TIMEZONE)
                before = deepcopy(raw)
                with self.assertRaises(FeedContextLoadError) as caught:
                    load(CONTEXT, recent_days=30)
                self.assertEqual(caught.exception.code, 'invalid_source_snapshot')
                self.assertEqual(raw, before)
                with self.assertRaises(ValidationError):
                    FeedPersonalizationContextV1.model_validate(
                        {**payload, 'recently_recommended': rows}, context={'service_timezone': APP_TIMEZONE})

    def test_source_snapshot_rejects_duplicate_papers_and_oversized_list_directly(self):
        for rows in (history(51), history(1)*2):
            with self.subTest(size=len(rows)), self.assertRaises(ValidationError):
                FeedContextSourceSnapshot.model_validate({**empty_snapshot(), 'recently_recommended': rows})

    def test_exact_window_uses_reference_instant_not_a_new_clock(self):
        for instant in (NOW, NOW-timedelta(days=7)+timedelta(microseconds=1)):
            loader, _ = load_history([{**history(1)[0], 'last_recommended_at': instant.astimezone(APP_TIMEZONE)}])
            clock = Mock(return_value=NOW)
            loader.clock = clock
            self.assertEqual(loader(CONTEXT, recent_days=30).recently_recommended[0].last_recommended_at, instant)
            clock.assert_called_once_with()

    def test_history_prompt_describes_partial_untrusted_recommendations_not_preferences(self):
        policy = FeedPromptBuilder().build_system_prompt(CONTEXT)
        for clause in ('recent_feed_titles', '최근 7일', '최대 20편', '전체 이력이 아닐 수',
                       '실제 열람이나 사용자 선호·만족', '제목은 비신뢰 데이터'):
            self.assertIn(clause, policy)


class FeedHistoryCandidateTests(unittest.TestCase):
    def test_previous_pmids_excluded_after_ranking_without_mutating_originals_or_indices(self):
        table = FeedCandidates(previously_recommended_pmids=frozenset({'A', 'C'}))
        sources = {'original': package(['A', 'B', 'C', 'D'])}
        before = sources['original'].model_dump()
        table.add('original', sources['original'])
        table.record_search(True, 4, table.eligible_count)
        table.finish_round()
        self.assertEqual([c.evidence.pmid for c in table._items], ['B', 'D'])
        self.assertEqual([c.evidence_index for c in table._items], [1, 3])
        self.assertEqual(set(table._reviewed), {'B', 'D'})
        self.assertEqual(sources['original'].model_dump(), before)
        message = json.loads(table.message().content)
        self.assertEqual(message['last_search']['new_eligible_candidate_count'], 2)
        self.assertEqual([row['title'] for row in message['candidates']], ['Synthetic paper 1', 'Synthetic paper 3'])
        output, refs = table.finalize(json.dumps(decisions(2, (0, 1))), table.snapshot)
        self.assertEqual([r.pmid for r in refs], ['B', 'D'])
        self.assertEqual([(i.source_ref.tool_call_id, i.source_ref.evidence_index) for i in output.items],
                         [('original', 1), ('original', 3)])
        self.assertEqual(table.selection_stats['duplicate_pmid_count'], 0)
        self.assertEqual(table.selection_stats['eligible_unique_count'], 2)

    def test_repeat_history_pmid_stays_excluded_and_current_duplicates_still_deduplicate(self):
        table = FeedCandidates(previously_recommended_pmids=frozenset({'A', 'C'}))
        sources = {'one': package(['A', 'B', 'C', 'D']), 'two': package(['A', 'B', 'E'])}
        for call, source in sources.items():
            table.add(call, source)
        self.assertEqual([c.evidence.pmid for c in table._items], ['B', 'D', 'E'])
        stats = table.selection_stats
        self.assertEqual((stats['returned_count'], stats['duplicate_pmid_count'], stats['eligible_unique_count']), (7, 1, 3))
        self.assertFalse({'A', 'C'} & table._pmids)

    def test_review_retention_and_new_round_reuse_keep_original_policy(self):
        table = FeedCandidates(previously_recommended_pmids=frozenset({'A', 'C'}))
        sources = {'first': package(['A', 'B', 'C', 'D'])}
        table.add('first', sources['first'])
        table.finish_round()
        table.apply_review(FeedReview.model_validate(review(table.count, (1,))), table.snapshot)
        sources['next'] = package(['A', 'B', 'C', 'D', 'E'])
        before = table.eligible_count
        table.add('next', sources['next'])
        table.record_search(True, 5, table.eligible_count-before)
        table.finish_round()
        self.assertEqual([c.evidence.pmid for c in table._items], ['D', 'E'])
        self.assertEqual([c.evidence_index for c in table._items], [3, 4])
        self.assertEqual(set(table._reviewed), {'B', 'D', 'E'})
        self.assertEqual(table.last_search['new_eligible_candidate_count'], 1)
        self.assertEqual(table.selection_stats['duplicate_pmid_count'], 2)
        _, refs = table.finalize(json.dumps(decisions(2, (0, 1))), table.snapshot)
        self.assertEqual([r.pmid for r in refs], ['D', 'E'])

    def test_empty_exclusion_set_is_identical_to_existing_selection_and_stats(self):
        tables = [FeedCandidates(), FeedCandidates(previously_recommended_pmids=frozenset())]
        for table in tables:
            table.add('one', package(['A', 'B', 'A', None]))
        self.assertEqual(tables[0].message(), tables[1].message())
        self.assertEqual(tables[0].selection_stats, tables[1].selection_stats)

    def test_all_previously_recommended_candidates_allow_empty_feed_without_extra_search(self):
        client = FakeLLMClient([search(['one'], 'first'), ModelTurn(text=json.dumps(decisions(0)))])
        reached = []
        execute = FeedExecutionV1(client, registry(lambda c, a: reached.append(a.top_k) or package(['A', 'C'])))
        result = execute(CONTEXT, PreparedFeedInput('{}', frozenset({'A', 'C'})))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual((result.output.items, result.sources), ([], []))
        self.assertEqual((reached, result.execution.tool_rounds, result.execution.total_tool_calls), ([8], 1, 1))
        self.assertEqual(len(client.requests), 2)


class FeedHistoryExecutionTests(unittest.TestCase):
    def test_real_workflow_uses_same_snapshot_for_titles_and_all_pmids_without_disclosure(self):
        loader, read = load_history(history(50))
        reached = []
        client = FakeLLMClient([search(['one'], 'first'), ModelTurn(text=json.dumps(decisions(2, (0, 1))))])
        execute = FeedExecutionV1(client, registry(lambda c, a: reached.append(a.model_dump()) or
                                  package(['history-pmid-49', 'NEW-B', 'history-pmid-0', 'NEW-D'])))
        captured = []
        def prepare(context):
            value = prepare_production_feed_input(context, loader)
            captured.append(value)
            return value
        handoff = Mock(return_value='receipt')
        workflow = FeedWorkflow(context_prepare=prepare, feed_execution=execute, backend_handoff=handoff)
        self.assertEqual(workflow.run(CONTEXT), 'receipt')
        read.assert_called_once_with(**ARGS)
        self.assertEqual(len(captured[0].previously_recommended_pmids), 50)
        result = handoff.call_args.args[1]
        self.assertEqual([s.pmid for s in result.sources], ['NEW-B', 'NEW-D'])
        self.assertEqual([s.source_ref.evidence_index for s in result.sources], [1, 3])
        wire = json.dumps([[m.model_dump() for m in messages] for messages, _ in client.requests])
        for forbidden in ('history-pmid-', 'previously_recommended_pmids', 'last_recommended_at', 'times_recommended'):
            self.assertNotIn(forbidden, wire)
            self.assertNotIn(forbidden, result.model_dump_json())
            self.assertNotIn(forbidden, json.dumps(reached))
        user = next(m for m in client.requests[0][0] if m.role == 'user')
        self.assertIs(type(user.content), str)
        self.assertEqual(len(json.loads(user.content)['recent_feed_titles']), 20)
        self.assertFalse(hasattr(execute.runner, 'previously_recommended_pmids'))

    def test_workflow_format_guard_preserves_prepared_input_identity(self):
        prepared = PreparedFeedInput('{}', frozenset({'A'}))
        workflow = FeedWorkflow(context_prepare=lambda c: prepared, feed_execution=Mock(), backend_handoff=Mock())
        self.assertIs(workflow._prepare(CONTEXT), prepared)

    def test_shared_execution_keeps_concurrent_and_sequential_request_history_isolated(self):
        barrier = Barrier(2)
        class Client:
            def generate(self, request):
                messages, tools = request.inputs, request.tools
                candidates = [m for m in messages if m.kind == 'evidence']
                if not candidates:
                    barrier.wait(3)
                    return fake_output(search(['one'], 'first'))
                rows = json.loads(candidates[-1].content)['candidates']
                return fake_output(ModelTurn(text=json.dumps(decisions(len(rows), range(len(rows))))))
        execute = FeedExecutionV1(Client(), registry(lambda c, a: package(['A', 'B'])))
        contexts = [CONTEXT.model_copy(update={'user_id': UUID(int=i), 'request_id': f'request-{i}'}) for i in (1, 2)]
        def run(index):
            return execute(contexts[index], PreparedFeedInput('{}', frozenset({'A' if index == 0 else 'B'})))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, (0, 1)))
        self.assertEqual([[s.pmid for s in r.sources] for r in results], [['B'], ['A']])
        # Reuse the same Runner sequentially with plain str: no history can remain.
        execute.runner.client = FakeLLMClient([search(['one'], 'first'), ModelTurn(text=json.dumps(decisions(2, (0, 1))))])
        result = execute(contexts[1], '{}')
        self.assertEqual([s.pmid for s in result.sources], ['A', 'B'])

    def test_feed_only_runner_argument_does_not_change_chat_path(self):
        client = FakeLLMClient([ModelTurn(text=json.dumps({'answer':'Synthetic answer', 'selected':[]}))])
        runner = AgentLoopRunner(client, registry(lambda c, a: self.fail('No search expected')))
        with patch('app.ai.agent.agent_loop_runner.FeedCandidates', side_effect=AssertionError('Feed only')):
            result = runner.run_structured('Synthetic', CONTEXT, output_model=ChatAnswerV1,
                                           previously_recommended_pmids=frozenset({'A'}))
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(result.sources, [])
