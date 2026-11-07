"""Regression tests for candidate priority and completion-order Feed failure."""
import json
import unittest
from threading import Barrier, Event, Thread
from unittest.mock import patch

from ..conversation import Conversation
from ..feed_candidates import FeedCandidates, FeedDecisionError
from ..schemas import ModelTurn
from ..testing.fake_llm import FakeLLMClient
from ..workflows.feed_execution import FeedExecutionV1
from .test_feed_candidates import package, decisions
from .test_parallel_feed import calls, registry
from .test_workflows import CONTEXT


class FeedP2RegressionTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect','psycopg.connect','app.ai.retrieval.embedding.get_model'):
            guard=patch(target,side_effect=AssertionError('Live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def test_followup_preserves_topic_b_top_result_and_exact_original_source(self):
        source={'A':package(['a1','a2','X']),'B':package(['X','b1']),
                'C':package(['c1','c2','c3','c4','c5'])}
        source['A'].evidence[2].title='X original A snapshot'
        source['B'].evidence[0].title='X original B snapshot'
        table=FeedCandidates()
        for call in ('A','B','C'): table.add(call,source[call])
        output,refs=table.finalize(json.dumps(decisions(9,range(5))),table.snapshot)
        self.assertEqual([s.pmid for s in refs],['a1','X','c1','a2','b1'])
        self.assertEqual([(s.source_ref.tool_call_id,s.source_ref.evidence_index) for s in refs],
                         [('A',0),('B',0),('C',0),('A',1),('B',1)])
        self.assertEqual(refs[1].title,'X original B snapshot')
        self.assertEqual([s.source_ref for s in refs],[i.source_ref for i in output.items])
        source['B'].evidence[0].title='mutated'
        _, refs=table.finalize(json.dumps(decisions(9,range(5))),table.snapshot)
        self.assertEqual(refs[1].title,'X original B snapshot')

    def test_all_duplicates_single_and_distinct_pools_keep_unique_bounded_candidates(self):
        for pools,expected in (([['x','x'],['x'],['x','x']],['x']),
                               ([['a','b']],['a','b']),
                               ([['a','b'],['c','d'],['e']],['a','c','e','b','d'])):
            table,source=FeedCandidates(),{}
            for i,rows in enumerate(pools):
                source[str(i)]=package(rows);table.add(str(i),source[str(i)])
            _,refs=table.finalize(json.dumps(decisions(len(expected),range(len(expected)))),table.snapshot)
            self.assertEqual([r.pmid for r in refs],expected)

    def test_later_failure_returns_before_first_slow_call_and_discards_late_state(self):
        barrier,release,finished=Barrier(2),Event(),Event()
        def handler(context,args):
            barrier.wait(2)
            if args.query.endswith('1'): raise RuntimeError('PRIVATE')
            try: release.wait(4)
            finally: finished.set()
            return package(['late'])
        client=FakeLLMClient([ModelTurn(tool_calls=calls(2))])
        execution=FeedExecutionV1(client,registry(handler),tool_timeout_seconds=3,request_timeout_seconds=4)
        box={}
        def run():box['result']=execution(CONTEXT,'{}')
        with patch.object(FeedCandidates,'add',autospec=True) as publish, patch.object(
                Conversation,'append_turn',autospec=True) as followup:
            worker=Thread(target=run,daemon=True);worker.start()
            try:
                worker.join(1)
                self.assertFalse(worker.is_alive(),'Failure must return without releasing the slow peer')
                self.assertFalse(finished.is_set())
                result=box['result']
                self.assertEqual(result.execution.status,'failed')
                self.assertEqual(result.execution.total_tool_calls,2)
                self.assertEqual(result.execution.errors[-1].code,'tool_exception')
                self.assertEqual([t.success for t in result.execution.called_tools],[False])
                self.assertIsNone(result.output)
                self.assertEqual(result.sources,[])
                self.assertEqual(len(client.requests),1)
                snapshot=result.model_dump_json()
                publish.assert_not_called();followup.assert_not_called()
            finally: release.set()
            worker.join(2)
            self.assertTrue(finished.wait(1))
            self.assertEqual(box['result'].model_dump_json(),snapshot)
            publish.assert_not_called();followup.assert_not_called()

    def test_failure_during_slot_admission_skips_unstarted_calls_and_releases_slots(self):
        from threading import BoundedSemaphore
        slots,barrier,release=BoundedSemaphore(2),Barrier(2),Event()
        reached=[]
        def handler(context,args):
            reached.append(args.query)
            barrier.wait(2)
            if args.query.endswith('1'):raise RuntimeError('synthetic failure')
            release.wait(4)
            return package(['late'])
        client=FakeLLMClient([ModelTurn(tool_calls=calls(3))])
        execution=FeedExecutionV1(client,registry(handler),tool_timeout_seconds=3,request_timeout_seconds=4)
        with patch('app.ai.agent._execution._PARALLEL_SLOTS',slots):
            try:
                result=execution(CONTEXT,'{}')
                self.assertEqual(result.execution.status,'failed')
                self.assertEqual(result.execution.total_tool_calls,2)
                self.assertCountEqual(reached,['topic 0','topic 1'])
                self.assertEqual(len(client.requests),1)
                self.assertEqual(result.sources,[])
            finally:release.set()
            acquired=0
            try:
                for _ in range(2):
                    self.assertTrue(slots.acquire(timeout=1),'Parallel slot leaked')
                    acquired+=1
            finally:
                for _ in range(acquired):slots.release()
