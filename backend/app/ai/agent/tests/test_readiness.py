"""Production readiness 회귀 검증. 외부 DB/API/임상 규칙은 사용하지 않는다."""
from dataclasses import replace
from threading import BoundedSemaphore, Event, Thread
from time import monotonic, sleep
from unittest import TestCase
from unittest.mock import patch
from uuid import UUID

import httpx
from openai import APITimeoutError
from pydantic import BaseModel, ValidationError

from .. import _execution
from ..executor import ToolExecutor
from ..agent_loop_runner import AgentLoopRunner
from ..prompts import DefaultPromptBuilder
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..schemas import AgentResult, ModelTurn, ToolOutcome, ToolTrace
from ..testing.fake_llm import FakeLLMClient
from ..testing.fake_tools import fake_registry
from ..tools import contracts as c
from .test_workflows import CONTEXT, call, tool_turn


def registry_with(handler, name=ToolName.PATIENT_PROFILE):
    return ToolRegistry((ToolSpec(TOOL_CONTRACTS[name], handler),))


class TimeoutTests(TestCase):
    def test_handler_timeout_types_stop_remaining_batch_and_llm(self):
        request = httpx.Request("GET", "https://synthetic.invalid")
        exceptions = [TimeoutError("PRIVATE timeout"),
                      httpx.ConnectTimeout("PRIVATE", request=request),
                      httpx.ReadTimeout("PRIVATE", request=request),
                      httpx.WriteTimeout("PRIVATE", request=request),
                      httpx.PoolTimeout("PRIVATE", request=request), APITimeoutError(request=request)]
        for exception in exceptions:
            with self.subTest(exception=type(exception).__name__):
                started = []
                def handler(context, args):
                    started.append(True)
                    raise exception
                client = FakeLLMClient([tool_turn(call(), call(identifier="call_2")), ModelTurn(text="must not run")])
                result = AgentLoopRunner(client, registry_with(handler)).run("synthetic", CONTEXT)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.errors[-1].code, "tool_timeout")
                self.assertEqual(result.total_tool_calls, 1)
                self.assertEqual(len(started), 1)
                self.assertEqual(len(client.requests), 1)
                self.assertIsNone(result.final_answer)
                self.assertNotIn("PRIVATE", result.model_dump_json())

    def test_non_timeout_errors_remain_recoverable(self):
        for exception in (RuntimeError("timeout mentioned in text"),
                          httpx.ConnectError("timeout mentioned in text")):
            with self.subTest(exception=type(exception).__name__):
                started = []
                def handler(context, args):
                    started.append(True)
                    raise exception
                client = FakeLLMClient([tool_turn(call(), call(identifier="call_2")), ModelTurn(text="recovered")])
                result = AgentLoopRunner(client, registry_with(handler)).run("synthetic", CONTEXT)
                self.assertEqual(result.status, "completed")
                self.assertEqual([error.code for error in result.errors], ["tool_exception", "tool_exception"])
                self.assertEqual(len(started), 2)
                self.assertEqual(len(client.requests), 2)

    def test_direct_handler_timeout_drops_result_and_does_not_wait_for_thread_exit(self):
        release, finished = Event(), Event()
        def handler(context, args):
            release.wait(2)
            finished.set()
            return c.PatientProfileOutput()
        try:
            started = monotonic()
            outcome, trace = ToolExecutor(registry_with(handler), timeout_seconds=0.03).execute(call(), CONTEXT)
            self.assertLess(monotonic() - started, 0.5)
            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error.code, "tool_timeout")
            self.assertEqual(trace.error_type, "tool_timeout")
            self.assertIsNone(outcome.data)
            self.assertFalse(finished.is_set())
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
        self.assertIsNone(outcome.data)

    def test_tool_timeout_stops_followup_and_remaining_batch(self):
        release = Event()
        calls = []
        def handler(context, args):
            calls.append(context.patient_id)
            release.wait(2)
            return c.PatientProfileOutput()
        client = FakeLLMClient([tool_turn(call(), call(identifier="call_2")), ModelTurn(text="must not run")])
        try:
            result = AgentLoopRunner(client, registry_with(handler), tool_timeout_seconds=0.03,
                                       request_timeout_seconds=1).run("synthetic", CONTEXT)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.errors[-1].code, "tool_timeout")
            self.assertEqual(result.total_tool_calls, 1)
            self.assertEqual(len(client.requests), 1)
            self.assertEqual(len(calls), 1)
            self.assertIsNone(result.final_answer)
        finally:
            release.set()

    def test_request_deadline_includes_provider_wait(self):
        release = Event()
        class SlowClient:
            def generate(self, messages, tools):
                release.wait(2)
                return ModelTurn(text="late answer")
        try:
            started = monotonic()
            result = AgentLoopRunner(SlowClient(), fake_registry(), request_timeout_seconds=0.04).run("synthetic", CONTEXT)
            self.assertLess(monotonic() - started, 0.5)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.errors[-1].code, "request_timeout")
            self.assertIsNone(result.final_answer)
            self.assertIsNone(_execution.request_deadline.get())
        finally:
            release.set()

    def test_request_deadline_wins_over_longer_tool_timeout(self):
        release = Event()
        def handler(context, args):
            release.wait(2)
            return c.PatientProfileOutput()
        client = FakeLLMClient([tool_turn(call())])
        try:
            result = AgentLoopRunner(client, registry_with(handler), tool_timeout_seconds=1,
                                       request_timeout_seconds=0.05).run("synthetic", CONTEXT)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.errors[-1].code, "request_timeout")
            self.assertIsNone(result.final_answer)
        finally:
            release.set()

    def test_request_budget_is_cumulative_across_rounds(self):
        class DelayedClient(FakeLLMClient):
            def generate(self, messages, tools):
                sleep(0.12)
                return super().generate(messages, tools)
        client = DelayedClient([tool_turn(call()), ModelTurn(text="late answer")])
        result = AgentLoopRunner(client, fake_registry(), request_timeout_seconds=0.2).run("synthetic", CONTEXT)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[-1].code, "request_timeout")
        self.assertEqual(result.total_tool_calls, 1)

    def test_slow_builder_is_covered_by_request_deadline(self):
        release = Event()
        class SlowBuilder(DefaultPromptBuilder):
            def build_initial_messages(self, user_input, context):
                release.wait(2)
                return super().build_initial_messages(user_input, context)
        try:
            result = AgentLoopRunner(FakeLLMClient([]), fake_registry(), prompt_builder=SlowBuilder(),
                                       request_timeout_seconds=0.03).run("synthetic", CONTEXT)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.errors[-1].code, "request_timeout")
        finally:
            release.set()

    def test_worker_capacity_is_bounded_and_expired_work_does_not_start(self):
        calls = []
        semaphore = BoundedSemaphore(1)
        semaphore.acquire()
        with patch.object(_execution, "_WORKER_SLOTS", semaphore):
            with self.assertRaises(_execution.ExecutionTimeout):
                _execution.bounded_call(lambda: calls.append("ran"), 0.02)
        semaphore.release()
        self.assertEqual(calls, [])

    def test_handler_capacity_is_separate_from_executor_capacity(self):
        client = FakeLLMClient([tool_turn(call()), ModelTurn(text="final")])
        with patch.object(_execution, "_WORKER_SLOTS", BoundedSemaphore(1)):
            result = AgentLoopRunner(client, fake_registry(), request_timeout_seconds=1).run("synthetic", CONTEXT)
        self.assertEqual(result.status, "completed")

    def test_deadline_isolated_between_concurrent_runs(self):
        release, started = Event(), Event()
        class SlowClient:
            def generate(self, messages, tools):
                started.set()
                release.wait(2)
                return ModelTurn(text="late")
        results = []
        thread = Thread(target=lambda: results.append(AgentLoopRunner(
            SlowClient(), fake_registry(), request_timeout_seconds=0.05).run("synthetic", CONTEXT)))
        try:
            thread.start()
            self.assertTrue(started.wait(1))
            fast = AgentLoopRunner(FakeLLMClient([ModelTurn(text="fast")]), fake_registry(),
                                     request_timeout_seconds=1).run("synthetic", CONTEXT)
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(fast.status, "completed")
            self.assertEqual(results[0].status, "failed")
            self.assertIsNone(_execution.request_deadline.get())
        finally:
            release.set()
            thread.join(1)


