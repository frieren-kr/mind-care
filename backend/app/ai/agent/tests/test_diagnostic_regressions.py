"""Diagnostic preservation, safe provider logging and Runner validation regressions."""
import io
import json
import logging
import unittest
from contextlib import contextmanager
from threading import Event
from app.ai.agent.agent_loop_runner import AgentLoopRunner
from app.ai.agent.registry import ToolRegistry
from app.ai.agent.schemas import ModelTurn, ToolCall
from app.ai.agent.outputs import ChatAnswerV1
from app.ai.agent.workflows.feed_execution import FeedExecutionV1
from app.ai.agent.workflows.chat_execution import ChatExecutionV1
from app.ai.agent.testing.fake_llm import FakeLLMClient
from app.ai.agent.testing.chat_workflow_fakes import chat_final
from app.ai.agent.tests.test_parallel_feed import registry
from app.ai.agent.tests.test_feed_batch_search import batch
from app.ai.agent.tests.test_feed_candidates import package
from app.ai.agent.tests.test_workflows import CONTEXT

@contextmanager
def captured(name='app.ai.agent'):
    stream=io.StringIO(); handler=logging.StreamHandler(stream)
    logger=logging.getLogger(name); previous=logger.level
    logger.setLevel(logging.INFO); logger.addHandler(handler)
    try: yield stream
    finally:
        logger.removeHandler(handler); logger.setLevel(previous)

