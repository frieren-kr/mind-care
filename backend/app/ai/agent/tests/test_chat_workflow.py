import ast
import inspect
import json
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

from ..agent_loop_runner import AgentLoopRunner
from ..executor import ToolExecutor
from ..outputs import FeedAnswerV1, StructuredAgentResult
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..schemas import ModelTurn
from ..testing.chat_workflow_fakes import FakeChatDependencies, STAGES, chat_call, chat_final
from ..testing.fake_llm import FakeLLMClient
from ..testing.fake_tools import fake_search_evidence
from ..workflows import chat
from ..workflows.chat import ChatWorkflow, ChatWorkflowError
from .test_chat_execution import CONTEXT


def reference(identifier="search_1", index=0):
    return {"tool_call_id": identifier, "evidence_index": index}


def search(identifier="search_1", query="Dementia sleep research"):
    return chat_call(ToolName.SEARCH_EVIDENCE, identifier=identifier, args={"query": query})


def outcomes(deps, request=-1):
    return [(m.tool_call_id, json.loads(m.content)) for m in deps.client.requests[request][0] if m.role == "tool"]


class ChatWorkflowTests(unittest.TestCase):
    def assert_stopped(self, deps, *, context=CONTEXT, stage="backend_handoff", question="합성 질문"):
        with self.assertRaises(ChatWorkflowError) as caught:
            deps.workflow().run(context, question)
        self.assertEqual(caught.exception.stage, stage)
        self.assertEqual(deps.saved, [])
        self.assertNotIn("PRIVATE", str(caught.exception))
        return deps.agent_result

    def test_direct_answer_passes_three_stages_and_one_real_runner_entry(self):
        deps = FakeChatDependencies([chat_final()])
        original = AgentLoopRunner.run_structured
        with patch.object(AgentLoopRunner, "run_structured", autospec=True, side_effect=original) as run:
            receipt = deps.workflow().run(CONTEXT, "  안녕하세요.\n ")
        self.assertIs(receipt, deps.receipt)
        run.assert_called_once()
        self.assertEqual(deps.events, list(STAGES))
        self.assertEqual(deps.tool_events, [])
        self.assertEqual(json.loads(deps.prepared)["user_message"], "  안녕하세요.\n ")
        self.assertEqual(deps.saved[0].output.citation_refs, [])
        self.assertEqual(deps.saved[0].sources, [])
        self.assertEqual(deps.saved_owners, [CONTEXT.user_id])
        self.assertEqual(deps.saved[0], deps.agent_result)
        self.assertIsNot(deps.saved[0], deps.agent_result)

    def test_invalid_question_stops_before_registry_provider_and_handoff(self):
        for question in ("", " \n", None, 123, "가" * 4001):
            deps = FakeChatDependencies([chat_final()])
            self.assert_stopped(deps, stage="context_prepare", question=question)
            self.assertEqual(deps.events, ["context_prepare"])
            self.assertEqual(deps.factory_contexts, [])
            self.assertEqual(deps.client.requests, [])
            self.assertEqual(deps.tool_events, [])

    def test_each_stage_exception_stops_later_stages_and_hides_details(self):
        for index, stage in enumerate(STAGES):
            deps = FakeChatDependencies([chat_final()], fail_at=stage)
            with self.assertRaises(ChatWorkflowError) as caught:
                deps.workflow().run(CONTEXT, "질문")
            self.assertEqual(caught.exception.stage, stage)
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertNotIn("PRIVATE", str(caught.exception))
            self.assertEqual(deps.events, list(STAGES[:index + 1]))
            self.assertEqual(deps.saved, [])
            self.assertEqual(len(deps.client.requests), 1 if stage == "backend_handoff" else 0)

    def test_invalid_prepared_dependency_output_blocks_execution(self):
        for value in (None, {}, "", "not JSON", "[]", "null"):
            deps = FakeChatDependencies([chat_final()])
            workflow = deps.workflow()
            workflow.context_prepare = lambda context, question: value
            with self.assertRaises(ChatWorkflowError) as caught:
                workflow.run(CONTEXT, "질문")
            self.assertEqual(caught.exception.stage, "context_prepare")
            self.assertEqual(deps.client.requests, [])
            self.assertEqual(deps.saved, [])

    def test_single_patient_tool_result_is_passed_to_followup(self):
        for name, key in ((ToolName.PATIENT_PROFILE, "symptoms"), (ToolName.RECENT_CARE_LOGS, "logs")):
            deps = FakeChatDependencies([ModelTurn(tool_calls=[chat_call(name)]),
                                        chat_final("조회한 저장 기록이며 확정 진단을 뜻하지 않습니다.")])
            self.assertIs(deps.workflow().run(CONTEXT, "등록된 정보가 뭐야?"), deps.receipt)
            self.assertEqual(len(deps.tool_events), 1)
            self.assertEqual(deps.tool_events[0][:2], (name.value, CONTEXT))
            self.assertEqual(len(deps.client.requests), 2)
            entries = outcomes(deps)
            self.assertEqual(entries[0][0], "profile_1")
            self.assertTrue(entries[0][1]["success"])
            self.assertIn(key, entries[0][1]["data"])
            self.assertEqual((deps.agent_result.execution.tool_rounds, deps.agent_result.execution.total_tool_calls), (1, 1))

    def test_search_and_resolved_citation_work_without_patient_scope(self):
        context = CONTEXT.model_copy(update={"patient_id": None})
        deps = FakeChatDependencies([ModelTurn(tool_calls=[search()]), chat_final(refs=[reference()])])
        self.assertIs(deps.workflow().run(context, "최근 연구를 알려줘"), deps.receipt)
        self.assertEqual([d["function"]["name"] for d in deps.client.requests[0][1]], ["search_evidence"])
        self.assertEqual(deps.saved[0].sources[0].pmid, "FAKE-PMID")
        self.assertEqual(deps.saved[0].sources[0].source_ref, deps.saved[0].output.citation_refs[0])

    def test_same_turn_three_tools_execute_once_before_one_followup_llm_call(self):
        calls = [chat_call(identifier="profile_1"),
                 chat_call(ToolName.RECENT_CARE_LOGS, identifier="logs_1"), search()]
        deps = FakeChatDependencies([ModelTurn(tool_calls=calls), chat_final(refs=[reference()])])
        self.assertIs(deps.workflow().run(CONTEXT, "저장 정보와 연구를 알려줘"), deps.receipt)
        self.assertEqual(deps.events, list(STAGES))
        self.assertEqual([e[0] for e in deps.tool_events], [c.name for c in calls])
        self.assertEqual([e[3] for e in deps.tool_events], [1, 1, 1])
        self.assertEqual(len(deps.client.requests), 2)
        self.assertEqual([call.id for call in next(m for m in deps.client.requests[1][0] if m.role == "assistant").tool_calls],
                         [c.id for c in calls])
        entries = outcomes(deps)
        self.assertEqual([identifier for identifier, _ in entries], [c.id for c in calls])
        self.assertTrue(all(value["success"] for _, value in entries))
        self.assertIn("symptoms", entries[0][1]["data"])
        self.assertIn("logs", entries[1][1]["data"])
        self.assertEqual(entries[2][1]["data"]["evidence"][0]["pmid"], "FAKE-PMID")
        result = deps.saved[0]
        self.assertEqual((result.execution.tool_rounds, result.execution.total_tool_calls), (1, 3))
        self.assertEqual(result.sources[0].source_ref.tool_call_id, "search_1")

    def test_followup_tool_choice_observes_prior_profile_result(self):
        test = self
        class ProfileDrivenFake(FakeLLMClient):
            def generate(self, messages, tools):
                if len(self.requests) == 1:
                    value = json.loads(next(m.content for m in messages if m.role == "tool"))
                    test.assertEqual(value["data"]["symptoms"], ["수면 변화"])
                    self.turns.append(ModelTurn(tool_calls=[search(query="Dementia sleep changes research")]))
                elif len(self.requests) == 2:
                    test.assertEqual([m.tool_call_id for m in messages if m.role == "tool"], ["profile_1", "search_1"])
                    self.turns.append(chat_final(refs=[reference()]))
                return super().generate(messages, tools)
        deps = FakeChatDependencies(client=ProfileDrivenFake([ModelTurn(tool_calls=[chat_call()])]))
        self.assertIs(deps.workflow().run(CONTEXT, "상태에 맞는 연구를 알려줘"), deps.receipt)
        self.assertEqual(len(deps.client.requests), 3)
        self.assertEqual([e[0] for e in deps.tool_events], ["get_patient_profile", "search_evidence"])
        self.assertEqual(deps.tool_events[1][2].query, "Dementia sleep changes research")
        self.assertEqual((deps.agent_result.execution.tool_rounds, deps.agent_result.execution.total_tool_calls), (2, 2))

    def test_initial_context_minimization_and_tool_results_are_never_system_policy(self):
        deps = FakeChatDependencies([ModelTurn(tool_calls=[chat_call(), search()]), chat_final(refs=[reference()])])
        question = "  PRIVATE user question \n"
        deps.workflow().run(CONTEXT, question)
        initial = deps.client.requests[0][0]
        self.assertEqual([(m.role, m.kind) for m in initial],
                         [("system", "policy"), ("system", "policy"), ("user", "user_input")])
        self.assertEqual(json.loads(initial[-1].content), {"user_message": question, "patient_scope_available": True})
        self.assertFalse(any(m.kind == "tool_result" for m in initial))
        for messages, _ in deps.client.requests:
            text = json.dumps([m.model_dump(mode="json") for m in messages], ensure_ascii=False)
            for identifier in (CONTEXT.request_id, str(CONTEXT.user_id), str(CONTEXT.patient_id)):
                self.assertNotIn(identifier, text)
            self.assertFalse(any(m.kind == "agent_context" for m in messages))
            self.assertTrue(all(m.role == "tool" for m in messages if m.kind == "tool_result"))
            system = "\n".join(m.content for m in messages if m.role == "system")
            self.assertNotIn("합성 테스트 환자", system)
            self.assertNotIn("FAKE-PMID", system)

    def test_none_scope_unexposed_patient_call_never_runs_handler(self):
        context = CONTEXT.model_copy(update={"patient_id": None})
        for name in (ToolName.PATIENT_PROFILE, ToolName.RECENT_CARE_LOGS):
            handler = Mock(side_effect=AssertionError("Must not run"))
            deps = FakeChatDependencies([ModelTurn(tool_calls=[chat_call(name)]),
                                        chat_final("환자 선택이 없어 조회하지 못했습니다.")], handlers={name: handler})
            deps.workflow().run(context, "환자 정보")
            handler.assert_not_called()
            value = outcomes(deps)[0][1]
            self.assertFalse(value["success"])
            self.assertIsNone(value["data"])
            self.assertEqual(value["error"]["code"], "unknown_tool")

    def test_existing_executor_none_scope_guard_remains_for_misassembled_registry(self):
        context = CONTEXT.model_copy(update={"patient_id": None})
        for name in (ToolName.PATIENT_PROFILE, ToolName.RECENT_CARE_LOGS):
            handler = Mock()
            registry = ToolRegistry((ToolSpec(TOOL_CONTRACTS[name], handler, test_only=True),), mode="test")
            outcome, _ = ToolExecutor(registry).execute(chat_call(name), context)
            handler.assert_not_called()
            self.assertEqual(outcome.error.code, "patient_context_mismatch")
            self.assertIsNone(outcome.data)

    def test_uncontracted_patient_identity_output_is_rejected_and_not_used_as_success(self):
        # Current patient Tool DTOs deliberately lack patient_id. Handler auth is
        # still required: Executor cannot detect a valid-shaped foreign profile.
        deps = FakeChatDependencies([ModelTurn(tool_calls=[chat_call()]), chat_final("조회에 실패했습니다.")],
            handlers={ToolName.PATIENT_PROFILE: lambda context, args: {
                "patient_id": str(UUID(int=999)), "name": "PRIVATE wrong patient"}})
        deps.workflow().run(CONTEXT, "환자 정보")
        value = outcomes(deps)[0][1]
        self.assertFalse(value["success"])
        self.assertEqual(value["error"]["code"], "invalid_output")
        self.assertIsNone(value["data"])
        self.assertNotIn("PRIVATE wrong patient", json.dumps([m.model_dump() for m in deps.client.requests[-1][0]]))

    def test_handler_ownership_rejection_cannot_supply_foreign_patient_data(self):
        # Simulate the Backend authorization seam, not real DB authorization.
        context = CONTEXT.model_copy(update={"patient_id": UUID(int=999)})
        def authorized_read(context, args):
            if context.patient_id != CONTEXT.patient_id:
                raise PermissionError("PRIVATE foreign patient")
            return {"symptoms": ["stored synthetic signal"]}
        handler = Mock(side_effect=authorized_read)
        deps = FakeChatDependencies([ModelTurn(tool_calls=[chat_call()]), chat_final("조회 권한을 확인해야 합니다.")],
            handlers={ToolName.PATIENT_PROFILE: handler})
        deps.workflow().run(context, "환자 정보")
        self.assertEqual(handler.call_args.args[0], context)
        value = outcomes(deps)[0][1]
        self.assertFalse(value["success"])
        self.assertIsNone(value["data"])
        self.assertEqual(value["error"]["code"], "tool_exception")
        self.assertNotIn("PRIVATE foreign", json.dumps(value))
        self.assertEqual(deps.saved[0].execution.errors[0].code, "tool_exception")

    def test_recoverable_tool_errors_keep_existing_runner_semantics(self):
        cases = ((chat_call("unknown_tool"), {}, "unknown_tool"),
                 (chat_call(args={"patient_id": "PRIVATE"}), {}, "invalid_arguments"),
                 (chat_call(), {ToolName.PATIENT_PROFILE: Mock(side_effect=RuntimeError("PRIVATE"))}, "tool_exception"))
        for call, handlers, error in cases:
            deps = FakeChatDependencies([ModelTurn(tool_calls=[call]), chat_final("조회 오류를 확인했습니다.")], handlers=handlers)
            deps.workflow().run(CONTEXT, "질문")
            self.assertEqual(deps.saved[0].execution.status, "completed")
            self.assertEqual(deps.saved[0].execution.errors[0].code, error)
            self.assertFalse(outcomes(deps)[0][1]["success"])
            self.assertEqual(len(deps.tool_events), 1 if error == "tool_exception" else 0)

    def test_handler_timeout_stops_before_followup_and_handoff(self):
        deps = FakeChatDependencies([ModelTurn(tool_calls=[chat_call()]), chat_final()],
            handlers={ToolName.PATIENT_PROFILE: Mock(side_effect=TimeoutError("PRIVATE timeout"))})
        result = self.assert_stopped(deps)
        self.assertEqual(result.execution.status, "failed")
        self.assertEqual(result.execution.errors[-1].code, "tool_timeout")
        self.assertEqual(len(deps.client.requests), 1)

    def test_actual_handler_wait_timeout_is_reused(self):
        release = threading.Event()
        started = threading.Event()
        def blocked(context, args):
            started.set()
            release.wait(1)
            return {}
        deps = FakeChatDependencies([ModelTurn(tool_calls=[chat_call()]), chat_final()],
            handlers={ToolName.PATIENT_PROFILE: blocked}, tool_timeout_seconds=0.02)
        try:
            result = self.assert_stopped(deps)
            self.assertTrue(started.is_set())
            self.assertEqual(result.execution.errors[-1].code, "tool_timeout")
            self.assertEqual(len(deps.client.requests), 1)
        finally:
            release.set()

    def test_sixth_round_is_blocked_after_five_executions(self):
        deps = FakeChatDependencies([ModelTurn(tool_calls=[chat_call(identifier=f"p_{i}")]) for i in range(6)])
        result = self.assert_stopped(deps)
        self.assertEqual(result.execution.status, "limit_reached")
        self.assertEqual((result.execution.tool_rounds, result.execution.total_tool_calls), (5, 5))
        self.assertEqual(len(deps.tool_events), 5)

    def test_over_budget_batch_blocks_all_calls(self):
        deps = FakeChatDependencies([ModelTurn(tool_calls=[chat_call(identifier=f"p_{i}") for i in range(11)])])
        result = self.assert_stopped(deps)
        self.assertEqual(result.execution.status, "limit_reached")
        self.assertEqual(result.execution.total_tool_calls, 0)
        self.assertEqual(deps.tool_events, [])

    def test_cumulative_budget_rejects_whole_batch_after_prior_calls(self):
        deps = FakeChatDependencies([
            ModelTurn(tool_calls=[chat_call(identifier=f"p_{i}") for i in range(6)]),
            ModelTurn(tool_calls=[chat_call(identifier=f"p_{i}") for i in range(6, 11)]),
        ])
        result = self.assert_stopped(deps)
        self.assertEqual(result.execution.status, "limit_reached")
        self.assertEqual(result.execution.total_tool_calls, 6)
        self.assertEqual(len(deps.tool_events), 6)
        self.assertEqual(len(deps.client.requests), 2)

    def test_request_deadline_stops_provider_wait_and_never_handoffs(self):
        release = threading.Event()
        class WaitingProvider(FakeLLMClient):
            def generate(self, messages, tools):
                release.wait(1)
                return super().generate(messages, tools)
        deps = FakeChatDependencies(client=WaitingProvider([chat_final()]), request_timeout_seconds=0.03)
        try:
            result = self.assert_stopped(deps)
            self.assertEqual(result.execution.status, "failed")
            self.assertEqual(result.execution.errors[-1].code, "request_timeout")
            self.assertIsNone(result.output)
            self.assertEqual(result.sources, [])
        finally:
            release.set()

    def test_final_answer_is_allowed_after_exact_total_budget(self):
        turns = [ModelTurn(tool_calls=[chat_call(identifier=f"p_{i}") for i in range(5)]),
                 ModelTurn(tool_calls=[chat_call(identifier=f"p_{i}") for i in range(5, 10)]), chat_final()]
        deps = FakeChatDependencies(turns)
        deps.workflow().run(CONTEXT, "질문")
        self.assertEqual(deps.saved[0].execution.total_tool_calls, 10)
        self.assertEqual(deps.saved[0].execution.tool_rounds, 2)
        self.assertEqual(len(deps.client.requests), 3)

    def test_duplicate_ids_in_same_or_followup_turn_do_not_repeat_handlers(self):
        for turns, expected in (([ModelTurn(tool_calls=[chat_call(), chat_call()])], 0),
                                ([ModelTurn(tool_calls=[chat_call()]), ModelTurn(tool_calls=[chat_call()])], 1)):
            deps = FakeChatDependencies(turns)
            result = self.assert_stopped(deps)
            self.assertEqual(result.execution.status, "invalid_response")
            self.assertEqual(result.execution.errors[-1].code, "duplicate_call_id")
            self.assertEqual(len(deps.tool_events), expected)

    def test_invalid_structured_output_or_evidence_reference_never_reaches_handoff(self):
        cases = ([ModelTurn(text="PRIVATE not JSON")],
                 [ModelTurn(text='{"schema_version":"1","response_type":"feed","items":[]}')],
                 [chat_final(answer=" ")], [chat_final(refs=[reference("missing")])],
                 [ModelTurn(tool_calls=[search()]), chat_final(refs=[reference(index=1)])],
                 [ModelTurn(tool_calls=[chat_call()]), chat_final(refs=[reference("profile_1")])],
                 [RuntimeError("PRIVATE provider credential")])
        for turns in cases:
            deps = FakeChatDependencies(turns)
            result = self.assert_stopped(deps)
            self.assertNotEqual(result.execution.status, "completed")
            self.assertIsNone(result.output)
            self.assertEqual(result.sources, [])
            self.assertEqual(deps.events, list(STAGES[:2]))

    def test_failed_search_cannot_be_cited(self):
        deps = FakeChatDependencies([ModelTurn(tool_calls=[search()]), chat_final(refs=[reference()])],
            handlers={ToolName.SEARCH_EVIDENCE: Mock(side_effect=RuntimeError("PRIVATE"))})
        result = self.assert_stopped(deps)
        self.assertEqual(result.execution.status, "invalid_response")
        self.assertEqual([e.code for e in result.execution.errors], ["tool_exception", "invalid_structured_output"])
        self.assertEqual(result.sources, [])

    def test_handoff_revalidates_nested_models_request_and_legacy_answer(self):
        mutations = (
            lambda r: setattr(r.execution, "request_id", "other-request"),
            lambda r: setattr(r.execution, "final_answer", "different answer"),
            lambda r: setattr(r.output, "answer", 123),
            lambda r: setattr(r.output.citation_refs[0], "evidence_index", "bad"),
            lambda r: setattr(r.sources[0].source_ref, "tool_call_id", "different"),
            lambda r: setattr(r.sources[0], "full_text_available", "bad bool"),
        )
        for mutate in mutations:
            deps = FakeChatDependencies([ModelTurn(tool_calls=[search()]), chat_final(refs=[reference()])])
            workflow = deps.workflow()
            def altered(context, prepared):
                result = deps.run_chat(context, prepared)
                mutate(result)
                return result
            workflow.chat_execution = altered
            with self.assertRaises(ChatWorkflowError) as caught:
                workflow.run(CONTEXT, "질문")
            self.assertEqual(caught.exception.stage, "backend_handoff")
            self.assertEqual(deps.saved, [])

    def test_completed_feed_result_is_rejected_at_chat_handoff(self):
        deps = FakeChatDependencies([chat_final()])
        result = deps.execution(CONTEXT, '{}')
        result.output = FeedAnswerV1(schema_version="1", response_type="feed", items=[])
        result.execution.final_answer = None
        result = StructuredAgentResult.model_validate(result)
        workflow = deps.workflow()
        workflow.chat_execution = lambda context, prepared: result
        with self.assertRaises(ChatWorkflowError) as caught:
            workflow.run(CONTEXT, "질문")
        self.assertEqual(caught.exception.stage, "backend_handoff")
        self.assertEqual(deps.saved, [])

    def test_raw_or_failed_results_cannot_become_success_receipts(self):
        deps = FakeChatDependencies([RuntimeError("PRIVATE provider")])
        failed = deps.execution(CONTEXT, '{}')
        for result in (None, failed.model_dump(), failed):
            workflow = deps.workflow()
            workflow.chat_execution = lambda context, prepared: result
            with self.assertRaises(ChatWorkflowError):
                workflow.run(CONTEXT, "질문")
            self.assertEqual(deps.saved, [])

    def test_handoff_nested_mutation_cannot_change_original_runner_result(self):
        deps = FakeChatDependencies([ModelTurn(tool_calls=[search()]), chat_final(refs=[reference()])])
        workflow = deps.workflow()
        def handoff(context, result):
            before = deps.agent_result.model_dump()
            self.assertIsNot(result.output, deps.agent_result.output)
            self.assertIsNot(result.sources[0], deps.agent_result.sources[0])
            result.output.answer = "Backend modified"
            result.sources[0].title = "Backend modified title"
            result.sources[0].source_ref.evidence_index = 9
            self.assertEqual(deps.agent_result.model_dump(), before)
            return deps.receipt
        workflow.backend_handoff = handoff
        self.assertIs(workflow.run(CONTEXT, "질문"), deps.receipt)

    def test_handoff_dependency_is_required_without_fake_success_default(self):
        deps = FakeChatDependencies()
        with self.assertRaises(TypeError):
            ChatWorkflow(context_prepare=deps.prepare, chat_execution=deps.run_chat)


class ChatArchitectureTests(unittest.TestCase):
    def test_exact_three_stages_without_runner_creation_or_private_tool_loop(self):
        from typing import get_args
        self.assertEqual(get_args(chat.ChatStage), STAGES)
        tree = ast.parse(inspect.getsource(chat))
        self.assertFalse(any(isinstance(n, (ast.While, ast.For)) for n in ast.walk(tree)))
        self.assertFalse(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                             and n.func.id in ("AgentLoopRunner", "FeedWorkflow") for n in ast.walk(tree)))
        self.assertEqual(inspect.signature(AgentLoopRunner).parameters["max_tool_rounds"].default, 5)
        self.assertEqual(inspect.signature(AgentLoopRunner).parameters["max_total_tool_calls"].default, 10)
