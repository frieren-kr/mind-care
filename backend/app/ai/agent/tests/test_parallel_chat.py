"""Bounded Chat parallel execution with synthetic, explicitly safe handlers."""
import json
from threading import Barrier, BoundedSemaphore, Event, Lock, Thread
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI

from ..agent_loop_runner import AgentLoopRunner
from ..clients.openai_client import OpenAIClient
from ..composition import build_evidence_registry
from ..outputs import ChatAnswerV1
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..schemas import ModelTurn, ToolCall
from ..testing.fake_llm import FakeLLMClient
from ..testing.fake_tools import fake_get_patient_profile, fake_get_recent_care_logs, fake_search_evidence
from ..testing.chat_workflow_fakes import chat_final
from ..tools.contracts import PatientProfileOutput
from ..workflows.chat_context_prepare import prepare_chat_input
from ..workflows.chat_execution import ChatExecutionV1
from .test_feed_candidates import package
from .test_responses import function, response, response_fixture
from .test_workflows import CONTEXT


def calls(count):
    return [ToolCall(id=f'call-{i}',name='search_evidence',arguments={'query':str(i)})
            for i in range(count)]


def registry(handler, safe=True):
    return ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE],handler,
                                  parallel_safe=safe),))


class ParallelChatTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect','psycopg.connect','app.ai.retrieval.embedding.get_model'):
            guard=patch(target,side_effect=AssertionError('Live I/O forbidden'))
            guard.start();self.addCleanup(guard.stop)

    def execute(self, client, tools, **options):
        return ChatExecutionV1(client,lambda context:tools,**options)(
            CONTEXT,prepare_chat_input(CONTEXT,'Synthetic question'))

    def test_safe_two_to_five_calls_run_concurrently_with_cap_and_order(self):
        for count in (2,3,4,5):
            with self.subTest(count=count):
                barriers=[Barrier(min(3,count-start)) for start in range(0,count,3)]
                finished=[Event() for _ in range(count)]
                lock=Lock();active=0;peak=0;completed=[]
                def handler(context,args):
                    nonlocal active,peak
                    i=int(args.query)
                    with lock:
                        active+=1;peak=max(peak,active)
                        self.assertLessEqual(active,3)
                    try:
                        barriers[i//3].wait(2)
                        end=min((i//3+1)*3,count)
                        if i+1<end:self.assertTrue(finished[i+1].wait(2))
                        with lock:completed.append(i)
                        return package([args.query])
                    finally:
                        with lock:active-=1
                        finished[i].set()
                client=FakeLLMClient([ModelTurn(tool_calls=calls(count)),chat_final()])
                result=self.execute(client,registry(handler))
                self.assertEqual(result.execution.status,'completed')
                self.assertEqual(result.execution.errors,[])
                self.assertEqual(peak,min(count,3))
                self.assertEqual((result.execution.tool_rounds,result.execution.total_tool_calls),(1,count))
                expected=[i for start in range(0,count,3) for i in reversed(range(start,min(start+3,count)))]
                self.assertEqual(completed,expected)
                outputs=[m for m in client.requests[1][0] if m.type=='function_call_output']
                self.assertEqual([m.call_id for m in outputs],[c.id for c in calls(count)])
                self.assertEqual([json.loads(m.content)['data']['evidence'][0]['pmid'] for m in outputs],
                                 [str(i) for i in range(count)])
                self.assertEqual([t.success for t in result.execution.called_tools],[True]*count)

    def test_unsafe_mixed_unknown_single_and_cap_one_use_sequential_path(self):
        for mode in ('unsafe','mixed','unknown','single','cap_one'):
            with self.subTest(mode=mode):
                executed=[]
                def search(context,args):
                    executed.append(args.query);return package([args.query])
                tools=registry(search,safe=mode!='unsafe')
                batch=calls(1 if mode=='single' else 5)
                if mode=='mixed':
                    def profile(context,args):
                        executed.append('profile');return PatientProfileOutput()
                    tools=ToolRegistry((tools.get('search_evidence'),ToolSpec(
                        TOOL_CONTRACTS[ToolName.PATIENT_PROFILE],profile)))
                    batch.insert(1,ToolCall(id='profile',name='get_patient_profile',arguments={}))
                elif mode=='unknown':batch.insert(1,ToolCall(id='unknown',name='unknown',arguments={}))
                client=FakeLLMClient([ModelTurn(tool_calls=batch),chat_final()])
                with patch('app.ai.agent.agent_loop_runner.bounded_parallel',
                           side_effect=AssertionError('Sequential fallback required')):
                    if mode=='cap_one':
                        result=AgentLoopRunner(client,tools,allow_parallel_tools=True,max_parallel_calls=1).run_structured(
                            'Synthetic',CONTEXT,output_model=ChatAnswerV1)
                    else:result=self.execute(client,tools)
                self.assertEqual(result.execution.status,'completed')
                self.assertEqual(executed,['0','profile','1','2','3','4'] if mode=='mixed'
                                 else [str(i) for i in range(1 if mode=='single' else 5)])
                self.assertEqual(len(result.execution.errors),int(mode=='unknown'))

    def test_sdk_and_production_registry_preserve_safe_metadata_and_call_ids(self):
        wire=[];barrier=Barrier(3)
        class SyntheticRetriever:
            def search_evidence(self,*,query,top_k,context):
                barrier.wait(2);return package([query])
        tools=build_evidence_registry(retriever=SyntheticRetriever(),parallel_safe=True)
        native=[function(f'call-{i}',json.dumps({'query':str(i)})) for i in range(3)]
        native[0].pop('id');native[1]['id']=None
        def transport(req):
            body=json.loads(req.content);wire.append(body)
            self.assertNotIn('parallel_tool_calls',body)
            self.assertFalse(body['store'])
            return httpx.Response(200,json=response(native) if len(wire)==1 else response_fixture({'text':json.dumps({
                'schema_version':'1','response_type':'chat','answer':'Synthetic','citation_refs':[
                    {'tool_call_id':f'call-{i}','evidence_index':0} for i in range(3)]})}))
        with OpenAI(api_key='offline',http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
            result=self.execute(OpenAIClient(sdk,model='offline'),tools)
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(result.execution.errors,[])
        self.assertEqual([s.pmid for s in result.sources],['0','1','2'])
        outputs=[i for i in wire[1]['input'] if i.get('type')=='function_call_output']
        self.assertEqual([i['call_id'] for i in outputs],[f'call-{i}' for i in range(3)])
        replay=[i for i in wire[1]['input'] if i.get('type')=='function_call']
        self.assertNotIn('id',replay[0]);self.assertNotIn('id',replay[1])
        self.assertEqual(replay[2]['id'],native[2]['id'])

    def test_explicitly_safe_synthetic_profile_logs_and_search_run_together(self):
        barrier=Barrier(3)
        handlers=((ToolName.PATIENT_PROFILE,fake_get_patient_profile,{}),
                  (ToolName.RECENT_CARE_LOGS,fake_get_recent_care_logs,{}),
                  (ToolName.SEARCH_EVIDENCE,fake_search_evidence,{'query':'Synthetic'}))
        specs=[];batch=[]
        for name,handler,args in handlers:
            def synchronized(context,args,handler=handler):
                barrier.wait(2);return handler(context,args)
            specs.append(ToolSpec(TOOL_CONTRACTS[name],synchronized,test_only=True,parallel_safe=True))
            batch.append(ToolCall(id=name.value,name=name.value,arguments=args))
        client=FakeLLMClient([ModelTurn(tool_calls=batch),chat_final()])
        result=self.execute(client,ToolRegistry(tuple(specs),mode='test'))
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual(result.execution.errors,[])
        self.assertEqual([t.tool_name for t in result.execution.called_tools],[c.name for c in batch])
        self.assertEqual([m.call_id for m in client.requests[1][0] if m.type=='function_call_output'],
                         [c.id for c in batch])

    def test_recoverable_exception_and_invalid_output_keep_followup_and_no_retry(self):
        for invalid_output in (False,True):
            with self.subTest(invalid_output=invalid_output):
                barrier=Barrier(3);executed=[];lock=Lock()
                def handler(context,args):
                    with lock:executed.append(args.query)
                    barrier.wait(2)
                    if args.query=='1':
                        if invalid_output:return {'PRIVATE':'invalid output'}
                        raise RuntimeError('PRIVATE exception')
                    return package([args.query])
                client=FakeLLMClient([ModelTurn(tool_calls=calls(3)),chat_final()])
                result=self.execute(client,registry(handler))
                self.assertEqual(result.execution.status,'completed')
                self.assertEqual([e.code for e in result.execution.errors],
                                 ['invalid_output' if invalid_output else 'tool_exception'])
                self.assertCountEqual(executed,['0','1','2'])
                outputs=[m for m in client.requests[1][0] if m.type=='function_call_output']
                self.assertEqual([json.loads(m.content)['success'] for m in outputs],[True,False,True])
                self.assertNotIn('PRIVATE',result.model_dump_json())

    def test_timeout_discards_late_results_and_never_starts_next_chunk(self):
        release=Event();finished=Event();barrier=Barrier(3);executed=[];lock=Lock()
        def handler(context,args):
            with lock:executed.append(args.query)
            barrier.wait(2)
            if args.query=='0':
                try:release.wait(3)
                finally:finished.set()
            return package([args.query])
        client=FakeLLMClient([ModelTurn(tool_calls=calls(5)),chat_final()])
        try:
            result=self.execute(client,registry(handler),tool_timeout_seconds=0.1)
            self.assertEqual(result.execution.status,'failed')
            self.assertEqual(result.execution.errors[-1].code,'tool_timeout')
            self.assertCountEqual(executed,['0','1','2'])
            self.assertEqual(result.execution.total_tool_calls,3)
            self.assertEqual(len(client.requests),1)
            snapshot=result.model_dump_json()
        finally:release.set()
        self.assertTrue(finished.wait(2))
        self.assertEqual(result.model_dump_json(),snapshot)

    def test_no_resource_admission_counts_no_calls_or_traces(self):
        slots=BoundedSemaphore(1);slots.acquire();executed=[]
        client=FakeLLMClient([ModelTurn(tool_calls=calls(3))])
        try:
            with patch('app.ai.agent._execution._PARALLEL_SLOTS',slots):
                result=self.execute(client,registry(lambda c,a:executed.append(a.query)),request_timeout_seconds=0.15)
        finally:slots.release()
        self.assertEqual(result.execution.status,'failed')
        self.assertEqual(result.execution.errors[-1].code,'request_timeout')
        self.assertEqual(result.execution.total_tool_calls,0)
        self.assertEqual(result.execution.called_tools,[])
        self.assertEqual(executed,[])

    def test_partial_admission_deadline_counts_only_started_calls(self):
        slots=BoundedSemaphore(1);started=Event();release=Event();finished=Event();executed=[]
        def handler(context,args):
            executed.append(args.query);started.set()
            try:release.wait(3)
            finally:finished.set()
            return package([args.query])
        client=FakeLLMClient([ModelTurn(tool_calls=calls(5))])
        try:
            with patch('app.ai.agent._execution._PARALLEL_SLOTS',slots):
                result=self.execute(client,registry(handler),request_timeout_seconds=0.2)
            self.assertTrue(started.is_set())
            self.assertEqual(result.execution.status,'failed')
            self.assertEqual(result.execution.total_tool_calls,1)
            self.assertEqual(result.execution.called_tools,[])
            self.assertEqual(executed,['0'])
            snapshot=result.model_dump_json()
        finally:release.set()
        self.assertTrue(finished.wait(2))
        self.assertEqual(result.model_dump_json(),snapshot)

    def test_shared_chat_execution_keeps_concurrent_tool_conversations_isolated(self):
        barrier=Barrier(4)
        class Client:
            def generate(self,request):
                from ..testing.fake_llm import fake_output
                outputs=[m for m in request.inputs if m.type=='function_call_output']
                user=next(m.content for m in request.inputs if m.role=='user')
                if not outputs:return fake_output(ModelTurn(tool_calls=calls(2)))
                owner=json.loads(user)['user_message']
                values=[json.loads(m.content)['data']['evidence'][0]['pmid'] for m in outputs]
                if values!=[owner+'-0',owner+'-1']:raise AssertionError('Cross-request state')
                return fake_output(chat_final(owner))
        def handler(context,args):
            barrier.wait(2);return package([context.request_id+'-'+args.query])
        execution=ChatExecutionV1(Client(),lambda c:registry(handler));results={}
        def run(owner):
            context=CONTEXT.model_copy(update={'request_id':owner})
            results[owner]=execution(context,prepare_chat_input(context,owner))
        threads=[Thread(target=run,args=(owner,)) for owner in ('A','B')]
        for thread in threads:thread.start()
        for thread in threads:thread.join(4)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual({k:v.output.answer for k,v in results.items()},{'A':'A','B':'B'})