class FailureBoundaryTests(TestCase):
    def assert_failed(self, engine, expected):
        result = engine.run("synthetic", CONTEXT)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.errors[-1].code, expected)
        self.assertIsNone(result.final_answer)
        self.assertNotIn("PRIVATE", result.model_dump_json())
        return result

    def test_each_prompt_builder_failure_returns_failed(self):
        for method in ("build_initial_messages", "build_final_response_context", "build_tool_followup_messages"):
            with self.subTest(method=method):
                builder = DefaultPromptBuilder()
                with patch.object(builder, method, side_effect=RuntimeError("PRIVATE prompt")):
                    engine = AgentLoopRunner(FakeLLMClient([tool_turn(call()), ModelTurn(text="final")]),
                                               fake_registry(), prompt_builder=builder)
                    self.assert_failed(engine, "prompt_builder_error")

    def test_executor_failure_returns_failed(self):
        engine = AgentLoopRunner(FakeLLMClient([tool_turn(call())]), fake_registry())
        with patch.object(engine.executor, "execute", side_effect=RuntimeError("PRIVATE executor")):
            self.assert_failed(engine, "executor_error")

    def test_malformed_executor_results_do_not_escape_or_continue(self):
        valid_outcome = ToolOutcome(call_id="call_1", tool_name=ToolName.PATIENT_PROFILE.value,
                                   success=True, data=c.PatientProfileOutput().model_dump(mode="json"))
        valid_trace = ToolTrace(tool_name=ToolName.PATIENT_PROFILE.value, success=True, duration_ms=0)
        invalid = [(valid_outcome, {"PRIVATE": "invalid"}), ({"PRIVATE": "invalid"}, valid_trace),
                   (valid_outcome, ToolTrace.model_construct(tool_name="PRIVATE", success=True, duration_ms=float("nan"))),
                   (ToolOutcome.model_construct(call_id="call_1", tool_name=ToolName.PATIENT_PROFILE.value,
                                                success=True, data="PRIVATE"), valid_trace),
                   (valid_outcome.model_copy(update={"call_id": "wrong_call"}), valid_trace),
                   (valid_outcome.model_copy(update={"tool_name": "wrong_tool"}), valid_trace),
                   (valid_outcome.model_copy(update={"success": False}), valid_trace),
                   (valid_outcome, valid_trace.model_copy(update={"success": False})),
                   None, (valid_outcome,)]
        for returned in invalid:
            with self.subTest(returned=type(returned).__name__):
                client = FakeLLMClient([tool_turn(call(), call(identifier="call_2")), ModelTurn(text="must not run")])
                engine = AgentLoopRunner(client, fake_registry())
                with patch.object(engine.executor, "execute", return_value=returned) as execute:
                    self.assert_failed(engine, "executor_error")
                self.assertEqual(execute.call_count, 1)
                self.assertEqual(len(client.requests), 1)

    def test_result_creation_failure_has_a_valid_sanitized_fallback(self):
        client = FakeLLMClient([ModelTurn(text="PRIVATE final answer")])
        engine = AgentLoopRunner(client, fake_registry())
        calls = []
        def result_factory(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return AgentResult(**{**kwargs, "called_tools": [{"PRIVATE": "invalid trace"}]})
            return AgentResult(**kwargs)
        with patch("app.ai.agent.agent_loop_runner.AgentResult", side_effect=result_factory):
            result = self.assert_failed(engine, "result_validation_error")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.called_tools, [])

    def test_executor_model_instances_are_revalidated(self):
        client = FakeLLMClient([tool_turn(call()), ModelTurn(text="done")])
        engine = AgentLoopRunner(client, fake_registry())
        outcome, trace = engine.executor.execute(call(), CONTEXT)
        with patch.object(engine.executor, "execute", return_value=(outcome, trace)):
            result = engine.run("synthetic", CONTEXT)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.called_tools[0], trace)

    def test_registry_failure_inside_run_returns_failed(self):
        registry = fake_registry()
        engine = AgentLoopRunner(FakeLLMClient([]), registry)
        with patch.object(registry, "definitions", side_effect=RuntimeError("PRIVATE schema")):
            self.assert_failed(engine, "registry_error")

    def test_invalid_evidence_from_executor_returns_failed(self):
        engine = AgentLoopRunner(FakeLLMClient([tool_turn(call(ToolName.SEARCH_EVIDENCE, {"query": "synthetic"}))]),
                                   fake_registry())
        outcome = ToolOutcome(call_id="call_1", tool_name=ToolName.SEARCH_EVIDENCE.value, success=True,
                              data={"query": "synthetic", "evidence": [{"PRIVATE": "invalid"}]})
        trace = ToolTrace(tool_name=ToolName.SEARCH_EVIDENCE.value, success=True, duration_ms=0)
        with patch.object(engine.executor, "execute", return_value=(outcome, trace)):
            self.assert_failed(engine, "evidence_validation_error")

    def test_profile_identity_field_is_rejected_and_not_sent_to_llm(self):
        client = FakeLLMClient([tool_turn(call()), ModelTurn(text="recovered")])
        registry = registry_with(lambda context, args: {"patient_id": UUID(int=99), "name": "PRIVATE patient"})
        outcome, _ = ToolExecutor(registry).execute(call(), CONTEXT)
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.error.code, "invalid_output")
        self.assertIsNone(outcome.data)
        result = AgentLoopRunner(client, registry).run("synthetic", CONTEXT)
        self.assertEqual(result.status, "completed")
        self.assertEqual([error.code for error in result.errors], ["invalid_output"])
        self.assertNotIn("PRIVATE", result.model_dump_json())
        tool_result = next(message for message in client.requests[-1][0] if message.role == "tool")
        self.assertNotIn("PRIVATE", tool_result.content)
        self.assertEqual(len(client.requests), 2)

    def test_profile_without_identity_succeeds(self):
        outcome, _ = ToolExecutor(registry_with(
            lambda context, args: c.PatientProfileOutput(name="synthetic"))).execute(call(), CONTEXT)
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.data["name"], "synthetic")
        self.assertNotIn("patient_id", outcome.data)


