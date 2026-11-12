"""History selection with serialized-payload budgets; no DB or Provider."""
from datetime import date, datetime, timedelta, timezone
import json
from unittest import TestCase
from unittest.mock import Mock, patch

from pydantic import ValidationError

from ..workflows.chat_history_context import (ChatHistorySnapshot, ChatHistorySource, CompletedChatTurn, DailyChatSummary,
    build_chat_history, serialize_chat_data, utf8_token_upper_bound)
from .test_workflows import CONTEXT
from ..workflows.chat_history_context import chat_history_token_counter

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def turn(hours=1, question='Stored question?', answer='Stored answer.'):
    at=NOW-timedelta(hours=hours)
    return CompletedChatTurn(user_message=question, assistant_message=answer, completed_at=at, started_at=at)


def snapshot(turns=(), summaries=(), context=CONTEXT):
    return ChatHistorySnapshot(user_id=context.user_id, patient_id=context.patient_id,
        recent_turns=list(turns), daily_summaries=list(summaries))


def build(data, **kwargs):
    # These boundary cases explicitly exercise the conservative fallback.
    kwargs.setdefault('count_tokens',utf8_token_upper_bound)
    return build_chat_history(data, user_id=CONTEXT.user_id, patient_id=CONTEXT.patient_id, reference_time=NOW, **kwargs)


def envelope(history):
    return serialize_chat_data({'previous_chat_context':history})


def history_source(number=1, **updates):
    return dict(citation_number=number, title='Stored study', pmid='12345678',
                evidence_type='new_research') | updates