class AgentAudit(unittest.TestCase):
    def test_completed_tool_diagnostics_survive_later_request_timeout(self):
        release,finished,entered=Event(),Event(),Event()
        def handler(context,args):
            if args.query=='good':return package(['verified'])
            if args.query=='broken':raise RuntimeError('SYNTHETIC_DB_FAILURE')
            entered.set()
            try:
                release.wait(3)
                return package(['late'])
            finally:finished.set()
        client=FakeLLMClient([ModelTurn(tool_calls=[batch(['good','broken','slow'])])])
        try:
            with captured() as logs:
                result=FeedExecutionV1(client,registry(handler),max_parallel_calls=2,
                    tool_timeout_seconds=2,request_timeout_seconds=.5)(CONTEXT,'{}')
                self.assertTrue(entered.is_set())
                text=logs.getvalue()
                self.assertIn('error_code=tool_exception',text)
                self.assertIn('success=True',text)
                self.assertEqual(result.execution.status,'failed')
                self.assertEqual(result.execution.total_tool_calls,3)
                self.assertEqual([t.success for t in result.execution.called_tools],[True,False])
                self.assertEqual([e.code for e in result.execution.errors],['tool_exception','request_timeout'])
                self.assertEqual(result.sources,[])
                snapshot=result.model_dump_json()
        finally:release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(result.model_dump_json(),snapshot)

    def test_completed_tool_diagnostics_survive_same_batch_request_timeout(self):
        release,finished,entered=Event(),Event(),Event()
        def handler(context,args):
            if args.query=='good':return package(['verified'])
            if args.query=='broken':raise RuntimeError('SYNTHETIC_DB_FAILURE')
            entered.set()
            try:
                release.wait(3)
                return package(['late'])
            finally:finished.set()
        client=FakeLLMClient([ModelTurn(tool_calls=[batch(['good','broken','slow'])])])
        try:
            with captured() as logs:
                result=FeedExecutionV1(client,registry(handler),max_parallel_calls=3,
                    tool_timeout_seconds=2,request_timeout_seconds=.5)(CONTEXT,'{}')
                self.assertTrue(entered.is_set())
                text=logs.getvalue()
                self.assertIn('error_code=tool_exception',text)
                self.assertIn('success=True',text)
                self.assertEqual(result.execution.status,'failed')
                self.assertEqual(result.execution.total_tool_calls,3)
                self.assertEqual([t.success for t in result.execution.called_tools],[True,False])
                self.assertEqual([e.code for e in result.execution.errors],['tool_exception','request_timeout'])
                self.assertEqual(result.sources,[])
                snapshot=result.model_dump_json()
        finally:release.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(result.model_dump_json(),snapshot)

    def test_chat_semantic_output_rejection_is_logged(self):
        call=ToolCall(id='contract-breach',name='search_evidence',arguments={'query':'synthetic','top_k':1})
        client=FakeLLMClient([ModelTurn(tool_calls=[call]),chat_final()])
        with captured() as logs:
            result=ChatExecutionV1(client,lambda context:registry(lambda c,a:package(['a','b'])))(CONTEXT,'{}')
        text=logs.getvalue()
        self.assertEqual(result.execution.status,'completed')
        self.assertEqual([e.code for e in result.execution.errors],['invalid_output'])
        self.assertFalse(result.execution.called_tools[0].success)
        self.assertEqual(result.execution.called_tools[0].error_type,'invalid_output')
        self.assertEqual(result.sources,[])
        self.assertIn('success=True',text)
        self.assertIn('agent_error',text)
        self.assertIn('retrieval_limit_exceeded',text)
        self.assertIn("'returned_count': 2",text)
        self.assertIn("'top_k': 1",text)
        self.assertIn('error_code=invalid_output',text)

    def test_chat_final_validation_identifies_model_field_and_origin(self):
        client=FakeLLMClient([ModelTurn(text=json.dumps({'answer':'synthetic','selected':'SYNTHETIC_PRIVATE_VALUE'}))])
        with captured() as logs:
            result=AgentLoopRunner(client,ToolRegistry()).run_structured('{}',CONTEXT,output_model=ChatAnswerV1)
        text=logs.getvalue()
        self.assertEqual(result.execution.status,'invalid_response')
        self.assertEqual([e.code for e in result.execution.errors],['invalid_structured_output'])
        self.assertIn('stage=final_output_validation',text)
        self.assertIn('error_code=invalid_structured_output',text)
        self.assertIn('input_origin=provider_output',text)
        self.assertIn("'validation_model': 'ChatDraft'",text)
        self.assertIn("'field': 'selected'",text)
        self.assertNotIn('SYNTHETIC_PRIVATE_VALUE',text)

    def test_wrong_evidence_type_is_logged_without_raw_evidence(self):
        from app.ai.agent.executor import diagnostic_call_id
        evidence = package(['a'])
        evidence.evidence[0].evidence_type = 'guideline'
        evidence.evidence[0].title = 'SYNTHETIC_PRIVATE_VALUE'
        call = ToolCall(id='type-breach', name='search_evidence', arguments={'query':'synthetic','top_k':1})
        client = FakeLLMClient([ModelTurn(tool_calls=[call]), chat_final()])
        with captured() as logs:
            result = ChatExecutionV1(client, lambda c: registry(lambda c,a: evidence))(CONTEXT, '{}')
        self.assertEqual([e.code for e in result.execution.errors], ['invalid_output'])
        self.assertIn('evidence_type_mismatch', logs.getvalue())
        self.assertIn('tool_name=search_evidence', logs.getvalue())
        self.assertIn(diagnostic_call_id(call.id), logs.getvalue())
        self.assertNotIn('SYNTHETIC_PRIVATE_VALUE', logs.getvalue())

    def test_malformed_final_json_reports_only_syntax_location(self):
        client = FakeLLMClient([ModelTurn(text='{"answer":SYNTHETIC_PRIVATE_VALUE}')])
        with captured() as logs:
            result = AgentLoopRunner(client, ToolRegistry()).run_structured('{}', CONTEXT, output_model=ChatAnswerV1)
        self.assertEqual(result.execution.errors[-1].code, 'invalid_structured_output')
        self.assertIn('invalid_json_syntax', logs.getvalue())
        self.assertIn("'position':", logs.getvalue())
        self.assertIn('input_origin=provider_output', logs.getvalue())
        self.assertNotIn('SYNTHETIC_PRIVATE_VALUE', logs.getvalue())

class ProviderDiagnosticPrivacy(unittest.TestCase):
    def test_provider_error_echo_is_not_written_to_warning_log(self):
        import httpx
        from openai import OpenAI
        from app.ai.agent.clients.openai_client import OpenAIClient
        from app.api.ai_failure_log import ErrorRecordingClient,logged_execution
        marker='SYNTHETIC_PATIENT_MEDICAL_NOTE'
        def transport(request):
            return httpx.Response(400,json={'error':{'message':'Invalid request: '+marker,
                'type':'invalid_request_error','code':'bad_input'}})
        with OpenAI(api_key='offline-placeholder',http_client=httpx.Client(transport=httpx.MockTransport(transport))) as sdk:
            runner=AgentLoopRunner(ErrorRecordingClient(OpenAIClient(sdk,model='offline-model')),ToolRegistry())
            run=logged_execution('audit',lambda c,p:runner.run_structured(p,c,output_model=ChatAnswerV1))
            with captured('app.api.ai_failure_log') as logs:
                result=run(CONTEXT,marker)
            self.assertEqual(result.execution.status,'provider_error')
            self.assertNotIn(marker,logs.getvalue())
            self.assertIn("BadRequestError status=400 code=unknown",logs.getvalue())
            self.assertNotIn(marker,result.model_dump_json())

