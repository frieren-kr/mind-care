"""순차/명시적 병렬 Tool Calling Loop. Prompt 규칙/Provider/DB 구현은 이 계층에 두지 않는다."""
from copy import deepcopy
from functools import partial
import json
import logging
from time import monotonic
from typing import cast

from pydantic import ValidationError

from ._execution import RequestTimeout, ParallelStopped, bounded_call, bounded_parallel, check_deadline, positive_seconds, request_deadline
from .clients.base import LLMClient, GenerationOptions, GenerationRequest
from .clients.openai_client import ProviderResponseError
from .executor import PERMISSION_FAILURE_MESSAGE, ToolExecutor, diagnostic_call_id, tool_diagnostic_context
from .error_diagnostics import diagnostic_message, summarize_json_error, summarize_validation_error
from .feed_search_policy import (MAX_FEED_ARGUMENT_RECOVERIES, MAX_FEED_RETRIEVAL,
                                 MAX_FEED_SEARCH_ROUNDS, MAX_FEED_SEARCHES,
                                 FeedSearchBudgetExceeded, FeedSearchInputError,
                                 feed_tool_definitions, prepare_feed_searches)
from .feed_candidates import FeedCandidates, FeedDecisionError
from .conversation import Conversation
from .evidence import EvidenceStore
from .outputs import (ChatAnswerV1, FeedAnswerV2, FinalOutputModel, ResolvedEvidence,
                      StructuredAgentResult)
from .prompts import DefaultPromptBuilder, PromptBuilder, build_structured_output_policy
from .registry import ToolRegistry
from .schemas import AgentContext, AgentError, AgentResult, AgentState, Message, ModelTurn, ToolOutcome, ToolTrace
from .tools.contracts import EvidencePackage, EvidenceSearchInput, GuidelineSearchInput


logger = logging.getLogger(__name__)

CHAT_DEFAULT_TOP_K = 3
MAX_CHAT_PAPERS = 7
MAX_CHAT_GUIDELINES = 4
MAX_CHAT_CANDIDATES = MAX_CHAT_PAPERS + MAX_CHAT_GUIDELINES
_CHAT_SEARCH_TYPES = {"search_evidence": "new_research", "search_guidelines": "guideline"}


def _chat_candidate_counts(keys, store):
    counts = {"new_research": 0, "guideline": 0}
    for key in keys:
        counts[store.item(key).evidence_type] += 1
    return counts


