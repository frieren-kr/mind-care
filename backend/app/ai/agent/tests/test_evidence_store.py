"""Source authority, positional judgments and request isolation; offline only."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import json
import random
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from ..agent_loop_runner import AgentLoopRunner
from ..evidence import EvidenceKey, EvidenceStore
from ..feed_candidates import FeedCandidates, FeedDecisionError, FeedReview
from ..outputs import ChatAnswerV1, ChatDraft
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..schemas import ModelTurn, ToolCall
from ..testing.chat_workflow_fakes import chat_final
from ..testing.fake_llm import FakeLLMClient
from .test_feed_candidates import decisions, package
from .test_workflows import CONTEXT


class EvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'psycopg.connect', 'app.ai.retrieval.embedding.get_model'):
            guard = patch(target, side_effect=AssertionError('Live I/O forbidden'))
            guard.start(); self.addCleanup(guard.stop)

    def test_registration_validates_entire_package_before_any_change(self):
        store = EvidenceStore()
        data = package(['A', 'B']); data.evidence[1].relevance_score = 2
        with self.assertRaises(ValidationError):
            store.add_search_result('call', data)
        self.assertEqual(store.call_ids, set())
        self.assertEqual(store.current('chat').keys, ())

    def test_original_and_returned_objects_cannot_mutate_authority(self):
        store = EvidenceStore(); data = package(['A'])
        candidates = store.add_search_result('source', data, provider_call_id='actual-provider-call')
        data.evidence[0].title = 'original input changed'
        item = candidates[0].evidence; item.title = 'returned item changed'
        packages = store.packages(); packages[0].evidence.clear()
        self.assertEqual(candidates[0].evidence.title, 'Synthetic paper 0')
        self.assertEqual(store.provider_call_id(candidates[0].key), 'actual-provider-call')
        self.assertIs(deepcopy(store), store)
        with self.assertRaises(ValueError):
            store.add_search_result('source', data)

    def test_snapshots_reject_stale_foreign_and_forged_replacements(self):
        store, other = EvidenceStore(), EvidenceStore()
        first = store.current('chat')
        store.add_search_result('source', package(['A']))
        current = store.current('chat')
        for invalid in (first, other.current('chat'), replace(current, keys=()), None):
            with self.subTest(snapshot=invalid), self.assertRaises(ValueError):
                store.select(invalid, [])
        with self.assertRaises(FrozenInstanceError):
            current.version = 999
        self.assertIs(deepcopy(current), current)
        store.add_search_result('empty', package([]))
        self.assertEqual(store.current('chat').keys, current.keys)
        with self.assertRaises(ValueError):
            store.finalize_chat(chat_final(selected=[True]).text, current)
        feed_snapshot = store.snapshot('feed', current.keys)
        with self.assertRaises(ValueError):
            store.finalize_chat(chat_final(selected=[True]).text, feed_snapshot)

    def test_invalid_refs_and_duplicate_occurrences_are_rejected_not_repaired(self):
        store = EvidenceStore(); store.add_search_result('source', package(['A']))
        key = EvidenceKey('source', 0)
        for keys in ((key, key), (EvidenceKey('missing', 0),), (EvidenceKey('source', 1),),
                     (EvidenceKey('source', -1),), (EvidenceKey('source', True),)):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                store.snapshot('selection', keys)
        with self.assertRaises(ValueError):
            store.resolve([key.reference(), key.reference()])

    def test_chat_preserves_duplicate_occurrences_and_sources_without_pmid(self):
        store = EvidenceStore()
        first = package(['same', None]); first.evidence[1].evidence_type = 'guideline'
        first.evidence[1].organization = 'Original guideline'
        second = package(['same']); second.evidence[0].title = 'Different occurrence'
        store.add_search_result('first', first); store.add_search_result('second', second)
        snapshot = store.current('chat')
        output, sources = store.finalize_chat(chat_final(selected=[True,True,True]).text, snapshot)
        self.assertEqual([(r.tool_call_id,r.evidence_index) for r in output.citation_refs], [('first',0),('first',1),('second',0)])
        self.assertEqual([s.pmid for s in sources], ['same',None,'same'])
        self.assertEqual(sources[-1].title, 'Different occurrence')
        self.assertEqual(sources[1].organization, 'Original guideline')

    def test_chat_draft_rejects_original_ids_metadata_and_bad_selection(self):
        store = EvidenceStore(); store.add_search_result('source', package(['A','B']))
        snapshot = store.current('chat')
        for mask in ([], [True], [True,False,True], [1,False], ['true',False], [None,False]):
            with self.subTest(mask=mask), self.assertRaises(ValueError):
                store.finalize_chat(json.dumps({'answer':'Research claim', 'selected':mask}), snapshot)
        for key in ('pmid','doi','source_ref','citation_refs','tool_call_id','evidence_index','candidate_revision'):
            with self.subTest(field=key), self.assertRaises(ValueError):
                store.finalize_chat(json.dumps({'answer':'Research claim','selected':[True,False],key:'invented'}), snapshot)
        self.assertEqual(set(ChatDraft.model_fields), {'answer','selected'})
        self.assertFalse(ChatDraft.model_json_schema()['additionalProperties'])

    def test_no_search_empty_search_and_uncited_answers(self):
        for search in (False, True):
            store = EvidenceStore()
            if search: store.add_search_result('empty', package([]))
            output, sources = store.finalize_chat(chat_final('일반 설명').text, store.current('chat'))
            self.assertEqual(output.citation_refs, []); self.assertEqual(sources, [])
        store = EvidenceStore(); store.add_search_result('source', package(['A']))
        output, sources = store.finalize_chat(chat_final('일반 설명', selected=[False]).text, store.current('chat'))
        self.assertEqual(output.answer, '일반 설명'); self.assertEqual(sources, [])

    def test_invalid_json_and_excessive_citations_fail_without_answer_fallback(self):
        store = EvidenceStore(); store.add_search_result('source', package([str(i) for i in range(21)]))
        snapshot = store.current('chat')
        for text in ('not JSON', '{"answer":"a","selected":[],"selected":[]}',
                     '{"answer":"a","selected":NaN}', 'x'*65537,
                     chat_final('Research claim', selected=[True]*21).text):
            with self.subTest(text=text[:60]), self.assertRaises(ValueError):
                store.finalize_chat(text, snapshot)

    def test_feed_staging_shares_authority_but_isolates_selection_state(self):
        store = EvidenceStore(); table = FeedCandidates(store)
        table.add('first', package(['A','B']), provider_call_id='provider')
        table.finish_round(); original = table.snapshot
        staged = deepcopy(table)
        self.assertIs(staged.store, store)
        staged.apply_review(FeedReview.model_validate(decisions(2,(1,))), original)
        self.assertEqual([c.evidence.pmid for c in table._items], ['A','B'])
        self.assertEqual([c.evidence.pmid for c in staged._retained], ['B'])
        with self.assertRaises(FeedDecisionError):
            table.finalize(json.dumps(decisions(2)), original)
        staged.add('next', package(['C']))
        staged.finish_round()
        output, sources = staged.finalize(json.dumps(decisions(2,(0,1))), staged.snapshot)
        self.assertEqual([s.pmid for s in sources], ['B','C'])
        self.assertEqual(output.items[0].source_ref.tool_call_id, 'first')
        self.assertEqual(store.provider_call_id(staged._items[0].key), 'provider')

    def test_100_deterministic_runner_requests_with_arbitrary_provider_ids(self):
        rng = random.Random(20261030)
        for iteration in range(100):
            ids = [f'{iteration}:{i}:' + ''.join(rng.choices('Ab9_-:/한글☃',k=35)) for i in range(2)]
            data = [package(['same',None]), package(['same','other'])]
            data[0].evidence[1].evidence_type='guideline'
            data[0].evidence[0].doi=f'original-doi-{iteration}'
            data[1].evidence[0].doi=f'later-doi-{iteration}'
            tools=ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE],lambda c,a:data[int(a.query)]),))
            client=FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id=ids[i],name='search_evidence',arguments={'query':str(i),'top_k':2*(i+1)}) for i in range(2)]),
                                 chat_final('정상 설명',selected=[True,True,False])])
            runner=AgentLoopRunner(client,tools)
            result=runner.run_structured('Synthetic',CONTEXT,output_model=ChatAnswerV1)
            self.assertEqual(result.execution.status,'completed')
            self.assertEqual([(s.source_ref.tool_call_id,s.source_ref.evidence_index) for s in result.sources],[(ids[0],0),(ids[0],1)])
            self.assertIsNone(result.sources[1].pmid)
            self.assertEqual(result.sources[0].doi,f'original-doi-{iteration}')
            client.turns.append(chat_final())
            self.assertEqual(runner.run_structured('New request',CONTEXT,output_model=ChatAnswerV1).sources,[])

    def test_parallel_source_stores_and_snapshots_never_cross_requests(self):
        def run(owner):
            store=EvidenceStore(); data=package([str(owner)]);store.add_search_result('same-call',data)
            snapshot=store.current('chat')
            return store,snapshot,store.finalize_chat(chat_final(selected=[True]).text,snapshot)[1][0]
        with ThreadPoolExecutor(4) as pool: results=list(pool.map(run, range(12)))
        self.assertEqual([row[2].pmid for row in results], [str(i) for i in range(12)])
        for a,b in zip(results,results[1:]):
            with self.assertRaises(ValueError):a[0].select(b[1],[True])

    def test_future_standard_evidence_contract_reuses_source_path_without_name_branch(self):
        from ..tools.contracts import EvidencePackage
        declared = replace(TOOL_CONTRACTS[ToolName.PATIENT_PROFILE], output_model=EvidencePackage)
        with patch.dict(TOOL_CONTRACTS, {declared.name:declared}):
            registry = ToolRegistry((ToolSpec(declared, lambda c,a:package(['source'])),))
            client = FakeLLMClient([ModelTurn(tool_calls=[ToolCall(id='future', name=declared.name.value, arguments={})]),
                                    chat_final(selected=[True])])
            result = AgentLoopRunner(client,registry).run_structured('Synthetic',CONTEXT,output_model=ChatAnswerV1)
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(result.sources[0].source_ref.tool_call_id,'future')
        outcome = next(m for m in client.requests[1][0] if m.type=='function_call_output')
        self.assertEqual(json.loads(outcome.content)['data']['returned_evidence_count'],1)
        self.assertNotIn('pmid',outcome.content)

    def test_structured_demo_uses_internal_drafts_and_server_built_public_results(self):
        from ..testing.structured_demo import main
        with patch('builtins.print') as printed:
            main()
        results = [json.loads(call.args[0]) for call in printed.call_args_list]
        self.assertEqual([r['execution']['status'] for r in results], ['completed','completed'])
        self.assertEqual([r['output']['response_type'] for r in results], ['chat','feed'])
        self.assertTrue(all(r['sources'][0]['pmid']=='FAKE-PMID' for r in results))
