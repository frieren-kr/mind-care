"""Production guideline assembly through Executor and Chat, using offline I/O."""
from functools import partial
import inspect
import json
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI

from ..adapters.rag_guidelines import RagGuidelineRetriever
from ..clients.openai_client import OpenAIClient
from ..composition import build_chat_registry, build_evidence_registry
from ..evidence import EvidenceStore
from ..executor import ToolExecutor
from ..registry import TOOL_CONTRACTS, ToolName
from ..schemas import ModelTurn, ToolCall
from ..testing.chat_workflow_fakes import chat_final
from ..testing.fake_llm import FakeLLMClient
from ..tools.contracts import EvidencePackage, GuidelineSearchInput
from ..tools.guidelines import GuidelineRetriever, SearchGuidelinesHandler, SEARCH_GUIDELINES_DATA_POLICY
from ..workflows.chat_context_prepare import prepare_chat_input
from ..workflows.chat_execution import ChatExecutionV1
from ..workflows.chat_prompts import CHAT_SEARCH_EVIDENCE_SUCCESS_POLICY
from .test_chat_guideline_budget import search
from .test_feed_candidates import package
from .test_responses import function, response, response_fixture
from .test_search_guidelines_adapter import items
from .test_workflows import CONTEXT

USER_CONTEXT=CONTEXT.model_copy(update={'patient_id':None})