class ChatHistorySourceTests(TestCase):
    def test_source_contract_and_empty_turn_default(self):
        self.assertEqual(set(ChatHistorySource.model_fields),
                         {'citation_number', 'title', 'pmid', 'evidence_type'})
        first, second = turn(), turn(2)
        first.sources.append(ChatHistorySource(**history_source()))
        self.assertEqual(second.sources, [])
        self.assertNotIn('sources', build(snapshot([second]))['recent_turns'][0])
        for kind in ('new_research', 'guideline'):
            self.assertIsNone(ChatHistorySource(**history_source(pmid=None, evidence_type=kind)).pmid)

    def test_invalid_source_fields_are_rejected(self):
        invalid = [{'citation_number': v} for v in (0, -1, '2', True, 1.5)]
        invalid += [{'title': v} for v in ('', '  ', 'x'*1001, 123)]
        invalid += [{'pmid': v} for v in ('', ' ', ' 123', '123 ', 123)]
        invalid += [{'evidence_type': 'other'}, {'evidence_type': 'guideline'},
                    {'source': 'Journal'}, {'publication_year': 2025}]
        for updates in invalid:
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                ChatHistorySource(**history_source(**updates))

    def test_sources_remain_attached_to_turn_with_original_nonconsecutive_numbers(self):
        sources = [history_source(3), history_source(5, title='Guide', pmid=None, evidence_type='guideline')]
        raw = turn().model_copy(update={'sources': sources})
        rows = build(snapshot([raw, turn(2), turn(3).model_copy(update={'sources': sources})]))['recent_turns']
        self.assertEqual(rows[0]['representation'], 'verbatim_excerpt')
        self.assertEqual(rows[0]['sources'], sources)
        self.assertNotIn('sources', rows[1])
        self.assertEqual(rows[2]['sources'], sources)

    def test_duplicate_descending_and_oversized_source_lists_are_rejected(self):
        for numbers in ([1, 1], [2, 1], list(range(1, 7))):
            with self.subTest(numbers=numbers), self.assertRaises(ValidationError):
                CompletedChatTurn(**(turn().model_dump() | {'sources': [history_source(n) for n in numbers]}))

    def test_source_overflow_keeps_raw_turns_and_existing_older_selection(self):
        turns = [turn(), turn(2), turn(3)]
        expected = build(snapshot(turns))
        turns[0] = turns[0].model_copy(update={'sources': [history_source(n, title='논'*1000) for n in (1, 2)]})
        self.assertEqual(build(snapshot(turns)), expected)
        too_large = turn(answer='x'*5500).model_copy(update={'sources': [history_source()]})
        self.assertEqual(build(snapshot([too_large, turn(2)]))['recent_turns'], [])

    def test_full_source_json_counts_toward_exact_budget_and_summary_budget_is_preserved(self):
        raw = turn(answer='x').model_copy(update={'sources': [history_source(title='한글 "title"\n')]})
        overhead = utf8_token_upper_bound(envelope(build(snapshot([raw])))) - 1
        fitting = raw.model_copy(update={'assistant_message': 'x'*(5500-overhead)})
        history = build(snapshot([fitting]))
        self.assertEqual(utf8_token_upper_bound(envelope(history)), 5500)
        self.assertEqual(history['recent_turns'][0]['sources'], [history_source(title='한글 "title"\n')])
        overflow = fitting.model_copy(update={'assistant_message': fitting.assistant_message+'x'})
        history = build(snapshot([overflow]))
        self.assertEqual(history['recent_turns'][0]['messages'][1]['content'], overflow.assistant_message)
        self.assertNotIn('sources', history['recent_turns'][0])
        summary = DailyChatSummary(summary_date=date(2026, 11, 5), summary='Stored day.')
        combined = build(snapshot([fitting], [summary]))
        self.assertEqual(combined['daily_summaries'][0]['summary'], summary.summary)
        self.assertLessEqual(utf8_token_upper_bound(envelope(combined)), 7000)

    def test_retained_sources_are_removed_before_dropping_an_older_raw_turn(self):
        newest = turn(1, answer='a'*2200).model_copy(update={'sources':[history_source(title='T'*1000)]})
        older = turn(2, answer='b'*2200)
        expected = build(snapshot([turn(1, answer='a'*2200), older]))
        self.assertEqual(len(expected['recent_turns']), 2)
        for sources in ([], [history_source()]):
            with self.subTest(older_sources=sources):
                data = snapshot([newest, older.model_copy(update={'sources':sources})])
                original = data.model_dump()
                result = build(data)
                self.assertEqual(result, expected)
                self.assertLessEqual(utf8_token_upper_bound(envelope(result)), 5500)
                self.assertEqual(data.model_dump(), original)

    def test_raw_overflow_still_stops_and_preserves_previously_selected_sources(self):
        newest = turn(1, answer='a'*2200).model_copy(update={'sources':[history_source(title='T'*1000)]})
        older = turn(2, answer='b'*3000)
        expected = build(snapshot([newest]))
        self.assertIn('sources', expected['recent_turns'][0])
        raw_rows = [build(snapshot([raw]))['recent_turns'][0] for raw in (older, turn(1, answer='a'*2200))]
        self.assertGreater(utf8_token_upper_bound(envelope({'recent_turns':raw_rows, 'daily_summaries':[]})), 5500)
        result = build(snapshot([newest, older, turn(3)]))
        self.assertEqual(result, expected)
        self.assertLessEqual(utf8_token_upper_bound(envelope(result)), 5500)

    def test_source_mutations_and_scope_mismatch_still_fail_validation(self):
        raw = turn().model_copy(update={'sources': [ChatHistorySource(**history_source()).model_copy(update={'title': ' '})]})
        with self.assertRaises(ValidationError):
            build(snapshot([raw]))
        data = snapshot([turn().model_copy(update={'sources': [history_source()]})])
        with self.assertRaises(ValueError):
            build(data.model_copy(update={'patient_id': None}))