def _prepare_chat_searches(calls, snapshot, store, target, search_tools):
    """Plan one Chat turn against the last disclosed snapshot, in call order.

    Paper top_k is a total target; guideline top_k is a per-call count.
    Chat-only review fields never reach the common search Handler.
    """
    store.validate_snapshot(snapshot)
    active_keys = list(snapshot.keys)
    prepared, skipped = [], {}
    reserved, search_index = {"new_research": 0, "guideline": 0}, 0
    close_search = set()
    for call in calls:
        if call.name not in search_tools:
            prepared.append(call)
            continue
        kind = _CHAT_SEARCH_TYPES[call.name]
        label = "논문" if kind == "new_research" else "가이드라인"
        if call.name in close_search:
            logger.info("agent_error metadata=%s tool_call_id=%r stage=chat_search_plan "
                        "input_origin=llm_arguments error_code=chat_search_budget_exhausted "
                        "diagnostic=%s recovery_action=existing_chat_loop",
                        tool_diagnostic_context.get(), diagnostic_call_id(call.id),
                        dict(reason="search_closed_within_turn", expected="use the accepted evidence"))
            skipped[call.id] = ToolOutcome(call_id=call.id, tool_name=call.name, success=False,
                error=AgentError(code="chat_search_budget_exhausted",
                    message=f"이번 요청의 {label} 검색을 종료하여 이 검색은 실행하지 않았습니다. 확보한 근거와 한계를 설명하세요."))
            continue
        first_search = search_index == 0
        search_index += 1
        diagnostic = {}
        try:
            args = json.loads(call.arguments) if isinstance(call.arguments, str) else dict(call.arguments)
            if not isinstance(args, dict):
                diagnostic = {"reason": "chat_search_policy", "expected": "Chat search input must be an object"}
                raise ValueError("Chat search policy violation")
            review_present = "exclude_candidates" in args or "candidate_version" in args
            version_present = "candidate_version" in args
            excluded = args.pop("exclude_candidates", [])
            version = args.pop("candidate_version", None)
            if not isinstance(excluded, list) or any(type(i) is not int for i in excluded):
                diagnostic = {"reason": "chat_search_policy", "expected": "Excluded candidates must be integer positions"}
                raise ValueError("Chat search policy violation")
            if len(excluded) != len(set(excluded)) or any(i < 0 or i >= len(snapshot.keys) for i in excluded):
                diagnostic = {"reason": "chat_search_policy", "expected": "Excluded candidates must identify current candidates"}
                raise ValueError("Chat search policy violation")
            if review_present and not first_search:
                diagnostic = {"reason": "chat_search_policy", "expected": "Only the first search may review this snapshot"}
                raise ValueError("Chat search policy violation")
            if excluded and version is None:
                diagnostic = {"reason": "chat_search_policy", "expected": "Candidate exclusion requires the disclosed version"}
                raise ValueError("Chat search policy violation")
            if version_present and (type(version) is not int or version != snapshot.version):
                diagnostic = {"reason": "chat_search_policy", "expected": "Stale candidate version"}
                raise ValueError("Chat search policy violation")
            if kind == "new_research":
                requested = args.get("top_k", target)
                if type(requested) is not int or requested < 1:
                    diagnostic = {"reason": "chat_search_policy", "expected": "Invalid Chat top_k"}
                    raise ValueError("Chat search policy violation")
                requested = min(requested, MAX_CHAT_PAPERS)
                args["top_k"] = requested
                validated = EvidenceSearchInput.model_validate(args)
            else:
                validated = GuidelineSearchInput.model_validate(args)
                requested = validated.top_k
            kept = [key for i, key in enumerate(active_keys) if i not in excluded]
            active_count = _chat_candidate_counts(kept, store)[kind]
            if kind == "new_research" and active_count + reserved[kind] > requested:
                diagnostic = {"reason": "chat_search_policy", "expected": "top_k is smaller than retained and reserved candidates"}
                raise ValueError("Chat search policy violation")
        except (ValueError, TypeError) as exc:
            if isinstance(exc, ValidationError):
                diagnostic = summarize_validation_error(exc,
                    EvidenceSearchInput if kind == "new_research" else GuidelineSearchInput)
            elif isinstance(exc, json.JSONDecodeError):
                diagnostic = summarize_json_error(exc)
            logger.info("agent_error metadata=%s tool_call_id=%r stage=chat_search_plan "
                        "input_origin=llm_arguments error_code=invalid_arguments exception_type=%s "
                        "diagnostic=%s recovery_action=existing_chat_loop",
                        tool_diagnostic_context.get(), diagnostic_call_id(call.id), type(exc).__name__, diagnostic)
            skipped[call.id] = ToolOutcome(call_id=call.id, tool_name=call.name, success=False,
                error=AgentError(code="invalid_arguments",
                    message="Chat 검색 입력·제외 인덱스·후보 버전 또는 top_k가 현재 후보와 일치하지 않습니다. "
                        + (diagnostic_message(diagnostic) if "issues" in diagnostic or "position" in diagnostic
                           else diagnostic.get("expected", "valid search object required"))))
            continue
        own_excluded = any(store.item(snapshot.keys[i]).evidence_type == kind for i in excluded)
        active_keys = kept
        if kind == "new_research":
            target = requested
            remaining = min(target - active_count - reserved[kind], MAX_CHAT_PAPERS - active_count - reserved[kind])
        else:
            remaining = min(requested, MAX_CHAT_GUIDELINES - active_count - reserved[kind])
        if remaining == 0:
            # Only real retained candidates close search, never same-turn reservations.
            closes = active_count > 0 and not own_excluded and reserved[kind] == 0
            if closes:
                close_search.add(call.name)
            logger.info("agent_error metadata=%s tool_call_id=%r stage=chat_search_plan "
                        "input_origin=llm_arguments error_code=chat_search_budget_exhausted "
                        "diagnostic=%s recovery_action=existing_chat_loop",
                        tool_diagnostic_context.get(), diagnostic_call_id(call.id),
                        dict(reason="retained_budget_full" if closes else "reserved_budget_full",
                             active_count=active_count, reserved_count=reserved[kind],
                             requested_count=requested, expected="use the accepted evidence"))
            skipped[call.id] = ToolOutcome(call_id=call.id, tool_name=call.name, success=False,
                error=AgentError(code="chat_search_budget_exhausted",
                    message=(f"유지 {label} 후보가 예산을 채웠는데 제외·확장 없이 재요청하여 이번 요청의 {label} 검색을 종료했습니다. 확보한 근거와 한계를 설명하세요."
                             if closes else f"현재 {label} 후보·검색 예약 예산이 채워져 이 검색은 실행하지 않았습니다.")))
            continue
        args = validated.model_dump()
        args["top_k"] = remaining
        reserved[kind] += remaining
        prepared.append(call.model_copy(update={"arguments": args}))
    return prepared, skipped, active_keys, target, close_search


