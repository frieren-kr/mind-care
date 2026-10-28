"""순차/명시적 병렬 Tool Calling Loop. Prompt 규칙/Provider/DB 구현은 이 계층에 두지 않는다."""
from copy import deepcopy
from functools import partial
from time import monotonic
from typing import cast

from ._execution import RequestTimeout, ParallelStopped, bounded_call, bounded_parallel, check_deadline, positive_seconds, request_deadline
from .clients.base import LLMClient, GenerationOptions, GenerationRequest
from .executor import ToolExecutor
from .feed_search_policy import (MAX_FEED_RETRIEVAL, MAX_FEED_SEARCH_ROUNDS, MAX_FEED_SEARCHES, FeedSearchBudgetExceeded,
                                 feed_tool_definitions, prepare_feed_searches)
from .feed_candidates import FeedCandidates, FeedDecisionError
from .conversation import Conversation
from .outputs import (ChatAnswerV1, FeedAnswerV2, FinalOutputModel, ResolvedEvidence,
                      StructuredAgentResult, validate_final_output)
from .prompts import DefaultPromptBuilder, PromptBuilder, build_structured_output_policy
from .registry import ToolName, ToolRegistry
from .schemas import AgentContext, AgentError, AgentResult, AgentState, ModelTurn, ToolOutcome, ToolTrace
from .tools.contracts import EvidencePackage


def _validate_tool_result(call, result):
    outcome, trace = result
    outcome = ToolOutcome.model_validate(
        outcome.model_dump(warnings=False) if isinstance(outcome, ToolOutcome) else outcome)
    trace = ToolTrace.model_validate(
        trace.model_dump(warnings=False) if isinstance(trace, ToolTrace) else trace)
    if (outcome.call_id != call.id or outcome.tool_name != call.name or
            outcome.success != (outcome.error is None) or trace.success != outcome.success):
        raise ValueError("Executor result does not match the call")
    return outcome, trace