class ChatHistoryContextTests(TestCase):
    def test_empty_history_and_no_summaries_are_normal(self):
        self.assertEqual(build(snapshot()), {'recent_turns':[], 'daily_summaries':[]})

    def test_newest_two_remain_exact_and_all_selected_turns_are_chronological(self):
        turns=[turn(i, f'  Original {i}?\n', f'  Exact answer {i}.\n') for i in range(1,6)]
        value=build(snapshot([turns[2],turns[0],turns[4],turns[1],turns[3]]))
        self.assertEqual([r['completed_at'] for r in value['recent_turns']], [t.completed_at.isoformat() for t in reversed(turns)])
        for row,original in zip(value['recent_turns'][-2:],reversed(turns[:2]),strict=True):
            self.assertEqual(row['representation'],'original')
            self.assertEqual([m['content'] for m in row['messages']],[original.user_message,original.assistant_message])

    def test_older_turns_extract_whole_original_paragraphs_without_rephrasing(self):
        source='First complete paragraph.\n\n'+'Long paragraph '*80+'.'
        value=build(snapshot([turn(1),turn(2),turn(3,answer=source)]))
        oldest=value['recent_turns'][0]
        self.assertEqual(oldest['representation'],'verbatim_excerpt')
        self.assertEqual(oldest['messages'][1]['content'],'First complete paragraph.')
        self.assertTrue(source.startswith(oldest['messages'][1]['content']))

    def test_unsafe_long_paragraph_omits_only_that_turn_without_slicing_it(self):
        value=build(snapshot([turn(1),turn(2),turn(3,answer='Incomplete '*90),turn(4)]))
        self.assertEqual(len(value['recent_turns']),3)
        self.assertEqual(value['recent_turns'][0]['completed_at'],turn(4).completed_at.isoformat())
        self.assertFalse(any('Incomplete' in m['content'] for row in value['recent_turns'] for m in row['messages']))

    def test_exact_recent_serialized_budget_and_first_overflow_stop(self):
        original=turn(answer='x')
        overhead=len(envelope(build(snapshot([original]))).encode())-1
        fitting=turn(answer='x'*(5500-overhead))
        value=build(snapshot([fitting]))
        self.assertEqual(utf8_token_upper_bound(envelope(value)),5500)
        self.assertEqual(value['recent_turns'][0]['messages'][1]['content'],fitting.assistant_message)
        overflow=turn(answer=fitting.assistant_message+'x')
        self.assertEqual(build(snapshot([overflow,turn(2)]))['recent_turns'],[])

    def test_recent_budget_includes_roles_dates_labels_unicode_and_json_escaping(self):
        data=turn(question='한글😀 "line"\n',answer='x')
        value=build(snapshot([data]))
        overhead=len(envelope(value).encode())-1
        fits=turn(question=data.user_message,answer='x'*(5500-overhead))
        value=build(snapshot([fits]))
        self.assertEqual(len(envelope(value).encode()),5500)
        self.assertLess(len((fits.user_message+fits.assistant_message).encode()),5500)
        self.assertEqual(build(snapshot([turn(question=data.user_message,answer=fits.assistant_message+'x')]))['recent_turns'],[])

    def test_summary_budget_includes_entire_json_and_stops_before_older_small_summary(self):
        first=DailyChatSummary(summary_date=date(2026,11,5),summary='x')
        overhead=len(envelope(build(snapshot(summaries=[first]))).encode())-1
        fitting=first.model_copy(update={'summary':'x'*(1500-overhead)})
        value=build(snapshot(summaries=[fitting]))
        self.assertEqual(len(envelope(value).encode()),1500)
        overflow=fitting.model_copy(update={'summary':fitting.summary+'x'})
        older=DailyChatSummary(summary_date=date(2026,11,4),summary='Small summary.')
        self.assertEqual(build(snapshot(summaries=[older,overflow]))['daily_summaries'],[])

    def test_both_budgets_are_independent_and_full_history_is_at_most_7000(self):
        value=build(snapshot([turn(1,answer='R'*5000)],
            [DailyChatSummary(summary_date=date(2026,11,5),summary='S'*1100)]))
        self.assertEqual((len(value['recent_turns']),len(value['daily_summaries'])),(1,1))
        self.assertLessEqual(len(envelope(value).encode()),7000)
        self.assertLessEqual(len(envelope({**value,'daily_summaries':[]}).encode()),5500)
        self.assertLessEqual(len(envelope({**value,'recent_turns':[]}).encode()),1500)

    def test_raw_days_exclude_entire_kst_daily_summary_even_when_raw_is_dropped(self):
        # 11/6 16:00 UTC is 11/7 01:00 KST. Another raw turn covers 11/6 KST.
        at=datetime(2026,11,6,16,tzinfo=timezone.utc)
        raw=turn(answer='x'*6000).model_copy(update={'completed_at':at,'started_at':at})
        summaries=[DailyChatSummary(summary_date=date(2026,11,6),summary='Overlapping day.'),
                   DailyChatSummary(summary_date=date(2026,11,5),summary='Older whole day.')]
        value=build(snapshot([raw,turn(30)],summaries))
        self.assertEqual(value['recent_turns'],[])
        self.assertEqual([s['summary_date'] for s in value['daily_summaries']],['2026-11-05'])

    def test_kst_midnight_overlap_boundary_and_summary_chronology(self):
        at=datetime(2026,11,5,15,tzinfo=timezone.utc)
        raw=turn().model_copy(update={'completed_at':at,'started_at':at})
        summaries=[DailyChatSummary(summary_date=date(2026,11,d),summary=f'Day {d}.') for d in (6,4,5)]
        value=build(snapshot([raw],summaries))
        self.assertEqual([s['summary_date'] for s in value['daily_summaries']],['2026-11-04','2026-11-05'])

    def test_turn_spanning_kst_midnight_excludes_both_days_and_unknown_start_is_conservative(self):
        raw=turn().model_copy(update={'started_at':datetime(2026,11,5,14,59,tzinfo=timezone.utc),
            'completed_at':datetime(2026,11,5,15,1,tzinfo=timezone.utc)})
        summaries=[DailyChatSummary(summary_date=date(2026,11,d),summary='Stored day.') for d in (4,5,6)]
        self.assertEqual([s['summary_date'] for s in build(snapshot([raw],summaries))['daily_summaries']],['2026-11-04'])
        legacy=raw.model_copy(update={'started_at':None})
        value=build(snapshot([legacy],summaries))
        self.assertEqual(len(value['recent_turns']),1)
        self.assertEqual(value['daily_summaries'],[])


    def test_scope_mismatch_and_future_or_duplicate_rows_are_rejected(self):
        from uuid import uuid4
        invalid=[snapshot().model_copy(update={'user_id':uuid4()}),snapshot().model_copy(update={'patient_id':None}),
                 snapshot([turn(0)]),snapshot([turn(-1)]),snapshot([turn(),turn()]),snapshot([turn(i) for i in range(1,7)]),
                 snapshot(summaries=[DailyChatSummary(summary_date=NOW.date(),summary='Incomplete day.')]),
                 snapshot(summaries=[DailyChatSummary(summary_date=date(2026,11,5),summary='x')]*2)]
        for data in invalid:
            with self.subTest(data=data),self.assertRaises(ValueError):build(data)

    def test_strict_message_and_aware_time_contract_revalidates_mutated_instances(self):
        for updates in ({'user_message':123},{'assistant_message':'  '},{'completed_at':NOW.replace(tzinfo=None)}):
            with self.assertRaises(ValidationError):build(snapshot([turn().model_copy(update=updates)]))

    def test_injected_counter_receives_full_serialized_wrappers_and_total_guard(self):
        seen=[]
        def counter(text):
            data=json.loads(text)
            self.assertEqual(set(data),{'previous_chat_context'})
            seen.append(data['previous_chat_context'])
            return len(text.encode())
        build(snapshot([turn()],[DailyChatSummary(summary_date=date(2026,11,5),summary='Stored.')]),count_tokens=counter)
        self.assertTrue(any(v['recent_turns'] and v['daily_summaries'] for v in seen))
        # A model-specific encoding is not additive: enforce the combined bound too.
        def nonlinear(text):
            data=json.loads(text)['previous_chat_context']
            return 7001 if data['recent_turns'] and data['daily_summaries'] else 100
        value=build(snapshot([turn()],[DailyChatSummary(summary_date=date(2026,11,5),summary='Stored.')]),count_tokens=nonlinear)
        self.assertEqual(value['daily_summaries'],[])