def _add_chat_candidates(candidates, active_keys, pmids, dois, guidelines, store):
    """Keep first occurrences; DOI supplements PMID, titles never identify papers."""
    counts = _chat_candidate_counts(active_keys, store)
    for candidate in candidates:
        item = candidate.evidence
        if item.evidence_type == "guideline":
            identity = (item.source_url, item.title)
            if identity not in guidelines:
                if counts["guideline"] >= MAX_CHAT_GUIDELINES or len(active_keys) >= MAX_CHAT_CANDIDATES:
                    raise ValueError("Chat guideline budget exceeded")
                active_keys.append(candidate.key)
                counts["guideline"] += 1
            guidelines.add(identity)
            continue
        pmid = (item.pmid or "").strip()
        doi = (item.doi or "").strip().casefold()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if doi.startswith(prefix):
                doi = doi[len(prefix):].strip()
                break
        duplicate = bool(pmid and pmid in pmids) or bool(
            doi and doi in dois and (not pmid or not dois[doi] or pmid in dois[doi]))
        if not duplicate:
            if counts["new_research"] >= MAX_CHAT_PAPERS or len(active_keys) >= MAX_CHAT_CANDIDATES:
                raise ValueError("Chat candidate budget exceeded")
            active_keys.append(candidate.key)
            counts["new_research"] += 1
        if pmid:
            pmids.add(pmid)
        if doi:
            dois.setdefault(doi, set()).update({pmid} if pmid else set())


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
        if output_model is FeedAnswerV2:
            from .workflows.feed_prompts import FeedPromptBuilder
            if not isinstance(self.prompt_builder, FeedPromptBuilder):
                raise ValueError("FeedAnswerV2 requires FeedPromptBuilder for its post-tool success policy")
        return cast(StructuredAgentResult, self._run(user_input, context, output_model=output_model,
                    previously_recommended_pmids=previously_recommended_pmids))

    def _tool_results(self, calls, context, state, *, stop_on_timeout=False, unaccepted=None, input_origins=None):
        def execute(call, execution_context):
            token = tool_diagnostic_context.set({**tool_diagnostic_context.get(),
                "input_origin": (input_origins or {}).get(call.id, "llm_arguments")})
            try:
                return self.executor.execute(call, execution_context)
            finally:
                tool_diagnostic_context.reset(token)

        def timed_out(outcome):
            # Feed collects other failures, but a Tool timeout ends collection.
            return stop_on_timeout and outcome.error is not None and outcome.error.code == "tool_timeout"

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
                        partial(execute, call.model_copy(deep=True), context.model_copy(deep=True))
                        for call in batch], stop_on_result=(
                            lambda index, raw: timed_out(_validate_tool_result(batch[index], raw)[0]))
                            if stop_on_timeout else None, on_started=record_started)
                except ParallelStopped as stopped:
                    if unaccepted is not None:
                        observed = {index for index, _ in stopped.results}
                        unaccepted.update({call.id: "not_accepted" for index, call in enumerate(batch)
                                           if index not in observed})
                        unaccepted.update({call.id: "cancelled" for call in calls[start + len(batch):]})
                    for index, raw in stopped.results:
                        yield batch[index], *_validate_tool_result(batch[index], raw)
                    return
                for call, raw in zip(batch, raw_results, strict=True):
                    yield call, *_validate_tool_result(call, raw)
        else:
            for call in calls:
                check_deadline()
                state.total_tool_calls += 1
                outcome, trace = _validate_tool_result(call, bounded_call(lambda: execute(call, context)))
                yield call, outcome, trace
                if timed_out(outcome):
                    if unaccepted is not None:
                        unaccepted.update({later.id: "cancelled" for later in calls[calls.index(call) + 1:]})
                    return

    def _run(self, user_input: str, context: AgentContext, *,
             output_model: FinalOutputModel | None = None,
             previously_recommended_pmids: frozenset[str] = frozenset()) -> AgentResult | StructuredAgentResult:
        state = AgentState(request_id=context.request_id)
        conversation = Conversation()
        evidence = EvidenceStore()
        chat_keys, chat_pmids, chat_dois, chat_guidelines = [], set(), {}, set()
        chat_target = CHAT_DEFAULT_TOP_K
        chat_search_closed = set()
        feed_candidates = (FeedCandidates(evidence, previously_recommended_pmids=previously_recommended_pmids)
                           if output_model is FeedAnswerV2 else None)
        feed_retrieval_used = 0
        feed_unaccepted = {"cancelled": 0, "not_accepted": 0}
        feed_model_calls = 0
        feed_argument_recoveries = 0
        feed_search_closed = False
        llm_turn = 0
        # One unique key per admitted search, including work never started.
        feed_query_keys: set[str] = set()
        structured_output: ChatAnswerV1 | FeedAnswerV2 | None = None
        sources: list[ResolvedEvidence] = []
        seen_ids: set[str] = set()
        delivered_policy_tools: set[str] = set()
        successful_tool_names: set[str] = set()
        deadline = monotonic() + self.request_timeout_seconds
        inherited = request_deadline.get()
        token = request_deadline.set(min(deadline, inherited) if inherited is not None else deadline)
        stage = "prompt_builder_error"
        diagnostic_token = tool_diagnostic_context.set({})

        def log_failure(exc, *, failure_stage=None, call_id=None, diagnostic=None,
                        origin="server", action="abort", code=None):
            call_id = ([diagnostic_call_id(identifier) for identifier in call_id] if isinstance(call_id, list)
                       else diagnostic_call_id(call_id) if call_id is not None else None)
            if diagnostic is None and isinstance(exc, (FeedDecisionError, ProviderResponseError)):
                diagnostic = exc.diagnostic
            if diagnostic is None and isinstance(exc, ValidationError):
                diagnostic = summarize_validation_error(exc, ModelTurn if failure_stage == "provider_response_validation" else
                    EvidencePackage if stage == "evidence_validation_error" else None)
            logger.info("agent_error request_id=%r workflow=%s llm_turn=%s tool_name=%s "
                        "tool_call_id=%r stage=%s error_code=%s exception_type=%s "
                        "input_origin=%s recovery_action=%s diagnostic=%s",
                        context.request_id, "feed" if feed_candidates is not None else "chat", llm_turn,
                        "search_evidence" if feed_candidates is not None else "<workflow>",
                        call_id, failure_stage or stage, code or stage,
                        type(exc.__cause__ or exc).__name__ if exc is not None else None,
                        origin, action, diagnostic or {})

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
                llm_turn += 1
                tool_diagnostic_context.set(dict(request_id=context.request_id,
                    workflow="feed" if feed_candidates is not None else "chat", llm_turn=llm_turn))
                stage = "prompt_builder_error"
                # 구조화 참조는 call ID가 있는 Tool Result만 사용한다. legacy의 근거 묶음은 유지한다.
                messages = conversation.snapshot() + (bounded_call(
                    lambda: self.prompt_builder.build_final_response_context(evidence.packages())) if output_model is None else [])
                # Static Workflow policy belongs to this request snapshot, never history.
                # Builders implementing the older Protocol keep their no-hook behavior.
                hook = getattr(self.prompt_builder, "build_post_tool_success_policies", None)
                if successful_tool_names and hook is not None:
                    policies = bounded_call(lambda: hook(frozenset(successful_tool_names)))
                    if any(policy.type != "message" or policy.role != "system" or policy.kind != "policy"
                           for policy in policies):
                        raise ValueError("Workflow hook must return system policies")
                    user_position = next((index for index, message in enumerate(messages)
                                          if message.role == "user"), len(messages))
                    messages[user_position:user_position] = policies
                if feed_candidates is not None:
                    rounds_remaining = max(0, min(MAX_FEED_SEARCH_ROUNDS, self.max_tool_rounds) - state.tool_round)
                    remaining = max(0, min(MAX_FEED_SEARCHES, self.max_total_tool_calls) - len(feed_query_keys))
                    if feed_search_closed:
                        remaining = rounds_remaining = 0
                    # No successful search can support a final Feed. Preserve the
                    # search errors instead of requesting an impossible final turn.
                    if not feed_candidates.successful_searches and (remaining == 0 or rounds_remaining == 0):
                        return finish("failed")
                    if feed_candidates.successful_searches or feed_candidates.failed_searches:
                        projection = bounded_call(lambda: feed_candidates.message(
                            remaining, model_tool_calls=feed_model_calls,
                            rounds_used=state.tool_round, rounds_remaining=rounds_remaining))
                        payload = json.loads(projection.content)
                        if feed_argument_recoveries or feed_search_closed or any(feed_unaccepted.values()):
                            payload['search_progress'].update(
                                search_calls_used=state.total_tool_calls,
                                search_calls_started=state.total_tool_calls,
                                search_calls_reserved=len(feed_query_keys),
                                argument_recoveries_used=feed_argument_recoveries,
                                argument_recoveries_remaining=max(0, MAX_FEED_ARGUMENT_RECOVERIES - feed_argument_recoveries),
                                **feed_unaccepted)
                        messages.append(projection.model_copy(update={'content':json.dumps(payload, ensure_ascii=False)}))
                selection_snapshot = feed_candidates.snapshot if feed_candidates is not None else evidence.current("chat")
                if output_model is ChatAnswerV1 and evidence.call_ids:
                    messages.append(bounded_call(lambda: evidence.chat_message(selection_snapshot)))
                if output_model is ChatAnswerV1:
                    counts = _chat_candidate_counts(chat_keys, evidence)
                    messages.append(Message(role="system", kind="agent_context", content=json.dumps({
                        "context_type": "chat_search_budget", "candidate_limit": MAX_CHAT_CANDIDATES,
                        "papers": {"active_count": counts["new_research"], "target_top_k": chat_target,
                            "candidate_limit": MAX_CHAT_PAPERS, "remaining_candidate_budget": chat_target - counts["new_research"],
                            "search_available": self.registry.get("search_evidence") is not None and "search_evidence" not in chat_search_closed},
                        "guidelines": {"active_count": counts["guideline"], "candidate_limit": MAX_CHAT_GUIDELINES,
                            "remaining_candidate_budget": MAX_CHAT_GUIDELINES - counts["guideline"],
                            "search_available": self.registry.get("search_guidelines") is not None and "search_guidelines" not in chat_search_closed}})))
                stage = "registry_error"
                definitions = bounded_call(self.registry.definitions)
                if feed_candidates is not None:
                    definitions = bounded_call(lambda: feed_tool_definitions(definitions))
                elif output_model is ChatAnswerV1:
                    definitions = deepcopy(definitions)
                    if chat_search_closed:
                        definitions = [definition for definition in definitions if definition["name"] not in chat_search_closed]
                    for definition in definitions:
                        if definition["name"] == "search_evidence":
                            definition["parameters"]["properties"]["top_k"].update(
                                default=chat_target, maximum=MAX_CHAT_PAPERS,
                                description="논문 목표 전체 후보 수(유지 논문+신규 논문). 최초 생략 시 3, 이후 생략 시 현재 목표 유지. 필요한 만큼만 확장하며 7을 초과하면 서버가 7로 축소한다.")
                        if definition["name"] in _CHAT_SEARCH_TYPES:
                            definition["parameters"]["properties"].update(
                                exclude_candidates={"type": "array", "items": {"type": "integer", "minimum": 0},
                                    "uniqueItems": True, "description": "근거가 부족해 제외할 최신 후보의 0-based 인덱스. 생략 시 모두 유지. 같은 Turn 첫 검색에만 제공한다."},
                                candidate_version={"type": "integer", "minimum": 1,
                                    "description": "exclude_candidates가 비어 있지 않으면 최신 untrusted_chat_candidates의 candidate_version을 그대로 전달한다."})
                stage = "provider_generation"
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
                except Exception as exc:
                    # Client-declared boundary metadata is whitelisted; never
                    # infer a phase from an exception's message or type name.
                    boundary = getattr(exc, "stage", None)
                    if type(boundary) is not str or boundary not in ("provider_request_validation", "provider_response_validation"):
                        boundary = stage
                    log_failure(exc, failure_stage=boundary, code="provider_error",
                        origin="provider_output" if boundary == "provider_response_validation" else "server")
                    state.errors.append(AgentError(code="provider_error", message="LLM 호출에 실패했습니다."))
                    return finish("provider_error")
                stage = "provider_response_validation"
                try:
                    turn = bounded_call(lambda: ModelTurn.model_validate(
                        turn.model_dump(warnings=False) if isinstance(turn, ModelTurn) else turn))
                except RequestTimeout:
                    raise
                except Exception as exc:
                    log_failure(exc, failure_stage=stage, code="invalid_model_turn", origin="provider_output")
                    state.errors.append(AgentError(code="invalid_model_turn", message="LLM 응답이 계약과 일치하지 않습니다."))
                    return finish("invalid_response")
                if feed_candidates is not None and not feed_candidates.successful_searches:
                    if not turn.tool_calls or any(call.name != "search_evidence" for call in turn.tool_calls):
                        log_failure(None, failure_stage="feed_search_admission", code="feed_search_required",
                            call_id=[call.id for call in turn.tool_calls], origin="llm_arguments",
                            diagnostic=dict(reason="unsupported_feed_tool" if turn.tool_calls else "initial_search_required",
                                field="tool_calls", expected="initial search_evidence call"))
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
                                    lambda: feed_candidates.finalize(turn.text, selection_snapshot))
                            else:
                                structured_output, sources = bounded_call(
                                    lambda: evidence.finalize_chat(turn.text, selection_snapshot))
                        except FeedDecisionError as error:
                            log_failure(error, failure_stage="feed_final_decision", code=error.code)
                            state.errors.append(AgentError(code=error.code,
                                message="Feed 판단 형식 또는 서버 후보 출처가 계약과 일치하지 않습니다."))
                            return finish("invalid_response")
                        except (ValueError, TypeError, RecursionError) as exc:
                            log_failure(exc, failure_stage="final_output_validation")
                            state.errors.append(AgentError(code="invalid_structured_output",
                                message="최종 답변의 구조 또는 출처 참조가 계약과 일치하지 않습니다."))
                            return finish("invalid_response")
                        check_deadline()
                        state.final_answer = (structured_output.answer
                                              if isinstance(structured_output, ChatAnswerV1) else None)
                    else:
                        state.final_answer = turn.text
                    return finish("completed")
                calls, skipped = turn.tool_calls, {}
                kept_chat_keys, next_chat_target = chat_keys, chat_target
                close_chat_search = set()
                if output_model is ChatAnswerV1:
                    disabled = next((call.name for call in calls if call.name in chat_search_closed), None)
                    if disabled is not None:
                        log_failure(None, failure_stage="chat_search_admission", code="chat_search_disabled",
                            call_id=[call.id for call in calls if call.name == disabled], origin="llm_arguments",
                            diagnostic=dict(reason="search_disabled", expected="use the accepted evidence"))
                        state.errors.append(AgentError(code="chat_search_disabled",
                            message=("Invalid tool request: Cannot use 'search evidence'" if disabled == "search_evidence"
                                     else "Invalid tool request: Cannot use 'search guidelines'")))
                        return finish("invalid_response")
                    stage = "chat_search_plan"
                    chat_search_tools = bounded_call(lambda: set(self.registry.names()).intersection(_CHAT_SEARCH_TYPES))
                    calls, skipped, kept_chat_keys, next_chat_target, close_chat_search = bounded_call(lambda: _prepare_chat_searches(
                        turn.tool_calls, selection_snapshot, evidence, chat_target, chat_search_tools))
                attempted_calls = len(calls) + sum(outcome.error.code != "chat_search_budget_exhausted"
                    for outcome in skipped.values())
                if attempted_calls and (feed_search_closed or state.tool_round >= (min(MAX_FEED_SEARCH_ROUNDS, self.max_tool_rounds) if feed_candidates is not None else self.max_tool_rounds) or
                        state.total_tool_calls + attempted_calls > self.max_total_tool_calls):
                    log_failure(None, failure_stage="tool_admission", code="tool_limit",
                        call_id=[call.id for call in turn.tool_calls], origin="llm_arguments",
                        diagnostic=dict(reason="tool_limit_reached", search_closed=feed_search_closed,
                            rounds_used=state.tool_round, calls_used=state.total_tool_calls,
                            requested_calls=attempted_calls, round_limit=(min(MAX_FEED_SEARCH_ROUNDS, self.max_tool_rounds)
                                if feed_candidates is not None else self.max_tool_rounds),
                            call_limit=self.max_total_tool_calls))
                    state.errors.append(AgentError(code="tool_limit", message="Tool 실행 상한에 도달했습니다."))
                    return finish("limit_reached")
                ids = [call.id for call in turn.tool_calls]
                if len(set(ids)) != len(ids) or seen_ids.intersection(ids):
                    log_failure(None, failure_stage="tool_call_linkage", code="duplicate_call_id",
                        call_id=ids, origin="llm_arguments", diagnostic=dict(reason="duplicate_call_id",
                            expected="unique new Tool Call IDs within this request"))
                    state.errors.append(AgentError(code="duplicate_call_id", message="LLM Tool Call ID가 중복되었습니다."))
                    return finish("invalid_response")
                if feed_candidates is not None:
                    feed_model_calls += len(turn.tool_calls)
                    try:
                        stage = "feed_search_plan"
                        search_plan = bounded_call(lambda: prepare_feed_searches(
                            calls, turn=state.tool_round + 1,
                            remaining_calls=min(MAX_FEED_SEARCHES, self.max_total_tool_calls) - len(feed_query_keys),
                            remaining_budget=MAX_FEED_RETRIEVAL - feed_retrieval_used,
                            previous_queries=feed_query_keys,
                            reserved_ids=seen_ids | evidence.call_ids))
                        staged_candidates = deepcopy(feed_candidates)
                        stage = "feed_candidate_review"
                        bounded_call(lambda: staged_candidates.apply_review(search_plan.review, selection_snapshot))
                        calls = search_plan.searches
                    except (FeedSearchInputError, FeedDecisionError) as exc:
                        if isinstance(exc, FeedDecisionError) and exc.code != "invalid_feed_review":
                            log_failure(exc)
                            state.errors.append(AgentError(code=exc.code,
                                message="Feed 후보 검토 정보가 계약과 일치하지 않습니다."))
                            return finish("invalid_response")
                        code = exc.code if isinstance(exc, FeedDecisionError) else "invalid_arguments"
                        error = AgentError(code=code, message=(
                            diagnostic_message(exc.diagnostic) if isinstance(exc, FeedDecisionError) and exc.diagnostic else
                            "현재 후보 전체에 대응하는 review.decisions를 지정하세요."
                            if isinstance(exc, FeedDecisionError) else exc.message))
                        failed_call_id = getattr(exc, "call_id", None)
                        if isinstance(exc, FeedSearchInputError) and exc.diagnostic.get("reason") == "conflicting_reviews":
                            failed_call_id = None  # A shared review conflict is a turn-wide error.
                        can_retry = feed_argument_recoveries < MAX_FEED_ARGUMENT_RECOVERIES
                        log_failure(exc, failure_stage=getattr(exc, "stage", stage),
                            call_id=failed_call_id if failed_call_id is not None else ids,
                            diagnostic=getattr(exc, "diagnostic", None),
                            origin="llm_arguments", code=code,
                            action=("llm_retry" if can_retry else "finalize_existing_evidence"
                                    if feed_candidates.successful_searches else "abort"))
                        state.errors.append(error)
                        # Reject the entire unadmitted turn without executing RAG or
                        # changing candidates/review. Acknowledge EVERY original call.
                        not_executed = AgentError(code=code,
                            message="같은 Turn의 다른 호출 검증 실패로 실행하지 않음")
                        outcomes = [ToolOutcome(call_id=call.id, tool_name=call.name,
                            success=False, error=(error if failed_call_id is None or call.id == failed_call_id
                                                  else not_executed)) for call in turn.tool_calls]
                        seen_ids.update(ids)
                        stage = "conversation_error"
                        bounded_call(lambda: conversation.append_turn(turn, outcomes))
                        if not can_retry:
                            if not feed_candidates.successful_searches:
                                return finish("failed")
                            feed_search_closed = True
                        else:
                            feed_argument_recoveries += 1
                        continue
                    except FeedSearchBudgetExceeded as exc:
                        log_failure(exc, code="tool_limit", call_id=ids, origin="llm_arguments", diagnostic=exc.diagnostic)
                        state.errors.append(AgentError(code='tool_limit', message='Feed 실제 검색 예산을 초과했습니다.'))
                        return finish('limit_reached')
                    feed_retrieval_used += sum(call.arguments["top_k"] for call in calls)
                    feed_query_keys.update(search_plan.query_keys)
                if output_model is ChatAnswerV1:
                    chat_target = next_chat_target
                    chat_search_closed.update(close_chat_search)
                    if kept_chat_keys != chat_keys:
                        chat_keys = kept_chat_keys
                        bounded_call(lambda: evidence.snapshot("chat", chat_keys))
                seen_ids.update(ids)
                if attempted_calls:
                    state.tool_round += 1
                stage = "executor_error"
                unaccepted = {}
                input_origins = ({call.id: "feed_search_plan" for call in calls} if feed_candidates is not None else
                    {call.id: "chat_search_plan" for call in calls if call.name in chat_search_tools}
                    if output_model is ChatAnswerV1 else {})
                results = self._tool_results(calls, context, state, stop_on_timeout=feed_candidates is not None,
                                             unaccepted=unaccepted if feed_candidates is not None else None,
                                             input_origins=input_origins)
                if skipped:
                    executed_results = iter(results)
                    results = ((call, skipped[call.id], None) if call.id in skipped else next(executed_results)
                               for call in turn.tool_calls)
                if feed_candidates is not None:
                    # Consume and validate the whole batch before exposing evidence.
                    results = list(results)
                    state.called_tools.extend(trace for _, _, trace in results)
                    state.errors.extend(outcome.error for _, outcome, _ in results if outcome.error)
                    failures = {call.id: outcome.error for call, outcome, _ in results if not outcome.success}
                    # Timeouts, scope/contract violations and server defects remain hard stops.
                    # Ordinary search failures affect only that search. The model
                    # decides whether to search further within the existing budget.
                    # PermissionError has a fixed server-owned message while retaining
                    # tool_exception. Do not inspect exception text to infer permissions.
                    if any(error.code not in ("tool_exception", "empty_result")
                           or error.message == PERMISSION_FAILURE_MESSAGE for error in failures.values()):
                        logger.info("agent_feed_search request_id=%r llm_turn=%s provider_call_ids=%r "
                                    "stage=feed_search_results recovery_action=abort error_codes=%s",
                                    context.request_id, llm_turn, [diagnostic_call_id(identifier) for identifier in ids],
                                    [error.code for error in failures.values()])
                        return finish("failed")
                    stage = "evidence_validation_error"
                    packages = {}
                    for call, outcome, _ in results:
                        if not outcome.success:
                            continue
                        package = bounded_call(lambda: EvidencePackage.model_validate(outcome.data))
                        if len(package.evidence) > call.arguments["top_k"]:
                            raise ValueError("Feed search returned more than its retrieval limit")
                        packages[call.id] = package
                    if failures or unaccepted:
                        logger.info("agent_feed_search request_id=%r llm_turn=%s provider_call_ids=%r "
                                    "stage=feed_search_results completed=%s failed=%s unaccepted=%s "
                                    "search_calls_started=%s search_calls_reserved=%s recovery_action=%s",
                                    context.request_id, llm_turn, [diagnostic_call_id(identifier) for identifier in ids],
                                    len(packages), len(failures),
                                    list(unaccepted.values()), state.total_tool_calls, len(feed_query_keys),
                                    "llm_decision")
                    stage = "candidate_source_mismatch"
                    for call, outcome, _ in results:
                        if not outcome.success:
                            staged_candidates.record_search(False)
                            continue
                        package = packages[call.id]
                        before_count = staged_candidates.eligible_count
                        bounded_call(lambda: staged_candidates.add(call.id, package,
                            provider_call_id=search_plan.provider_call_id(call.id)))
                        staged_candidates.record_search(True, len(package.evidence),
                            staged_candidates.eligible_count - before_count)
                    for status in unaccepted.values():
                        feed_unaccepted[status] += 1
                    check_deadline()
                    if failures or unaccepted:
                        if not packages:
                            # Do not apply an unfulfilled review to previous candidates.
                            staged_candidates = deepcopy(feed_candidates)
                            # EvidenceStore is shared, append-only source authority.
                            # apply_review already published a staged projection; create
                            # a fresh validated projection of the last disclosed keys.
                            staged_candidates.snapshot = bounded_call(lambda: staged_candidates.store.snapshot(
                                "feed", selection_snapshot.keys))
                            for _ in failures:
                                staged_candidates.record_search(False)
                    if packages:
                        staged_candidates.finish_round()
                    feed_candidates = staged_candidates
                    outcomes = search_plan.provider_outcomes(packages, failures, unaccepted)
                else:
                    # Default Chat/legacy mode still validates and commits each
                    # sequential call before starting the next dependent call.
                    outcomes = []
                    for call, outcome, trace in results:
                        if (output_model is ChatAnswerV1 and call.name in _CHAT_SEARCH_TYPES and outcome.success
                                and (len(outcome.data["evidence"]) > call.arguments["top_k"]
                                     or any(item["evidence_type"] != _CHAT_SEARCH_TYPES[call.name] for item in outcome.data["evidence"]))):
                            outcome = ToolOutcome(call_id=call.id, tool_name=call.name, success=False,
                                error=AgentError(code="invalid_output", message="검색 결과의 자료 유형 또는 반환 개수가 계약과 일치하지 않습니다."))
                            trace = trace.model_copy(update={"success": False, "error_type": "invalid_output", "result_count": None})
                        outcomes.append(outcome)
                        if trace is None:
                            # Skips never claim execution/success; malformed reviews remain visible errors.
                            if outcome.error.code != "chat_search_budget_exhausted":
                                state.total_tool_calls += 1
                                state.called_tools.append(ToolTrace(tool_name=call.name, success=False,
                                    duration_ms=0, error_type=outcome.error.code))
                                state.errors.append(outcome.error)
                            continue
                        state.called_tools.append(trace)
                        if outcome.error:
                            state.errors.append(outcome.error)
                            if outcome.error.code in ("tool_timeout", "patient_context_mismatch"):
                                return finish("failed")
                        if (outcome.success and (spec := self.registry.get(call.name)) is not None
                                and spec.contract.output_model is EvidencePackage):
                            stage = "evidence_validation_error"
                            package = bounded_call(lambda: EvidencePackage.model_validate(outcome.data))
                            candidates = bounded_call(lambda: evidence.add_search_result(call.id, package))
                            if output_model is ChatAnswerV1:
                                bounded_call(lambda: _add_chat_candidates(candidates, chat_keys, chat_pmids, chat_dois, chat_guidelines, evidence))
                                bounded_call(lambda: evidence.snapshot("chat", chat_keys))
                        stage = "executor_error"
                stage = "conversation_error"
                bounded_call(lambda: conversation.append_turn(turn, outcomes,
                    project_search_results=output_model is ChatAnswerV1,
                    evidence_call_ids=evidence.call_ids))
                successful_tool_names.update(outcome.tool_name for outcome in outcomes if outcome.success)
                # Only server-owned Contract text becomes policy. Keep the entire
                # Provider output/result bundle contiguous and in original order.
                for outcome in outcomes:
                    if outcome.success and outcome.tool_name not in delivered_policy_tools:
                        spec = self.registry.get(outcome.tool_name)
                        if spec is not None and spec.contract.data_policy:
                            conversation.items.append(Message(role="system", kind="policy",
                                content=spec.contract.data_policy))
                            delivered_policy_tools.add(outcome.tool_name)
        except RequestTimeout as exc:
            log_failure(exc, failure_stage=stage, code="request_timeout")
            state.final_answer = None
            error = AgentError(code="request_timeout", message="Agent 전체 실행 시간이 초과되었습니다.")
            state.errors.append(error)
            # 만료된 deadline을 다시 검사하지 않는다. 실패 metadata 검증도 실패하면 최소 timeout 결과만 반환한다.
            try:
                return build_result("failed")
            except Exception:
                return minimal_failure(error)
        except Exception as exc:
            log_failure(exc)
            state.final_answer = None
            state.errors.append(AgentError(code=stage, message="Agent 내부 실행 단계에 실패했습니다."))
            return finish("failed")
        finally:
            request_deadline.reset(token)
            tool_diagnostic_context.reset(diagnostic_token)