class RegistrationAndConfigurationTests(TestCase):
    def test_non_callable_handler_rejected(self):
        for handler in (None, 42, "function"):
            with self.subTest(handler=handler), self.assertRaises(ValueError):
                registry_with(handler)

    def test_invalid_model_classes_rejected(self):
        for field in ("input_model", "output_model"):
            for model in (None, dict, BaseModel, c.PatientProfileInput()):
                with self.subTest(field=field, model=model), self.assertRaises(ValueError):
                    contract = replace(TOOL_CONTRACTS[ToolName.PATIENT_PROFILE], **{field: model})
                    ToolRegistry((ToolSpec(contract, lambda context, args: None),))

    def test_broken_model_schema_rejected(self):
        class BrokenModel(BaseModel):
            @classmethod
            def model_json_schema(cls, *args, **kwargs):
                raise RuntimeError("PRIVATE model schema")
        contract = replace(TOOL_CONTRACTS[ToolName.PATIENT_PROFILE], output_model=BrokenModel)
        with self.assertRaises(ValueError) as raised:
            ToolRegistry((ToolSpec(contract, lambda context, args: None),))
        self.assertNotIn("PRIVATE", str(raised.exception))

    def test_canonical_contract_mismatch_rejected_in_both_modes(self):
        canonical = TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE]
        for mode in ("production", "test"):
            for change in ({"description": "different"}, {"input_model": c.PatientProfileInput},
                           {"output_model": c.PatientProfileOutput}, {"name": "search_evidence"}):
                with self.subTest(mode=mode, change=change), self.assertRaises(ValueError):
                    ToolRegistry((ToolSpec(replace(canonical, **change), lambda context, args: None),), mode=mode)

    def test_equivalent_copy_of_canonical_contract_accepted(self):
        contract = replace(TOOL_CONTRACTS[ToolName.PATIENT_PROFILE])
        registry = ToolRegistry((ToolSpec(contract, lambda context, args: None),))
        self.assertEqual(registry.definitions()[0], contract.definition())

    def test_invalid_spec_and_mode_rejected(self):
        for specs, mode in [((None,), "test"), ((), "unknown"),
                            ((ToolSpec(None, lambda context, args: None),), "production")]:
            with self.subTest(mode=mode, specs=specs), self.assertRaises(ValueError):
                ToolRegistry(specs, mode=mode)

    def test_round_and_call_limits_require_strict_positive_integers(self):
        for name in ("max_tool_rounds", "max_total_tool_calls"):
            for value in (0, -1, True, False, 1.0, 1.5, float("nan"), float("inf"), "1", None):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    AgentLoopRunner(FakeLLMClient([]), fake_registry(), **{name: value})

    def test_timeout_configuration_requires_positive_finite_seconds(self):
        for name in ("tool_timeout_seconds", "request_timeout_seconds"):
            for value in (0, -1, True, False, float("nan"), float("inf"), 10**1000, "1", None):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    AgentLoopRunner(FakeLLMClient([]), fake_registry(), **{name: value})


