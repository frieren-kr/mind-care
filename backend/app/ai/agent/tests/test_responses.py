"""Responses-native SDK wire, continuation and failure contracts; no live I/O."""
from copy import deepcopy
import json
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI

from ..agent_loop_runner import AgentLoopRunner
from ..clients.base import GenerationOptions, GenerationRequest
from ..clients.openai_client import OpenAIClient
from ..conversation import Conversation
from ..schemas import Message, ModelTurn, ToolCall, ToolOutcome
from ..workflows.feed_execution import FeedExecutionV1
from ..workflows.chat_execution import ChatExecutionV1
from .test_workflows import CONTEXT
from .test_feed_candidates import package, decisions
from .test_feed_review_rounds import review
from .test_parallel_feed import registry
from .test_openai_client import response_fixture


def function(identifier='call-one', arguments='{"query":"dementia"}'):
    return {'type': 'function_call', 'id': 'output-'+identifier, 'call_id': identifier,
            'name': 'search_evidence', 'arguments': arguments, 'status': 'completed'}


def reasoning(identifier='reasoning-one'):
    return {'type': 'reasoning', 'id': identifier, 'summary': [],
            'encrypted_content': 'PRIVATE_ENCRYPTED_CONTINUATION', 'status': 'completed'}


def response(items, *, status='completed'):
    data = response_fixture({})
    data.update(output=items, status=status)
    return data


class ResponsesTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket.socket.connect', 'psycopg.connect'):
            guard = patch(name, side_effect=AssertionError('Live I/O forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def client(self, handler):
        sdk = OpenAI(api_key='offline-placeholder', max_retries=2,
                     http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.addCleanup(sdk.close)
        return OpenAIClient(sdk, model='explicit-offline-model')

    def request(self, **options):
        return GenerationRequest(inputs=(Message(role='user',kind='user_input',content='Synthetic request'),),
                                 tools=registry(lambda c,a:package(['A'])).definitions(),
                                 options=GenerationOptions(**options))

    def test_manual_reasoning_continuation_and_original_call_id_are_preserved(self):
        wire=[]
        native=[reasoning(), function()]
        def transport(req):
            self.assertEqual(req.url.path, '/v1/responses')
            body=json.loads(req.content);wire.append(body)
            if len(wire)==1:return httpx.Response(200,json=response(native))
            return httpx.Response(200,json=response_fixture({'text':'Public answer'}))
        result=AgentLoopRunner(self.client(transport), registry(lambda c,a:package(['A']))).run('Synthetic',CONTEXT)
        self.assertEqual(result.status,'completed')
        self.assertEqual(result.final_answer,'Public answer')
        self.assertEqual(wire[1]['input'][3:5],native)
        self.assertEqual(wire[1]['input'][5]['type'],'function_call_output')
        self.assertEqual(wire[1]['input'][5]['call_id'],'call-one')
        self.assertNotEqual(wire[1]['input'][5]['call_id'],native[1]['id'])
        self.assertNotIn('PRIVATE',result.model_dump_json())
        for payload in wire:
            self.assertIs(payload['store'],False)
            self.assertEqual(payload['include'],['reasoning.encrypted_content'])
            for key in ('previous_response_id','reasoning','reasoning_effort','messages'):
                self.assertNotIn(key,payload)
            for tool in payload['tools']:
                self.assertFalse(tool['strict'])
                self.assertNotIn('function',tool)
        self.assertEqual(sum(i.get('type')=='reasoning' for i in wire[1]['input']),1)

    def test_reasoning_and_feed_rounds_keep_only_current_candidate_projection(self):
        wire=[]
        def transport(req):
            wire.append(json.loads(req.content));n=len(wire)
            if n<=2:
                args={'queries':[f'query-{n}']}
                if n==2:args['review']=review(1,(0,))
                return httpx.Response(200,json=response([reasoning(f'rs-{n}'),function(f'call-{n}',json.dumps(args))]))
            return httpx.Response(200,json=response_fixture({'text':json.dumps(decisions(2,(0,1)))}))
        result=FeedExecutionV1(self.client(transport),registry(lambda c,a:package([a.query])))(CONTEXT,'{"patients":[]}')
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(len(wire),3)
        for index,body in enumerate(wire):
            projections=[i for i in body['input'] if isinstance(i.get('content'), str) and i['content'].startswith('{') and json.loads(i['content']).get('context_type') == 'untrusted_feed_candidates']
            self.assertEqual(len(projections),int(index>0))
            if projections:self.assertEqual(json.loads(projections[0]['content'])['candidate_revision'],index)
            self.assertEqual(sum(i.get('type')=='reasoning' for i in body['input']),index)
            self.assertNotIn('feed-rag:',json.dumps(body['input']))
        self.assertEqual([s.pmid for s in result.sources],['query-1','query-2'])

    def test_completed_function_response_is_not_final_even_with_public_text(self):
        wire=[]
        first=response_fixture({'text':'Provisional answer','calls':[function()]})
        def transport(req):
            wire.append(json.loads(req.content))
            return httpx.Response(200,json=first if len(wire)==1 else response_fixture({'text':'Final answer'}))
        calls=[]
        result=AgentLoopRunner(self.client(transport),registry(lambda c,a:calls.append(a.query) or package(['A']))).run('Synthetic',CONTEXT)
        self.assertEqual(result.final_answer,'Final answer')
        self.assertEqual(calls,['dementia'])
        self.assertEqual([i for i in wire[1]['input'] if i.get('type') in ('message','function_call')],first['output'])

    def test_noncompleted_statuses_fail_closed_without_tool_execution(self):
        for status in ('incomplete','failed','cancelled','queued','in_progress',None,'unknown'):
            with self.subTest(status=status):
                reached=[]
                result=AgentLoopRunner(self.client(lambda req:httpx.Response(200,json=response([function()],status=status))),
                    registry(lambda c,a:reached.append(a))).run('PRIVATE_INPUT',CONTEXT)
                self.assertEqual(result.status,'provider_error')
                self.assertEqual(reached,[])
                self.assertIsNone(result.final_answer)
                self.assertNotIn('PRIVATE',result.model_dump_json())

    def test_refusal_unknown_tools_and_malformed_output_items_are_rejected(self):
        invalid=[{'type':'web_search_call','id':'web'}, {'type':'future_output','id':'future'},
                 {'type':'function_call','id':'item','name':'search_evidence','arguments':'{}'},
                 {'type':'message','id':'msg','role':'assistant','status':'completed',
                  'content':[{'type':'refusal','refusal':'PRIVATE_REFUSAL'}]},
                 {**reasoning(),'status':'incomplete'}]
        for item in invalid:
            with self.subTest(kind=item['type']):
                result=AgentLoopRunner(self.client(lambda req:httpx.Response(200,json=response([item]))),
                    registry(lambda c,a:self.fail('No Tool allowed'))).run('Synthetic',CONTEXT)
                self.assertEqual(result.status,'provider_error')
                self.assertNotIn('PRIVATE',result.model_dump_json())

    def test_error_or_incomplete_details_on_completed_response_cannot_pass(self):
        for field,value in [('error',{'code':'server_error','message':'PRIVATE'}),
                            ('incomplete_details',{'reason':'max_output_tokens'})]:
            data=response_fixture({'text':'Must not appear'});data[field]=value
            result=AgentLoopRunner(self.client(lambda req:httpx.Response(200,json=data)),registry(lambda c,a:None)).run('Synthetic',CONTEXT)
            self.assertEqual(result.status,'provider_error')
            self.assertIsNone(result.final_answer)
            self.assertNotIn('PRIVATE',result.model_dump_json())

    def test_duplicate_call_ids_are_rejected_before_executor(self):
        data=response([function(),{**function(),'id':'another-output-item'}])
        result=AgentLoopRunner(self.client(lambda req:httpx.Response(200,json=data)),
            registry(lambda c,a:self.fail('No duplicate execution'))).run('Synthetic',CONTEXT)
        self.assertEqual(result.status,'invalid_response')
        self.assertEqual(result.errors[-1].code,'duplicate_call_id')

    def test_explicit_reasoning_and_flat_forced_function_options(self):
        wire=[]
        def transport(req):
            wire.append(json.loads(req.content));return httpx.Response(200,json=response_fixture({'text':'Synthetic'}))
        client=self.client(transport)
        for effort in ('minimal','low','medium','high'):
            turn=client.generate(self.request(reasoning_effort=effort, required_tool='search_evidence',parallel_tool_calls=True))
            self.assertEqual(turn.text,'Synthetic')
            self.assertEqual(wire[-1]['reasoning'],{'effort':effort})
            self.assertEqual(wire[-1]['tool_choice'],{'type':'function','name':'search_evidence'})
        client.generate(self.request())
        self.assertNotIn('reasoning',wire[-1])
        self.assertEqual(wire[-1]['tool_choice'],'auto')
        self.assertNotIn('parallel_tool_calls',wire[-1])

    def test_no_tools_omits_tool_options_and_invalid_force_does_not_call_provider(self):
        wire=[]
        def transport(req):
            wire.append(json.loads(req.content));return httpx.Response(200,json=response_fixture({'text':'Synthetic'}))
        client=self.client(transport);request=self.request()
        client.generate(GenerationRequest(inputs=request.inputs,tools=[]))
        for key in ('tools','tool_choice','parallel_tool_calls'):self.assertNotIn(key,wire[0])
        with self.assertRaises(ValueError):
            client.generate(GenerationRequest(inputs=request.inputs,tools=[],options=GenerationOptions(tool_choice='required')))
        self.assertEqual(len(wire),1)

    def test_parallel_option_distinguishes_omitted_true_and_false(self):
        wire=[]
        def transport(req):
            wire.append(json.loads(req.content))
            return httpx.Response(200,json=response_fixture({'text':'Synthetic'}))
        client=self.client(transport)
        for value in (None,True,False):
            client.generate(self.request(parallel_tool_calls=value))
            if value is None:self.assertNotIn('parallel_tool_calls',wire[-1])
            else:self.assertIs(wire[-1]['parallel_tool_calls'],value)
        for value in (0,1,'false'):
            with self.subTest(invalid=value),self.assertRaises(ValueError):
                client.generate(self.request(parallel_tool_calls=value))
        self.assertEqual(len(wire),3)

    def test_chat_multiple_calls_keep_sequential_handlers_and_original_call_ids(self):
        wire=[];issued=[];executed=[]
        def transport(req):
            body=json.loads(req.content);wire.append(body)
            self.assertNotIn('parallel_tool_calls',body)
            if len(issued)<6:
                count=1 if body.get('parallel_tool_calls') is False else 3
                batch=[function(f'call-{i}',json.dumps({'query':f'query-{i}'}))
                       for i in range(len(issued),min(len(issued)+count,6))]
                issued.extend(c['call_id'] for c in batch)
                return httpx.Response(200,json=response(batch))
            return httpx.Response(200,json=response_fixture({'text':json.dumps({
                'schema_version':'1','response_type':'chat','answer':'Synthetic','citation_refs':[]})}))
        def handler(context,args):
            executed.append(args.query)
            return package([args.query])
        execution=ChatExecutionV1(self.client(transport),lambda c:registry(handler,safe=False))
        with patch('app.ai.agent.agent_loop_runner.bounded_parallel',
                   side_effect=AssertionError('Chat handlers must stay sequential')):
            result=execution(CONTEXT,'{"user_message":"Synthetic","patient_scope_available":true}')
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(result.execution.errors,[])
        self.assertEqual((result.execution.tool_rounds,result.execution.total_tool_calls),(2,6))
        self.assertEqual(executed,[f'query-{i}' for i in range(6)])
        self.assertEqual(len(wire),3)
        for index,body in enumerate(wire):
            outputs=[i for i in body['input'] if i.get('type')=='function_call_output']
            self.assertEqual([i['call_id'] for i in outputs],issued[:index*3])
            self.assertTrue(all(json.loads(i['output'])['success'] for i in outputs))

    def test_chat_sdk_keeps_five_round_and_ten_call_limits(self):
        for batch_size,expected_calls in ((1,5),(11,0)):
            with self.subTest(batch_size=batch_size):
                wire=[];executed=[]
                def transport(req):
                    wire.append(json.loads(req.content))
                    return httpx.Response(200,json=response([
                        function(f'call-{len(wire)}-{i}') for i in range(batch_size)]))
                execution=ChatExecutionV1(self.client(transport),lambda c:registry(
                    lambda c,a:executed.append(a.query) or package(['A'])))
                result=execution(CONTEXT,'{"user_message":"Synthetic","patient_scope_available":true}')
                self.assertEqual(result.execution.status,'limit_reached')
                self.assertEqual(result.execution.errors[-1].code,'tool_limit')
                self.assertEqual(result.execution.total_tool_calls,expected_calls)
                self.assertEqual(len(executed),expected_calls)

    def test_function_item_id_present_omitted_and_null_keep_call_id_linkage(self):
        for variant in ('present','omitted','null'):
            with self.subTest(variant=variant):
                native=function();wire=[];executed=[]
                if variant=='omitted':native.pop('id')
                elif variant=='null':native['id']=None
                def transport(req):
                    wire.append(json.loads(req.content))
                    return httpx.Response(200,json=response([native]) if len(wire)==1
                        else response_fixture({'text':'Synthetic'}))
                result=AgentLoopRunner(self.client(transport),registry(
                    lambda c,a:executed.append(a.query) or package(['A']))).run('Synthetic',CONTEXT)
                self.assertEqual(result.status,'completed')
                self.assertEqual(executed,['dementia'])
                self.assertEqual(len(wire),2)
                replay=next(i for i in wire[1]['input'] if i.get('type')=='function_call')
                self.assertEqual(replay,{k:v for k,v in native.items() if v is not None})
                output=next(i for i in wire[1]['input'] if i.get('type')=='function_call_output')
                self.assertEqual(output['call_id'],'call-one')
                self.assertTrue(json.loads(output['output'])['success'])

    def test_invalid_call_id_and_invalid_present_item_id_cannot_execute(self):
        invalid=[{k:v for k,v in function().items() if k!='call_id'}]
        invalid.extend({**function(),'call_id':value} for value in (None,'','   ',123,[]))
        invalid.extend({**function(),'id':value} for value in ('',123,[]))
        for native in invalid:
            with self.subTest(native=native):
                result=AgentLoopRunner(self.client(lambda req:httpx.Response(200,json=response([native]))),
                    registry(lambda c,a:self.fail('Invalid identifiers must not execute'))).run('Synthetic',CONTEXT)
                self.assertEqual(result.status,'provider_error')
                self.assertEqual(result.total_tool_calls,0)

    def test_message_and_reasoning_still_require_item_ids(self):
        message=response_fixture({'text':'Synthetic'})['output'][0]
        for original in (message,reasoning()):
            for variant in ('omitted','null'):
                with self.subTest(kind=original['type'],variant=variant):
                    native=deepcopy(original)
                    if variant=='omitted':native.pop('id')
                    else:native['id']=None
                    result=AgentLoopRunner(self.client(lambda req:httpx.Response(200,json=response([native]))),
                        registry(lambda c,a:self.fail('No execution'))).run('Synthetic',CONTEXT)
                    self.assertEqual(result.status,'provider_error')

    def test_shared_client_has_no_response_state_between_concurrent_requests(self):
        barrier=Barrier(2);seen=[]
        def transport(req):
            body=json.loads(req.content);seen.append(body);barrier.wait(3)
            user=next(i['content'] for i in body['input'] if i.get('role')=='user')
            return httpx.Response(200,json=response_fixture({'text':user}))
        client=self.client(transport)
        def run(i):return AgentLoopRunner(client,registry(lambda c,a:None)).run(f'user-{i}',CONTEXT).final_answer
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(list(pool.map(run,(1,2))),['user-1','user-2'])
        self.assertTrue(all(not any(i.get('type') in ('reasoning','function_call_output') for i in p['input']) for p in seen))
        self.assertFalse(hasattr(client,'previous_response_id'))

    def test_conversation_error_preserves_existing_history(self):
        conversation=Conversation();conversation.items=[Message(role='user',kind='user_input',content='Synthetic')]
        before=deepcopy(conversation.items)
        turn=ModelTurn(tool_calls=[ToolCall(id='call',name='search_evidence',arguments='{}')],output_items=[function('different')])
        with self.assertRaises(ValueError):conversation.append_turn(turn,[ToolOutcome(call_id='call',tool_name='search_evidence',success=True,data={})])
        self.assertEqual(conversation.items,before)

    def test_continuation_not_exposed_in_repr(self):
        self.assertNotIn('PRIVATE',repr(ModelTurn(output_items=[reasoning()])))
        self.assertNotIn('PRIVATE',repr(Message(type='provider_output',kind='model',provider_output=reasoning())))

    def test_conversation_failure_is_sanitized_and_not_retried(self):
        wire=[]
        def transport(req):
            wire.append(req)
            return httpx.Response(200,json=response([function()]))
        with patch.object(Conversation,'append_turn',side_effect=RuntimeError('PRIVATE_CONTINUATION')):
            result=AgentLoopRunner(self.client(transport),registry(lambda c,a:package(['A']))).run('Synthetic',CONTEXT)
        self.assertEqual(result.status,'failed')
        self.assertEqual(result.errors[-1].code,'conversation_error')
        self.assertEqual(len(wire),1)
        self.assertNotIn('PRIVATE',result.model_dump_json())


class ResponsesBackendBridgeTests(unittest.TestCase):
    """Unchanged Backend assembly/API with Responses-native SDK and Mock DB/RAG."""
    def check_bridge(self):
        from tests.test_recommendations import OfflineFeedHandoffTests
        from .test_feed_execution import final_turn
        from app.feed_schemas import SavedFeedBatch
        from uuid import uuid4
        fixture=OfflineFeedHandoffTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        wire=[]
        def transport(request):
            self.assertEqual(request.url.path,'/v1/responses')
            wire.append(json.loads(request.content))
            return httpx.Response(200,json=response([function('search_1','{"queries":["Dementia sleep research"]}')])
                if len(wire)==1 else response_fixture({'text':final_turn().text}))
        fixture.use_real_dependency_with_mock_provider(transport)
        with patch('app.ai.retrieval.search.search',return_value=fixture.search.return_value) as rag, \
             patch('app.api.recommendations.feed_read.save_feed_batch',return_value=SavedFeedBatch(
                 batch_id=uuid4(),generated_at=fixture.profile.generated_at)) as save:
            result=fixture.get(refresh=True)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['mode'],'ai')
        self.assertEqual(result.json()['items'][0]['title'],'DB title 0')
        self.assertEqual(result.json()['items'][0]['personal_reason'],'Synthetic care interest')
        self.assertIsNotNone(result.json()['generated_at'])
        self.assertEqual(len(wire),2)
        self.assertEqual([t['name'] for t in wire[0]['tools']],['search_evidence'])
        self.assertTrue(all(p['store'] is False for p in wire))
        self.assertEqual([p['tool_choice'] for p in wire],['required','auto'])
        rag.assert_called_once_with(query='Dementia sleep research',k=8)
        save.assert_called_once()
        fixture.search.assert_not_called()

    def test_unchanged_backend_dependency_and_handoff_work_with_responses(self):
        self.check_bridge()

    def test_unchanged_backend_works_with_loopback_socketpair(self):
        import socket
        fallback=getattr(socket,'_fallback_socketpair',None)
        if fallback is None:self.skipTest('No platform fallback socketpair')
        with patch.object(socket,'socketpair',wraps=fallback) as sockets:
            self.check_bridge()
        sockets.assert_called()