class ChatHistoryTokenizerTests(TestCase):
    def test_model_mapping_selects_correct_encoding_and_treats_special_strings_as_data(self):
        for model,name in (('gpt-4o','o200k_base'),('gpt-4','cl100k_base')):
            encoding=Mock()
            encoding.encode_ordinary.return_value=[1,2,3]
            with patch('tiktoken.get_encoding',return_value=encoding) as load:
                counter=chat_history_token_counter(model)
                self.assertEqual(counter('과거 데이터 <|endoftext|>'),3)
            load.assert_called_once_with(name)
            encoding.encode_ordinary.assert_called_once_with('과거 데이터 <|endoftext|>')

    def test_default_uses_backend_execution_model_priority(self):
        from app.config import settings
        for agent,summary,expected in (('gpt-4o','gpt-4','o200k_base'),('','gpt-4','cl100k_base')):
            with patch.object(settings,'openai_agent_model',agent),patch.object(settings,'openai_summary_model',summary), \
                 patch('tiktoken.get_encoding') as load:
                chat_history_token_counter()
            load.assert_called_once_with(expected)

    def test_explicit_model_overrides_configuration(self):
        from app.config import settings
        with patch.object(settings,'openai_agent_model','gpt-4o'),patch('tiktoken.get_encoding') as load:
            chat_history_token_counter('gpt-4')
        load.assert_called_once_with('cl100k_base')

    def test_unsupported_model_falls_back_without_guessing_or_loading_encoding(self):
        with patch('tiktoken.get_encoding') as load,self.assertLogs(level='WARNING') as logs:
            counter=chat_history_token_counter('unsupported-synthetic-model')
        self.assertIs(counter,utf8_token_upper_bound)
        self.assertIn('model_unsupported',' '.join(logs.output))
        load.assert_not_called()

    def test_unconfigured_model_has_a_separate_fallback(self):
        with patch('tiktoken.get_encoding') as load,self.assertLogs(level='WARNING') as logs:
            counter=chat_history_token_counter('')
        self.assertIs(counter,utf8_token_upper_bound)
        self.assertIn('model_unconfigured',' '.join(logs.output))
        load.assert_not_called()

    def test_encoding_load_failure_is_distinct_from_unsupported_model(self):
        with patch('tiktoken.get_encoding',side_effect=OSError('PRIVATE detail')),self.assertLogs(level='WARNING') as logs:
            counter=chat_history_token_counter('gpt-4o')
        self.assertIs(counter,utf8_token_upper_bound)
        self.assertIn('encoding_unavailable',' '.join(logs.output))
        self.assertNotIn('PRIVATE',' '.join(logs.output))

    def test_default_builder_and_stage_one_use_tiktoken_counter(self):
        from ..workflows.chat_context_prepare import prepare_chat_input
        from ..workflows.chat_history_context import ChatHistoryReader
        encoding=Mock()
        seen=[]
        def count(text):
            seen.append(json.loads(text))
            return [1]*100
        encoding.encode_ordinary.side_effect=count
        reader=Mock(spec=ChatHistoryReader);reader.read_history.return_value=snapshot([turn(answer='x'*5600)])
        with patch('tiktoken.get_encoding',return_value=encoding) as load:
            prepared=prepare_chat_input(CONTEXT,'Current question',history_reader=reader,reference_time=NOW,model='gpt-4o')
        load.assert_called_once_with('o200k_base')
        history=json.loads(prepared)['previous_chat_context']
        self.assertEqual(history['recent_turns'][0]['messages'][1]['content'],'x'*5600)
        self.assertTrue(all('previous_chat_context' in data for data in seen))

    def test_injected_counter_still_takes_precedence_over_default_model_lookup(self):
        with patch('tiktoken.get_encoding',side_effect=AssertionError('Explicit counter must win')):
            result=build_chat_history(snapshot([turn()]),user_id=CONTEXT.user_id,patient_id=CONTEXT.patient_id,
                reference_time=NOW,model='gpt-4o',count_tokens=lambda text:100)
        self.assertEqual(len(result['recent_turns']),1)