class OutputConsistencyTests(TestCase):
    def logs(self, timestamps, total=None):
        return c.RecentCareLogsOutput(period={"start_at": "2026-10-01T00:00:00Z", "end_at": "2026-10-06T00:00:00Z"},
            logs=[{"logged_at": timestamp} for timestamp in timestamps],
            total_count=len(timestamps) if total is None else total)

    def test_care_log_range_half_open_and_compares_instants(self):
        output = self.logs(["2026-10-05T19:59:59-04:00", "2026-09-30T20:00:00-04:00"], total=9)
        self.assertEqual(len(output.logs), 2)
        for timestamp in ("2026-10-01T00:00:00+09:00", "2026-10-05T20:00:00-04:00"):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValidationError):
                self.logs([timestamp])

    def test_equal_visible_care_logs_are_allowed(self):
        output = self.logs(["2026-10-05T01:00:00+09:00"] * 2)
        self.assertEqual(len(output.logs), 2)

    def test_naive_log_timestamps_rejected(self):
        with self.assertRaises(ValidationError):
            self.logs(["2026-10-05T01:00:00"])

    def test_log_total_cannot_be_smaller_than_returned_count(self):
        with self.assertRaises(ValidationError):
            self.logs(["2026-10-05T01:00:00+09:00"], total=0)
