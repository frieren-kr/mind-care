"""순차 Tool Calling Loop. Prompt 규칙/Provider/DB 구현은 이 계층에 두지 않는다."""
from time import monotonic
from typing import cast

from ._execution import RequestTimeout, bounded_call, check_deadline, positive_seconds, request_deadline
from .clients.base import LLMClient
from .executor import ToolExecutor
from .outputs import (ChatAnswerV1, FeedAnswerV1, FinalOutputModel, ResolvedEvidence,
                      StructuredAgentResult, validate_final_output)
from .prompts import DefaultPromptBuilder, PromptBuilder, build_structured_output_policy
from .registry import ToolName, ToolRegistry
from .schemas import AgentContext, AgentError, AgentResult, AgentState, ModelTurn, ToolOutcome, ToolTrace
from .tools.contracts import EvidencePackage


class AgentLoopRunner:
    def __init__(self, client: LLMClient, registry: ToolRegistry, *,
                 prompt_builder: PromptBuilder | None = None,
                 max_tool_rounds: int = 5, max_total_tool_calls: int = 10,
                 tool_timeout_seconds: float = 30.0, request_timeout_seconds: float = 120.0):
        for name, value in (("max_tool_rounds", max_tool_rounds), ("max_total_tool_calls", max_total_tool_calls)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a strict positive integer")
        self.client = client
        self.registry = registry
        self.executor = ToolExecutor(registry, timeout_seconds=tool_timeout_seconds)
        self.prompt_builder = prompt_builder if prompt_builder is not None else DefaultPromptBuilder()
        self.max_tool_rounds = max_tool_rounds
        self.max_total_tool_calls = max_total_tool_calls
        self.request_timeout_seconds = positive_seconds(request_timeout_seconds, "request_timeout_seconds")

    def run(self, user_input: str, context: AgentContext) -> AgentResult:
        return cast(AgentResult, self._run(user_input, context))

    def run_structured(self, user_input: str, context: AgentContext, *,
                       output_model: FinalOutputModel) -> StructuredAgentResult:
        """명시적 opt-in. 기존 Client/Tool Loop로 실행하고 마지막 text만 검증한다."""
        if output_model not in (ChatAnswerV1, FeedAnswerV1):
            raise ValueError("Only ChatAnswerV1 and FeedAnswerV1 are supported")
        return cast(StructuredAgentResult, self._run(user_input, context, output_model=output_model))

    def _run(self, user_input: str, context: AgentContext, *,
             output_model: FinalOutputModel | None = None) -> AgentResult | StructuredAgentResult:
        state = AgentState(request_id=context.request_id)
        evidence: list[EvidencePackage] = []
        evidence_by_call: dict[str, EvidencePackage] = {}
        structured_output: ChatAnswerV1 | FeedAnswerV1 | None = None
        sources: list[ResolvedEvidence] = []
        seen_ids: set[str] = set()
        deadline = monotonic() + self.request_timeout_seconds
        inherited = request_deadline.get()
        token = request_deadline.set(min(deadline, inherited) if inherited is not None else deadline)
        stage = "prompt_builder_error"

        def build_result(status):
            result = AgentResult(status=status, request_id=state.request_id, final_answer=state.final_answer,
                               tool_rounds=state.tool_round, total_tool_calls=state.total_tool_calls,
                               called_tools=[trace.model_dump(warnings=False) for trace in state.called_tools],
                               errors=[error.model_dump(warnings=False) for error in state.errors])
            if output_model is not None:
                return StructuredAgentResult(execution=result,
                    output=structured_output.model_dump(warnings=False) if status == "completed" else None,
                    sources=[source.model_dump(warnings=False) for source in sources] if status == "completed" else [])
            return result

        def minimal_failure(error):
            # deadline 만료 후에도 반환 가능해야 한다. 오염된 metadata와 콘텐츠는 사용하지 않는다.
            result = AgentResult(status="failed", request_id=context.request_id, final_answer=None,
                               tool_rounds=state.tool_round, total_tool_calls=state.total_tool_calls,
                               called_tools=[], errors=[error])
            return StructuredAgentResult(execution=result, output=None, sources=[]) if output_model else result

        def finish(status):
            try:
                # 성공 확정에는 최종 모델 검증까지 포함한다. 실패 응답은 deadline 밖에서 만든다.
                return bounded_call(lambda: build_result(status)) if status == "completed" else build_result(status)
            except RequestTimeout:
                raise
            except Exception:
                return minimal_failure(AgentError(code="result_validation_error",
                    message="Agent 실행 결과가 계약과 일치하지 않습니다."))

        try:
            state.messages = bounded_call(lambda: self.prompt_builder.build_initial_messages(user_input, context))
            if output_model is not None:
                user_position = next((index for index, message in enumerate(state.messages)
                                      if message.role == "user"), len(state.messages))
                state.messages.insert(user_position, bounded_call(lambda: build_structured_output_policy(output_model)))
            while True:
                check_deadline()
                stage = "prompt_builder_error"
                # 구조화 참조는 call ID가 있는 Tool Result만 사용한다. legacy의 근거 묶음은 유지한다.
                messages = state.messages + (bounded_call(
                    lambda: self.prompt_builder.build_final_response_context(evidence)) if output_model is None else [])
                stage = "registry_error"
                definitions = bounded_call(self.registry.definitions)
                try:
                    turn = bounded_call(lambda: self.client.generate(messages, definitions))
                except RequestTimeout:
                    raise
                except Exception:
                    state.errors.append(AgentError(code="provider_error", message="LLM 호출에 실패했습니다."))
                    return finish("provider_error")
                try:
                    turn = bounded_call(lambda: ModelTurn.model_validate(
                        turn.model_dump(warnings=False) if isinstance(turn, ModelTurn) else turn))
                except RequestTimeout:
                    raise
                except Exception:
                    state.errors.append(AgentError(code="invalid_model_turn", message="LLM 응답이 계약과 일치하지 않습니다."))
                    return finish("invalid_response")
                if not turn.tool_calls:
                    if not turn.text or not turn.text.strip():
                        state.errors.append(AgentError(code="empty_response", message="LLM 답변이 비어 있습니다."))
                        return finish("invalid_response")
                    check_deadline()
                    if output_model is not None:
                        stage = "structured_output_error"
                        try:
                            structured_output, sources = bounded_call(
                                lambda: validate_final_output(turn.text, output_model, evidence_by_call))
                        except (ValueError, TypeError, RecursionError):
                            state.errors.append(AgentError(code="invalid_structured_output",
                                message="최종 답변의 구조 또는 출처 참조가 계약과 일치하지 않습니다."))
                            return finish("invalid_response")
                        check_deadline()
                        state.final_answer = (structured_output.answer
                                              if isinstance(structured_output, ChatAnswerV1) else None)
                    else:
                        state.final_answer = turn.text
                    return finish("completed")
                if (state.tool_round >= self.max_tool_rounds or
                        state.total_tool_calls + len(turn.tool_calls) > self.max_total_tool_calls):
                    state.errors.append(AgentError(code="tool_limit", message="Tool 실행 상한에 도달했습니다."))
                    return finish("limit_reached")
                ids = [call.id for call in turn.tool_calls]
                if len(set(ids)) != len(ids) or seen_ids.intersection(ids):
                    state.errors.append(AgentError(code="duplicate_call_id", message="LLM Tool Call ID가 중복되었습니다."))
                    return finish("invalid_response")
                seen_ids.update(ids)
                state.tool_round += 1
                outcomes = []
                for call in turn.tool_calls:
                    check_deadline()
                    state.total_tool_calls += 1
                    stage = "executor_error"
                    outcome, trace = bounded_call(lambda: self.executor.execute(call, context))
                    # Pydantic instance도 dump 후 재검증하여 model_construct/변경된 필드를 차단한다.
                    outcome = ToolOutcome.model_validate(
                        outcome.model_dump(warnings=False) if isinstance(outcome, ToolOutcome) else outcome)
                    trace = ToolTrace.model_validate(
                        trace.model_dump(warnings=False) if isinstance(trace, ToolTrace) else trace)
                    if (outcome.call_id != call.id or outcome.tool_name != call.name or
                            outcome.success != (outcome.error is None) or trace.success != outcome.success):
                        raise ValueError("Executor result does not match the call")
                    outcomes.append(outcome)
                    state.called_tools.append(trace)
                    if outcome.error:
                        state.errors.append(outcome.error)
                        # timeout는 작업 완료 여부가 불명확하다. LLM 재호출/추가 Tool을 중단한다.
                        if outcome.error.code in ("tool_timeout", "patient_context_mismatch"):
                            return finish("failed")
                    if outcome.success and call.name == ToolName.SEARCH_EVIDENCE.value:
                        stage = "evidence_validation_error"
                        package = bounded_call(lambda: EvidencePackage.model_validate(outcome.data))
                        evidence.append(package)
                        if output_model is not None:
                            evidence_by_call[call.id] = package.model_copy(deep=True)
                stage = "prompt_builder_error"
                state.messages.extend(bounded_call(lambda: self.prompt_builder.build_tool_followup_messages(turn, outcomes)))
        except RequestTimeout:
            state.final_answer = None
            error = AgentError(code="request_timeout", message="Agent 전체 실행 시간이 초과되었습니다.")
            state.errors.append(error)
            # 만료된 deadline을 다시 검사하지 않는다. 실패 metadata 검증도 실패하면 최소 timeout 결과만 반환한다.
            try:
                return build_result("failed")
            except Exception:
                return minimal_failure(error)
        except Exception:
            state.final_answer = None
            state.errors.append(AgentError(code=stage, message="Agent 내부 실행 단계에 실패했습니다."))
            return finish("failed")
        finally:
            request_deadline.reset(token)