class SearchGuidelinesHandlerTests(TestCase):
    def setUp(self):
        for target in ('socket.socket.connect','psycopg.connect','app.ai.retrieval.embedding.get_model'):
            guard=patch(target,side_effect=AssertionError('Live I/O forbidden'))
            guard.start();self.addCleanup(guard.stop)

    def factory(self,lookup,**kwargs):
        return partial(build_chat_registry,guidelines_retriever=RagGuidelineRetriever(search_fn=lookup),**kwargs)

    def execute(self,turns,lookup,**kwargs):
        client=FakeLLMClient(turns)
        result=ChatExecutionV1(client,self.factory(lookup,**kwargs))(USER_CONTEXT,prepare_chat_input(USER_CONTEXT,'Synthetic question'))
        return result,client

    def test_handler_forwards_exact_context_and_values_and_returns_same_package(self):
        retriever=Mock(spec=GuidelineRetriever)
        result=EvidencePackage(query='q',evidence=[])
        retriever.search_guidelines.return_value=result
        args=GuidelineSearchInput(query=' q ',top_k=4)
        self.assertIs(SearchGuidelinesHandler(retriever)(USER_CONTEXT,args),result)
        retriever.search_guidelines.assert_called_once_with(query=' q ',top_k=4,context=USER_CONTEXT)
        self.assertIs(retriever.search_guidelines.call_args.kwargs['context'],USER_CONTEXT)
        self.assertIs(inspect.signature(SearchGuidelinesHandler.__init__).parameters['retriever'].annotation,GuidelineRetriever)

    def test_factory_binds_canonical_production_tool_in_both_scopes_and_keeps_feed_factory(self):
        for context in (CONTEXT,USER_CONTEXT):
            registry=build_chat_registry(context)
            spec=registry.get('search_guidelines')
            self.assertEqual(registry.mode,'production')
            self.assertIs(spec.contract,TOOL_CONTRACTS[ToolName.SEARCH_GUIDELINES])
            self.assertFalse(spec.test_only)
            self.assertFalse(spec.contract.patient_scoped)
            self.assertTrue(spec.parallel_safe)
            self.assertIsInstance(spec.handler,SearchGuidelinesHandler)
            self.assertIsInstance(spec.handler.retriever,RagGuidelineRetriever)
            self.assertIsNone(spec.handler.retriever.search_fn)
            self.assertFalse(build_chat_registry(context,parallel_safe=False).get('search_guidelines').parallel_safe)
        self.assertEqual(set(build_chat_registry(USER_CONTEXT).names()),
                         {'search_evidence','search_guidelines','get_caregiver_profile'})
        self.assertEqual(build_evidence_registry().names(),('search_evidence',))

    def test_construction_and_direct_answer_never_execute_retrieval(self):
        from app.ai.retrieval import search as rag
        with patch.object(rag,'search_guidelines',side_effect=AssertionError('Unrequested RAG call')) as lookup:
            client=FakeLLMClient([chat_final('Synthetic direct answer')])
            result=ChatExecutionV1(client,build_chat_registry)(USER_CONTEXT,prepare_chat_input(USER_CONTEXT,'Hello'))
        self.assertEqual(result.execution.status,'completed')
        lookup.assert_not_called()
        self.assertFalse(any(m.content==SEARCH_GUIDELINES_DATA_POLICY for m in client.requests[0][0]))
        self.assertIn('search_guidelines',[d['name'] for d in client.requests[0][1]])

    def test_executor_default_arguments_and_trace_with_no_patient(self):
        lookup=Mock(return_value=items('A','B'))
        registry=self.factory(lookup)(USER_CONTEXT)
        outcome,trace=ToolExecutor(registry).execute(search(' q ','guide'),USER_CONTEXT)
        self.assertTrue(outcome.success)
        self.assertEqual(trace.result_count,2)
        self.assertEqual(outcome.data['query'],'q')
        lookup.assert_called_once_with(query=' q ',k=2)

    def test_invalid_input_is_rejected_before_handler(self):
        for args in ({'query':' '},{'query':'x'*2001},{'query':'q','top_k':5},
                     {'query':'q','top_k':True},{'query':'q','top_k':'2'},
                     {'query':'q','patient_id':'PRIVATE'},{'query':'q','source_url':'PRIVATE'}):
            lookup=Mock(side_effect=AssertionError('Invalid input reached RAG'))
            outcome,trace=ToolExecutor(self.factory(lookup)(USER_CONTEXT)).execute(
                ToolCall(id='bad',name='search_guidelines',arguments=args),USER_CONTEXT)
            self.assertEqual(outcome.error.code,'invalid_arguments')
            self.assertFalse(trace.success)
            lookup.assert_not_called()

    def test_normal_empty_result_delivers_policy_once_and_has_zero_trace_count(self):
        lookup=Mock(return_value=[])
        result,client=self.execute([ModelTurn(tool_calls=[search('g','guide')]),chat_final()],lookup)
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(result.execution.errors,[])
        self.assertEqual(result.execution.called_tools[0].result_count,0)
        self.assertEqual(result.sources,[])
        messages=client.requests[1][0]
        self.assertEqual(sum(m.content==SEARCH_GUIDELINES_DATA_POLICY for m in messages),1)
        self.assertFalse(any(m.content==CHAT_SEARCH_EVIDENCE_SUCCESS_POLICY for m in messages))

    def test_rag_failure_and_bad_shapes_do_not_become_empty_success_or_deliver_policy(self):
        for raw in (None,[{}],['PRIVATE'],RuntimeError('PRIVATE failure')):
            lookup=Mock(side_effect=raw) if isinstance(raw,Exception) else Mock(return_value=raw)
            result,client=self.execute([ModelTurn(tool_calls=[search('bad','guide')]),chat_final()],lookup)
            self.assertEqual(result.execution.status,'completed')
            self.assertEqual(result.execution.errors[0].code,'tool_exception')
            self.assertFalse(result.execution.called_tools[0].success)
            self.assertIsNone(result.execution.called_tools[0].result_count)
            self.assertNotIn('PRIVATE',result.model_dump_json())
            self.assertFalse(any(m.content==SEARCH_GUIDELINES_DATA_POLICY for m in client.requests[1][0]))
            lookup.assert_called_once()

    def test_output_validation_still_belongs_to_executor(self):
        retriever=Mock(spec=GuidelineRetriever)
        retriever.search_guidelines.return_value={'query':'q','evidence':[{'PRIVATE':'bad'}]}
        result=ChatExecutionV1(FakeLLMClient([ModelTurn(tool_calls=[search('g','guide')]),chat_final()]),
            partial(build_chat_registry,guidelines_retriever=retriever))(USER_CONTEXT,prepare_chat_input(USER_CONTEXT,'Synthetic'))
        self.assertEqual(result.execution.errors[0].code,'invalid_output')
        self.assertEqual(result.sources,[])

    def test_wrong_type_and_overreturn_are_rejected_by_existing_runner_before_source_registration(self):
        for raw in (package(['A']).evidence,items('A','B')):
            store=EvidenceStore()
            with patch('app.ai.agent.agent_loop_runner.EvidenceStore',return_value=store):
                result,client=self.execute([ModelTurn(tool_calls=[search('bad','guide',1)]),chat_final()],Mock(return_value=raw))
            self.assertEqual(result.execution.errors[0].code,'invalid_output')
            self.assertEqual(store.call_ids,set())
            self.assertFalse(any(m.content==SEARCH_GUIDELINES_DATA_POLICY for m in client.requests[1][0]))

    def test_explicit_timeout_propagates_existing_classification_without_retry(self):
        lookup=Mock(side_effect=TimeoutError('PRIVATE'))
        result,client=self.execute([ModelTurn(tool_calls=[search('g','guide')])],lookup)
        self.assertEqual(result.execution.status,'failed')
        self.assertEqual(result.execution.errors[0].code,'tool_timeout')
        self.assertEqual(len(client.requests),1)
        lookup.assert_called_once()

    def test_blocking_retrieval_is_bounded_by_existing_executor(self):
        entered,release=Event(),Event()
        def slow(**kwargs):
            entered.set();release.wait(2)
            return items('late')
        lookup=Mock(side_effect=slow)
        try:
            result,trace=ToolExecutor(self.factory(lookup)(USER_CONTEXT),timeout_seconds=0.05).execute(search('g','guide'),USER_CONTEXT)
            self.assertTrue(entered.is_set())
            self.assertEqual(result.error.code,'tool_timeout')
            self.assertIsNone(result.data)
            self.assertFalse(trace.success)
        finally:release.set()

    def test_real_composition_sdk_mixed_search_budget_policies_and_original_citations(self):
        wire=[]
        def guide(identifier,args):return {**function(identifier,args),'name':'search_guidelines'}
        turns=[response([function('p','{"query":"papers","top_k":2}'),guide('g1','{"query":"g1"}')]),
            response([guide('g2','{"query":"g2","top_k":4}')]),response_fixture({'text':chat_final(selected=[True]*6).text})]
        def transport(request):
            wire.append(json.loads(request.content))
            return httpx.Response(200,json=turns[len(wire)-1])
        lookup=Mock(side_effect=lambda **kw:items(*(kw['query']+str(i) for i in range(kw['k']))))
        papers=Mock()
        papers.search_evidence.side_effect=lambda **kw:package(['P'+str(i) for i in range(kw['top_k'])])
        with OpenAI(api_key='offline-placeholder',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
            result=ChatExecutionV1(OpenAIClient(sdk,model='offline'),self.factory(lookup,retriever=papers))(
                USER_CONTEXT,prepare_chat_input(USER_CONTEXT,'Synthetic mixed request'))
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual([c.kwargs for c in lookup.call_args_list],[{'query':'g1','k':2},{'query':'g2','k':2}])
        self.assertEqual([s.evidence_type for s in result.sources],['new_research']*2+['guideline']*4)
        self.assertEqual([(s.source_ref.tool_call_id,s.source_ref.evidence_index) for s in result.sources],
                         [('p',0),('p',1),('g1',0),('g1',1),('g2',0),('g2',1)])
        self.assertEqual(result.sources[-1].source_url,'https://example.invalid/g21')
        self.assertFalse(any(m.get('content')==SEARCH_GUIDELINES_DATA_POLICY for m in wire[0]['input']))
        for body in wire[1:]:
            messages=body['input']
            policies=[m for m in messages if m.get('content')==SEARCH_GUIDELINES_DATA_POLICY]
            self.assertEqual(len(policies),1)
            self.assertEqual(policies[0]['role'],'system')
            outputs=[m for m in messages if m.get('type')=='function_call_output']
            self.assertEqual([m['call_id'] for m in outputs],['p','g1'] if body is wire[1] else ['p','g1','g2'])
            self.assertGreater(messages.index(policies[0]),messages.index(outputs[1]))
            candidates=next(json.loads(m['content']) for m in messages if m.get('role')=='user' and 'untrusted_chat_candidates' in m.get('content',''))
            self.assertEqual(candidates['candidates'][-1]['abstract'],items('A')[0].abstract)
            self.assertTrue(all('source_url' not in row and 'pmid' not in row for row in candidates['candidates']))
            self.assertFalse(any('Ignore previous instructions.' in m.get('content','') for m in messages if m.get('role')=='system'))
        calls=[m for m in wire[-1]['input'] if m.get('type')=='function_call']
        self.assertEqual(json.loads(calls[-1]['arguments'])['top_k'],4)

    def test_default_factory_reaches_real_rag_guidelines_entry_only_when_called(self):
        from app.ai.retrieval import search as rag
        with patch.object(rag,'search_guidelines',return_value=items('A')) as lookup:
            client=FakeLLMClient([ModelTurn(tool_calls=[search(' q ','guide')]),chat_final(selected=[True])])
            result=ChatExecutionV1(client,build_chat_registry)(USER_CONTEXT,prepare_chat_input(USER_CONTEXT,'Synthetic'))
        self.assertEqual(result.execution.status,'completed')
        lookup.assert_called_once_with(query=' q ',k=2)
        self.assertEqual(result.sources[0].source_url,'https://example.invalid/A')
