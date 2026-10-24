"""No live I/O: deterministic barriers prove overlap/order and late-result isolation."""
import json
import unittest
from contextvars import ContextVar
from threading import Barrier, Event, Lock
from time import monotonic
from unittest.mock import patch

import httpx
from openai import OpenAI

from .._execution import bounded_parallel, request_deadline, RequestTimeout
from ..agent_loop_runner import AgentLoopRunner
from ..clients.openai_client import OpenAIClient
from ..composition import build_evidence_registry
from ..feed_candidates import FeedCandidates
from ..outputs import FeedAnswerV2
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..schemas import ModelTurn, ToolCall
from ..testing.fake_llm import FakeLLMClient
from ..workflows.feed_execution import FeedExecutionV1
from .test_feed_candidates import decisions, package
from .test_openai_client import completion
from .test_workflows import CONTEXT


def calls(count):
    return [ToolCall(id=f"opaque_{i}", name="search_evidence", arguments={"query": f"topic {i}"})
            for i in range(count)]


def registry(handler, safe=True):
    return ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE], handler, parallel_safe=safe),))


class ParallelFeedTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'psycopg.connect', 'app.ai.retrieval.embedding.get_model'):
            guard = patch(target, side_effect=AssertionError('Live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def run_feed(self, handler, count=3, *, safe=True, **options):
        client = FakeLLMClient([ModelTurn(tool_calls=calls(count)),
                                ModelTurn(text=json.dumps(decisions(5, range(5))))])
        execution = FeedExecutionV1(client, registry(handler, safe), **options)
        result = execution(CONTEXT, '{"patients":[]}')
        return result, client

    def test_three_searches_overlap_finish_out_of_order_and_keep_all_source_order(self):
        barrier, release_a, release_b = Barrier(3), Event(), Event()
        finished, contexts, lock = [], [], Lock()
        marker = ContextVar('synthetic_context', default=None)
        token = marker.set('request-local')
        self.addCleanup(marker.reset, token)
        def handler(context, args):
            index = int(args.query[-1])
            with lock:
                contexts.append((context.request_id, marker.get(), request_deadline.get()))
            barrier.wait(2)  # Sequential execution cannot pass this barrier.
            if index == 0:
                if not release_a.wait(2): raise TimeoutError()
            elif index == 1:
                if not release_b.wait(2): raise TimeoutError()
            with lock: finished.append(index)
            if index == 2: release_a.set()
            if index == 0: release_b.set()
            return package([f"{index}-{i}" for i in range(args.top_k)])
        try:
            result, client = self.run_feed(handler)
        finally:
            release_a.set(); release_b.set()
        self.assertEqual(finished, [2, 0, 1])
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual((result.execution.tool_rounds, result.execution.total_tool_calls, len(client.requests)), (1, 3, 2))
        self.assertEqual([s.pmid for s in result.sources], ['0-0', '1-0', '2-0', '0-1', '1-1'])
        self.assertEqual([(s.source_ref.tool_call_id, s.source_ref.evidence_index) for s in result.sources],
                         [('opaque_0', 0), ('opaque_1', 0), ('opaque_2', 0), ('opaque_0', 1), ('opaque_1', 1)])
        tool_messages = [m for m in client.requests[1][0] if m.role == 'tool']
        self.assertEqual([m.tool_call_id for m in tool_messages], [f'opaque_{i}' for i in range(3)])
        self.assertEqual([t.result_count for t in result.execution.called_tools], [8, 8, 8])
        self.assertTrue(all(c[0] == CONTEXT.request_id and c[1] == 'request-local' and c[2] for c in contexts))

    def test_batch_budget_overflow_or_duplicate_ids_executes_nothing(self):
        for requested in (calls(4), [calls(1)[0], calls(1)[0]]):
            touched = []
            client = FakeLLMClient([ModelTurn(tool_calls=requested)])
            result = FeedExecutionV1(client, registry(lambda c, a: touched.append(a)))(CONTEXT, '{}')
            self.assertEqual(touched, [])
            self.assertEqual(result.execution.total_tool_calls, 0)
            self.assertEqual(len(client.requests), 1)
            self.assertEqual(result.execution.errors[-1].code,
                             'tool_limit' if len(requested) == 4 else 'duplicate_call_id')

    def test_invalid_or_excess_top_k_is_rejected_before_any_search(self):
        for bad in (9, 20, 0, True, '5'):
            requested = calls(2)
            requested[-1].arguments['top_k'] = bad
            touched = []
            client = FakeLLMClient([ModelTurn(tool_calls=requested)])
            result = FeedExecutionV1(client, registry(lambda c,a: touched.append(a)))(CONTEXT, '{}')
            self.assertEqual(result.execution.errors[-1].code, 'invalid_arguments')
            self.assertEqual(touched, [])
            self.assertEqual(result.execution.total_tool_calls, 0)

    def test_one_failed_search_never_publishes_partial_candidates_or_retries(self):
        barrier = Barrier(3)
        def handler(context, args):
            barrier.wait(2)
            if args.query.endswith('1'): raise RuntimeError('PRIVATE')
            return package([args.query])
        with patch.object(FeedCandidates, 'add', autospec=True) as publish:
            result, client = self.run_feed(handler)
            publish.assert_not_called()
        self.assertEqual(result.execution.status, 'failed')
        self.assertIsNone(result.output)
        self.assertEqual(result.sources, [])
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(result.execution.total_tool_calls, 3)
        # Early failure reports observed completions only, in request order;
        # pending peers have no fabricated completion Trace.
        self.assertEqual(sum(not t.success for t in result.execution.called_tools), 1)
        self.assertTrue(1 <= len(result.execution.called_tools) <= 3)
        self.assertEqual([t.error_type for t in result.execution.called_tools if not t.success], ['tool_exception'])
        self.assertNotIn('PRIVATE', result.model_dump_json())

    def test_invalid_evidence_in_batch_is_not_partially_published(self):
        from ..schemas import ToolOutcome, ToolTrace
        client = FakeLLMClient([ModelTurn(tool_calls=calls(2))])
        execution = FeedExecutionV1(client, registry(lambda c,a: package(['A'])))
        def execute(call, context):
            data = package(['A']).model_dump() if call.id.endswith('0') else {'query':'x','evidence':[{}]}
            return ToolOutcome(call_id=call.id, tool_name=call.name, success=True, data=data), ToolTrace(
                tool_name=call.name, success=True, duration_ms=0)
        with patch.object(execution.runner.executor, 'execute', side_effect=execute), patch.object(FeedCandidates, 'add') as publish:
            result = execution(CONTEXT, '{}')
            publish.assert_not_called()
        self.assertEqual(result.execution.status, 'failed')
        self.assertEqual(result.sources, [])
        self.assertEqual(len(client.requests), 1)

    def test_tool_timeout_returns_without_join_and_discards_late_result(self):
        release, finished, entered = Event(), Event(), Event()
        def handler(context, args):
            entered.set()
            if args.query.endswith('1'):
                try: release.wait(3)
                finally: finished.set()
            return package([args.query])
        try:
            result, client = self.run_feed(handler, tool_timeout_seconds=0.1)
            self.assertTrue(entered.is_set())
            self.assertFalse(finished.is_set())  # Returned while Handler is still running.
            self.assertEqual(result.execution.status, 'failed')
            self.assertEqual(result.execution.errors[-1].code, 'tool_timeout')
            before = result.model_dump_json()
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(result.model_dump_json(), before)
        self.assertEqual(len(client.requests), 1)

    def test_request_deadline_propagates_and_late_workers_do_not_mutate_state(self):
        release, done = Event(), [Event() for _ in range(3)]
        def handler(context, args):
            try: release.wait(3)
            finally: done[int(args.query[-1])].set()
            return package([args.query])
        try:
            result, client = self.run_feed(handler, request_timeout_seconds=0.2)
            self.assertEqual(result.execution.errors[-1].code, 'request_timeout')
            self.assertEqual(result.sources, [])
            self.assertIsNone(result.output)
            before = result.model_dump_json()
        finally:
            release.set()
        self.assertTrue(all(event.wait(1) for event in done))
        self.assertEqual(result.model_dump_json(), before)
        self.assertEqual(len(client.requests), 1)

    def test_workflow_opt_out_unsafe_spec_or_low_cap_preserve_sequential_execution(self):
        for safe, options in ((True, {'allow_parallel_tools':False}), (False, {}),
                              (True, {'max_parallel_calls':1}), (True, {'max_parallel_calls':2})):
            order = []
            def handler(context, args):
                order.append(args.query)
                return package([f'{args.query}-{i}' for i in range(args.top_k)])
            result, _ = self.run_feed(handler, safe=safe, **options)
            self.assertEqual(result.execution.status, 'completed')
            self.assertEqual(order, [f'topic {i}' for i in range(3)])

    def test_default_chat_is_sequential_even_with_parallel_safe_tool(self):
        order = []
        def handler(context, args):
            order.append(args.query)
            return package([args.query])
        client = FakeLLMClient([ModelTurn(tool_calls=calls(3)), ModelTurn(text='Legacy chat')])
        result = AgentLoopRunner(client, registry(handler)).run('Synthetic', CONTEXT)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(order, [f'topic {i}' for i in range(3)])
        self.assertEqual(client.options, [None, None])

    def test_round_robin_dedup_no_scores_and_followup_rebuild(self):
        table, source = FeedCandidates(), {}
        for call_id, pmids in [('A',[None,'same','a','b','c','d']), ('B',['same','x','y']), ('C',['z'])]:
            source[call_id] = package(pmids)
            table.add(call_id, source[call_id])
        result, refs = table.finalize(json.dumps(decisions(5, range(5))), source)
        self.assertEqual([s.pmid for s in refs], ['same','z','a','x','b'])
        self.assertEqual([(i.source_ref.tool_call_id, i.source_ref.evidence_index) for i in result.items],
                         [('A',1),('C',0),('A',2),('B',1),('A',3)])
        self.assertEqual(json.loads(table.message().content)['candidate_count'], 5)

    def test_registry_marks_only_explicit_production_search_parallel_safe(self):
        self.assertTrue(build_evidence_registry().get('search_evidence').parallel_safe)
        self.assertFalse(registry(lambda c,a:None, safe=False).get('search_evidence').parallel_safe)

    def test_sdk_parallel_searches_require_tools_and_use_two_generations(self):
        wire, top_k = [], []
        lock = Lock()
        barrier = Barrier(3)
        def transport(request):
            wire.append(json.loads(request.content))
            if len(wire) == 1:
                return httpx.Response(200,json=completion({'role':'assistant','content':None,'tool_calls':[
                    {'id':c.id,'type':'function','function':{'name':c.name,'arguments':json.dumps(c.arguments)}}
                    for c in calls(3)]}, 'tool_calls'))
            return httpx.Response(200,json=completion({'role':'assistant','content':json.dumps(decisions(5,range(5)))}))
        sdk = OpenAI(api_key='offline-placeholder',http_client=httpx.Client(transport=httpx.MockTransport(transport)))
        self.addCleanup(sdk.close)
        def handler(context,args):
            with lock: top_k.append((args.query,args.top_k))
            barrier.wait(2)
            return package([f'{args.query}-{i}' for i in range(args.top_k)])
        result = FeedExecutionV1(OpenAIClient(sdk,model='offline-model'),registry(handler))(CONTEXT,'{"patients":[]}')
        self.assertEqual(result.execution.status, 'completed')
        self.assertEqual(len(wire), 2)
        self.assertEqual(wire[0]['tool_choice'], 'required')
        self.assertEqual(wire[1]['tool_choice'], 'none')
        self.assertTrue(all(w['parallel_tool_calls'] and w['reasoning_effort']=='none' for w in wire))
        self.assertEqual(sorted(top_k), [('topic 0',8),('topic 1',8),('topic 2',8)])
        self.assertNotIn('FeedDecisionDraft', str(wire[0]['messages']))
        self.assertEqual(sum('"untrusted_feed_candidates"' in (m.get('content') or '') for m in wire[1]['messages']),1)

    def test_resource_admission_respects_deadline_without_starting_a_worker(self):
        from threading import BoundedSemaphore
        slots = BoundedSemaphore(1)
        slots.acquire()
        touched = []
        token = request_deadline.set(monotonic() + 0.05)
        try:
            with patch('app.ai.agent._execution._PARALLEL_SLOTS', slots):
                with self.assertRaises(RequestTimeout):
                    bounded_parallel([lambda: touched.append(True)])
        finally:
            slots.release()
            request_deadline.reset(token)
        self.assertEqual(touched, [])

    def test_mixed_safe_and_unsafe_tools_use_original_sequential_order(self):
        from ..tools.contracts import PatientProfileOutput
        order = []
        def profile(context, args):
            order.append('profile')
            return PatientProfileOutput()
        def search(context, args):
            order.append('search')
            return package(['A'])
        tools = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.PATIENT_PROFILE],profile),
                              ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE],search,parallel_safe=True)))
        client = FakeLLMClient([ModelTurn(tool_calls=[
            ToolCall(id='profile',name='get_patient_profile',arguments={}),calls(1)[0]]),ModelTurn(text='Synthetic')])
        result = AgentLoopRunner(client,tools,allow_parallel_tools=True).run('Synthetic',CONTEXT)
        self.assertEqual(result.status,'completed')
        self.assertEqual(order,['profile','search'])

    def test_parallel_options_are_explicit_strict_and_can_disable_only_rag(self):
        for value in (0, -1, True, 1.2, 9):
            with self.assertRaises(ValueError):
                AgentLoopRunner(FakeLLMClient([]),registry(lambda c,a:None),max_parallel_calls=value)
        with self.assertRaises(ValueError):
            AgentLoopRunner(FakeLLMClient([]),registry(lambda c,a:None),allow_parallel_tools='yes')
        self.assertFalse(build_evidence_registry(parallel_safe=False).get('search_evidence').parallel_safe)

    def test_prompt_quality_policy_preserves_privacy_and_cold_start(self):
        from ..workflows.feed_prompts import FeedPromptBuilder
        from ..feed_candidates import build_feed_decision_policy
        initial = FeedPromptBuilder().build_system_prompt(CONTEXT)
        final = build_feed_decision_policy().content
        for term in ('1~3개','하나의 연구 목적','영어 검색어','없는 단계/중증도/위험도','환자마다 검색하거나 3회를 채우지 않는다'):
            self.assertIn(term, initial)
        for term in ('include=false','프로토콜','인과관계','카테고리','Cold Start'):
            # Category instructions use the canonical field name.
            self.assertIn('category' if term == '카테고리' else term,final)
        self.assertIn('이름/UUID/patient_ref', initial)
        self.assertIn('비신뢰 데이터',final)

    def test_registry_policy_lookup_is_also_bounded_by_request_deadline(self):
        release, finished = Event(), Event()
        tools = registry(lambda c,a: package(['A']))
        original = tools.get
        def blocked(name):
            try: release.wait(3)
            finally: finished.set()
            return original(name)
        client = FakeLLMClient([ModelTurn(tool_calls=calls(3))])
        execution = FeedExecutionV1(client, tools, request_timeout_seconds=0.2)
        try:
            with patch.object(tools, 'get', side_effect=blocked):
                result = execution(CONTEXT, '{}')
                self.assertEqual(result.execution.errors[-1].code,'request_timeout')
                self.assertEqual(result.execution.total_tool_calls,0)
                self.assertEqual(len(client.requests),1)
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