class AgentLoopRunner:
    def __init__(self, client: LLMClient, registry: ToolRegistry, *,
                 prompt_builder: PromptBuilder | None = None,
                 max_tool_rounds: int = 5, max_total_tool_calls: int = 10,
                 tool_timeout_seconds: float = 30.0, request_timeout_seconds: float = 120.0,
                 allow_parallel_tools: bool = False, max_parallel_calls: int = 3):
        for name, value in (("max_tool_rounds", max_tool_rounds), ("max_total_tool_calls", max_total_tool_calls)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a strict positive integer")
        if type(allow_parallel_tools) is not bool:
            raise ValueError("allow_parallel_tools must be a boolean")
        if type(max_parallel_calls) is not int or not 1 <= max_parallel_calls <= 8:
            raise ValueError("max_parallel_calls must be an integer from 1 to 8")
        self.allow_parallel_tools = allow_parallel_tools
        self.max_parallel_calls = max_parallel_calls
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
                       output_model: FinalOutputModel,
                       previously_recommended_pmids: frozenset[str] = frozenset()) -> StructuredAgentResult:
        """명시적 opt-in. 기존 Client/Tool Loop로 실행하고 마지막 text만 검증한다."""
        if output_model not in (ChatAnswerV1, FeedAnswerV2):
            raise ValueError("Only ChatAnswerV1 and FeedAnswerV2 are supported")
        return cast(StructuredAgentResult, self._run(user_input, context, output_model=output_model,
                    previously_recommended_pmids=previously_recommended_pmids))

    def _tool_results(self, calls, context, state, *, stop_on_failure):
        parallel = False
        if self.allow_parallel_tools and self.max_parallel_calls > 1 and len(calls) > 1:
            parallel = bounded_call(lambda: all(
                (spec := self.registry.get(call.name)) is not None and spec.parallel_safe for call in calls))
        if parallel:
            def record_started(count):
                state.total_tool_calls += count
            for start in range(0, len(calls), self.max_parallel_calls):
                batch = calls[start:start + self.max_parallel_calls]
                try:
                    raw_results = bounded_parallel([
                        partial(self.executor.execute, call.model_copy(deep=True), context.model_copy(deep=True))
                        for call in batch], stop_on_result=(
                            lambda index, raw: not _validate_tool_result(batch[index], raw)[0].success)
                            if stop_on_failure else None, on_started=record_started)
                except ParallelStopped as stopped:
                    for index, raw in stopped.results:
                        yield batch[index], *_validate_tool_result(batch[index], raw)
                    return
                for call, raw in zip(batch, raw_results, strict=True):
                    yield call, *_validate_tool_result(call, raw)
        else:
            for call in calls:
                check_deadline()
                state.total_tool_calls += 1
                outcome, trace = _validate_tool_result(call, bounded_call(lambda: self.executor.execute(call, context)))
                yield call, outcome, trace
                if stop_on_failure and not outcome.success:
                    return

    def _run(self, user_input: str, context: AgentContext, *,
             output_model: FinalOutputModel | None = None,
             previously_recommended_pmids: frozenset[str] = frozenset()) -> AgentResult | StructuredAgentResult:
        state = AgentState(request_id=context.request_id)
        conversation = Conversation()
        evidence: list[EvidencePackage] = []
        evidence_by_call: dict[str, EvidencePackage] = {}
        feed_candidates = (FeedCandidates(previously_recommended_pmids=previously_recommended_pmids)
                           if output_model is FeedAnswerV2 else None)
        feed_retrieval_used = 0
        feed_model_calls = 0
        feed_query_keys: set[str] = set()
        structured_output: ChatAnswerV1 | FeedAnswerV2 | None = None
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
            conversation.items = bounded_call(lambda: self.prompt_builder.build_initial_messages(user_input, context))
            if output_model is not None and feed_candidates is None:
                user_position = next((index for index, message in enumerate(conversation.items)
                                      if message.role == "user"), len(conversation.items))
                conversation.items.insert(user_position, bounded_call(lambda: build_structured_output_policy(output_model)))
            while True:
                check_deadline()
                stage = "prompt_builder_error"
                # 구조화 참조는 call ID가 있는 Tool Result만 사용한다. legacy의 근거 묶음은 유지한다.
                messages = conversation.snapshot() + (bounded_call(
                    lambda: self.prompt_builder.build_final_response_context(evidence)) if output_model is None else [])
                if feed_candidates is not None:
                    rounds_remaining = max(0, min(MAX_FEED_SEARCH_ROUNDS, self.max_tool_rounds) - state.tool_round)
                    remaining = max(0, min(MAX_FEED_SEARCHES, self.max_total_tool_calls) - state.total_tool_calls)
                    if feed_candidates.successful_searches:
                        # Feed evaluation policy/projection belong to this request only,
                        # after a successful search, never to persistent history.
                        user_position = next((index for index, message in enumerate(messages)
                                              if message.role == "user"), len(messages))
                        messages.insert(user_position, bounded_call(
                            lambda: build_structured_output_policy(output_model)))
                        messages.append(bounded_call(lambda: feed_candidates.message(
                            remaining, model_tool_calls=feed_model_calls,
                            rounds_used=state.tool_round, rounds_remaining=rounds_remaining)))
                stage = "registry_error"
                definitions = bounded_call(self.registry.definitions)
                if feed_candidates is not None:
                    definitions = bounded_call(lambda: feed_tool_definitions(definitions))
                try:
                    if feed_candidates is not None:
                        options = GenerationOptions(
                            tool_choice=("none" if remaining == 0 or rounds_remaining == 0 else
                                         "auto" if feed_candidates.successful_searches else "required"),
                            parallel_tool_calls=self.allow_parallel_tools and self.max_parallel_calls > 1)
                        turn = bounded_call(lambda: self.client.generate(GenerationRequest(
                            inputs=tuple(messages), tools=definitions, options=options)))
                    else:
                        turn = bounded_call(lambda: self.client.generate(GenerationRequest(
                            inputs=tuple(messages), tools=definitions)))
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
                if feed_candidates is not None and not feed_candidates.successful_searches:
                    if not turn.tool_calls or any(call.name != "search_evidence" for call in turn.tool_calls):
                        state.errors.append(AgentError(code="feed_search_required",
                            message="Feed는 최초 search_evidence 실행이 필요합니다."))
                        return finish("invalid_response")
                if not turn.tool_calls:
                    if not turn.text or not turn.text.strip():
                        state.errors.append(AgentError(code="empty_response", message="LLM 답변이 비어 있습니다."))
                        return finish("invalid_response")
                    check_deadline()
                    if output_model is not None:
                        stage = "structured_output_error"
                        try:
                            if feed_candidates is not None:
                                structured_output, sources = bounded_call(
                                    lambda: feed_candidates.finalize(turn.text, evidence_by_call))
                            else:
                                structured_output, sources = bounded_call(
                                    lambda: validate_final_output(turn.text, output_model, evidence_by_call))
                        except FeedDecisionError as error:
                            state.errors.append(AgentError(code=error.code,
                                message="Feed 판단 형식 또는 서버 후보 출처가 계약과 일치하지 않습니다."))
                            return finish("invalid_response")
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
                if (state.tool_round >= (min(MAX_FEED_SEARCH_ROUNDS, self.max_tool_rounds) if feed_candidates is not None else self.max_tool_rounds) or
                        state.total_tool_calls + len(turn.tool_calls) > self.max_total_tool_calls):
                    state.errors.append(AgentError(code="tool_limit", message="Tool 실행 상한에 도달했습니다."))
                    return finish("limit_reached")
                ids = [call.id for call in turn.tool_calls]
                if len(set(ids)) != len(ids) or seen_ids.intersection(ids):
                    state.errors.append(AgentError(code="duplicate_call_id", message="LLM Tool Call ID가 중복되었습니다."))
                    return finish("invalid_response")
                calls = turn.tool_calls
                if feed_candidates is not None:
                    try:
                        search_plan = bounded_call(lambda: prepare_feed_searches(
                            calls, turn=state.tool_round + 1,
                            remaining_calls=min(MAX_FEED_SEARCHES, self.max_total_tool_calls) - state.total_tool_calls,
                            remaining_budget=MAX_FEED_RETRIEVAL - feed_retrieval_used,
                            previous_queries=feed_query_keys,
                            reserved_ids=seen_ids | set(evidence_by_call)))
                        staged_candidates = deepcopy(feed_candidates)
                        bounded_call(lambda: staged_candidates.apply_review(search_plan.review, evidence_by_call))
                        calls = search_plan.searches
                    except FeedDecisionError as error:
                        state.errors.append(AgentError(code=error.code, message="Feed 후보 검토 정보가 계약과 일치하지 않습니다."))
                        return finish("invalid_response")
                    except FeedSearchBudgetExceeded:
                        state.errors.append(AgentError(code='tool_limit', message='Feed 실제 검색 예산을 초과했습니다.'))
                        return finish('limit_reached')
                    except (ValueError, TypeError):
                        state.errors.append(AgentError(code="invalid_arguments",
                            message="Feed 검색 입력 또는 검색량이 계약과 일치하지 않습니다."))
                        return finish("failed")
                    feed_retrieval_used += sum(call.arguments["top_k"] for call in calls)
                    feed_model_calls += len(turn.tool_calls)
                    feed_query_keys.update(search_plan.query_keys)
                seen_ids.update(ids)
                state.tool_round += 1
                stage = "executor_error"
                results = self._tool_results(calls, context, state, stop_on_failure=feed_candidates is not None)
                if feed_candidates is not None:
                    # Consume and validate the whole batch before exposing evidence.
                    results = list(results)
                    state.called_tools.extend(trace for _, _, trace in results)
                    state.errors.extend(outcome.error for _, outcome, _ in results if outcome.error)
                    if any(not outcome.success for _, outcome, _ in results):
                        for _, outcome, _ in results:
                            feed_candidates.record_search(outcome.success)
                        return finish("failed")
                    stage = "evidence_validation_error"
                    packages = [(call.id, bounded_call(
                        lambda outcome=outcome: EvidencePackage.model_validate(outcome.data)))
                        for call, outcome, _ in results]
                    if any(len(package.evidence) > call.arguments["top_k"]
                           for call, (_, package) in zip(calls, packages, strict=True)):
                        raise ValueError("Feed search returned more than its retrieval limit")
                    stage = "candidate_source_mismatch"
                    for call_id, package in packages:
                        before_count = staged_candidates.eligible_count
                        bounded_call(lambda: staged_candidates.add(call_id, package))
                        staged_candidates.record_search(True, len(package.evidence),
                            staged_candidates.eligible_count - before_count)
                    check_deadline()
                    staged_candidates.finish_round()
                    feed_candidates = staged_candidates
                    for call_id, package in packages:
                        evidence.append(package)
                        evidence_by_call[call_id] = package.model_copy(deep=True)
                    # Exactly one Tool response per REAL Provider call, even
                    # when that call fanned out to multiple server searches.
                    outcomes = search_plan.provider_outcomes(dict(packages))
                else:
                    # Default Chat/legacy mode still validates and commits each
                    # sequential call before starting the next dependent call.
                    outcomes = []
                    for call, outcome, trace in results:
                        outcomes.append(outcome)
                        state.called_tools.append(trace)
                        if outcome.error:
                            state.errors.append(outcome.error)
                            if outcome.error.code in ("tool_timeout", "patient_context_mismatch"):
                                return finish("failed")
                        if outcome.success and call.name == ToolName.SEARCH_EVIDENCE.value:
                            stage = "evidence_validation_error"
                            package = bounded_call(lambda: EvidencePackage.model_validate(outcome.data))
                            evidence.append(package)
                            if output_model is not None:
                                evidence_by_call[call.id] = package.model_copy(deep=True)
                        stage = "executor_error"
                stage = "conversation_error"
                bounded_call(lambda: conversation.append_turn(turn, outcomes))
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
