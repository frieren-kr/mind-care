"""Feed-only search admission; shared Tool input and Chat remain unchanged."""
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, StrictStr, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from .error_diagnostics import diagnostic_message, summarize_json_error, summarize_validation_error
from .schemas import AgentError, AgentModel, ToolCall, ToolOutcome
from .feed_candidates import FeedReview
from .outputs import _json_object, _reject_constant
from .tools.contracts import EvidenceSearchInput

FEED_SEARCH_TOP_K = 8
MAX_FEED_SEARCH_ROUNDS = 3
MAX_FEED_SEARCHES_PER_ROUND = 3
MAX_FEED_SEARCHES = 9
MAX_FEED_ARGUMENT_RECOVERIES = 2
MAX_FEED_RETRIEVAL = MAX_FEED_SEARCHES * FEED_SEARCH_TOP_K


class FeedSearchInput(AgentModel):
    review: FeedReview | None = Field(default=None, description=(
        "후속 검색에는 현재 후보 전체 순서에 대응하는 decisions 배열을 전달한다. include=true인 유지 후보에는 이유/category를 작성한다. "
        "유지하지 않는 현재 후보는 검토 완료로 처리한다. 첫 검색에는 생략한다."))
    queries: list[Annotated[StrictStr, Field(min_length=1, max_length=2000)]] = Field(
        min_length=1, max_length=3, description=(
            "전체 최소화된 Context에서 LLM이 선택한 독립 연구 검색어 1~3개. "
            "연구 목적마다 별도 배열 항목을 작성한다. 간결한 영어 우선, 한국어도 허용. "
            "환자 증상은 환자 중심으로 표현하고 개인정보를 복사하지 않는다. "
            "검색어당 서버 top_k=8; 라운드당 최대 3회, 3라운드/요청 전체 최대 9회다."))

    @field_validator('queries', mode='before')
    @classmethod
    def json_array_only(cls, value):
        if type(value) is not list:
            raise PydanticCustomError('feed_query_array', 'Feed requires a JSON string array')
        return value

    @field_validator('queries')
    @classmethod
    def independent_entries(cls, value):
        keys = [query_key(q) for q in value]
        if any(not key for key in keys):
            raise PydanticCustomError('feed_query_blank', 'Blank Feed query')
        if len(keys) != len(set(keys)):
            raise PydanticCustomError('feed_query_duplicate', 'Repeated Feed query')
        if any(any(c in q for c in '\r\n;') for q in value):
            raise PydanticCustomError('feed_query_delimiter', 'Use separate array entries, not line or semicolon delimiters')
        return value  # Valid query text is preserved; no rewriting/translation.


def query_key(query: str) -> str:
    return ' '.join(query.split()).casefold()


def feed_tool_definitions(definitions: list[dict]) -> list[dict]:
    """Request-local input adapter. Registry/Executor/Chat stay canonical."""
    result = deepcopy(definitions)
    for definition in result:
        if definition['name'] == 'search_evidence':
            definition['parameters'] = FeedSearchInput.model_json_schema()
            definition['description'] += (
                ' Feed에서는 독립 연구 목적별 queries 배열을 사용한다. 별도의 Planner 호출은 없다.')
    return result


class FeedSearchBudgetExceeded(ValueError):
    def __init__(self, message="Feed actual search budget exceeded", *, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic or {}


class FeedSearchInputError(ValueError):
    """Original LLM input rejected before server conversion or execution."""
    def __init__(self, call, *, stage="feed_search_plan",
                 diagnostic=None, reason="invalid_search_plan", expected="valid Feed search input"):
        super().__init__("Invalid Feed search input")
        self.call_id = call.id
        self.stage = stage
        self.diagnostic = diagnostic or dict(reason=reason, expected=expected)
        self.message = (diagnostic_message(diagnostic) if diagnostic else expected)


def _validate_input(model, raw, call, *, stage="feed_search_input_validation"):
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        error = FeedSearchInputError(call, stage=stage,
            diagnostic=summarize_validation_error(exc, model))
        raise error from exc


@dataclass(frozen=True)
class FeedSearchPlan:
    # Every batch owns one REAL Provider call; searches own server-only IDs.
    batches: list[tuple[ToolCall, list[ToolCall]]]
    query_keys: set[str]
    review: FeedReview | None = None

    @property
    def searches(self) -> list[ToolCall]:
        return [search for _, searches in self.batches for search in searches]

    def provider_call_id(self, search_id: str) -> str:
        for call, searches in self.batches:
            if any(search.id == search_id for search in searches):
                return call.id
        raise ValueError("Search is not in the admitted batch")

    def provider_outcomes(self, packages: dict, failures: dict | None = None,
                          unaccepted: dict | None = None) -> list[ToolOutcome]:
        """One real Provider acknowledgement; only validated packages are successes.

        Pending work is not called a failure/completion: a running Python thread
        cannot be cancelled. Later, unstarted batches are explicitly cancelled.
        """
        failures, unaccepted = failures or {}, unaccepted or {}
        partial = bool(failures or unaccepted)
        outcomes = []
        for call, searches in self.batches:
            rows = []
            for i, search in enumerate(searches):
                if search.id in packages:
                    row = dict(query_index=i, returned_evidence_count=len(packages[search.id].evidence))
                    if partial:
                        row['status'] = 'completed'
                elif search.id in failures:
                    row = dict(query_index=i, status='failed', error=failures[search.id].model_dump())
                else:
                    row = dict(query_index=i, status=unaccepted[search.id])
                rows.append(row)
            success = any(search.id in packages for search in searches)
            outcomes.append(ToolOutcome(call_id=call.id, tool_name=call.name, success=success,
                data={'candidate_list': 'See the latest untrusted_feed_candidates message', 'searches': rows},
                error=None if success else AgentError(code='feed_search_failed',
                    message='이 호출에서 수락된 검색 결과가 없습니다. 최신 후보와 검색 상태만 사용하세요.')))
        return outcomes


def prepare_feed_searches(calls: list[ToolCall], *, turn: int, remaining_calls: int,
                          remaining_budget: int, previous_queries: set[str],
                          reserved_ids: set[str]) -> FeedSearchPlan:
    """Validate the WHOLE model turn before admission; never infer topics."""
    batches, keys, reviews = [], set(), []
    occupied = reserved_ids | {call.id for call in calls}
    for call_index, call in enumerate(calls):
        if call.name != 'search_evidence':
            raise FeedSearchInputError(call, reason='unsupported_tool', expected='search_evidence only')
        if isinstance(call.arguments, str):
            if len(call.arguments) > 65536:
                raise FeedSearchInputError(call, reason='arguments_too_large', expected='JSON arguments <=65536 characters')
            try:
                raw = json.loads(call.arguments, object_pairs_hook=_json_object, parse_constant=_reject_constant)
            except json.JSONDecodeError as exc:
                raise FeedSearchInputError(call, stage="feed_search_arguments_json",
                    diagnostic=summarize_json_error(exc)) from exc
            except ValueError as exc:
                raise FeedSearchInputError(call, stage="feed_search_arguments_json",
                    reason="invalid_json_object", expected="JSON object without duplicate keys or non-finite values") from exc
        else:
            raw = call.arguments
        if isinstance(raw, dict) and 'queries' in raw:
            parsed = _validate_input(FeedSearchInput, raw, call)
            queries = parsed.queries
            if parsed.review is not None:
                reviews.append(parsed.review)
            searches = []
            for index, query in enumerate(queries):
                identifier = f'feed-rag:{turn}:{call_index}:{index}'
                while identifier in occupied:
                    identifier += '#'
                occupied.add(identifier)
                searches.append(ToolCall(id=identifier, name=call.name,
                    arguments={'query': query, 'top_k': FEED_SEARCH_TOP_K}))
        else:
            # Compatibility only for existing pre-array callers. Not advertised
            # in the Feed schema. Never split their query on commas/delimiters.
            if call.id in reserved_ids:
                raise ValueError('Feed source ID already used')
            if isinstance(raw, dict):
                raw = dict(raw)
                review = raw.pop('review', None)
                if review is not None:
                    reviews.append(_validate_input(FeedReview, review, call, stage='feed_review_validation'))
            # Validate the original input once, then apply the Feed-only default.
            # Executor still validates the generated arguments at its boundary.
            if not isinstance(raw, dict) or 'query' not in raw:
                _validate_input(FeedSearchInput, raw, call)
            original = _validate_input(EvidenceSearchInput, raw, call, stage="feed_legacy_input_validation")
            if original.top_k > FEED_SEARCH_TOP_K:
                raise FeedSearchInputError(call, reason="retrieval_limit", expected="top_k <=8")
            arguments = original.model_dump()
            if 'top_k' not in raw:
                arguments['top_k'] = FEED_SEARCH_TOP_K
            searches = [call.model_copy(deep=True, update={'arguments': arguments})]

        for search in searches:
            key = query_key(search.arguments['query'])
            if key in previous_queries or key in keys:
                raise FeedSearchInputError(call, reason='duplicate_query', expected='use distinct queries not searched in this request')
            keys.add(key)
        batches.append((call, searches))
    if reviews and any(review != reviews[0] for review in reviews):
        raise FeedSearchInputError(call, reason="conflicting_reviews", expected="one consistent review for the current snapshot")
    plan = FeedSearchPlan(batches, keys, reviews[0] if reviews else None)
    search_count = len(plan.searches)
    retrieval = sum(s.arguments['top_k'] for s in plan.searches)
    if not 1 <= search_count <= MAX_FEED_SEARCHES_PER_ROUND:
        reason = "feed_round_search_limit"
    elif search_count > remaining_calls:
        reason = "feed_remaining_search_limit"
    elif retrieval > remaining_budget:
        reason = "feed_retrieval_limit"
    else:
        return plan
    raise FeedSearchBudgetExceeded(diagnostic=dict(reason=reason,
        search_count=search_count, per_round_limit=MAX_FEED_SEARCHES_PER_ROUND,
        remaining_calls=remaining_calls, requested_retrieval=retrieval, remaining_retrieval=remaining_budget))


def bounded_feed_searches(calls: list[ToolCall], *, remaining_budget: int = MAX_FEED_RETRIEVAL
                          ) -> list[ToolCall]:
    """Validate the entire turn before I/O: <=8 per search, <=72 per request (<=24 per round).

    Query text/Call IDs are preserved. Original assistant messages retain the
    requested arguments; Executor-bound copies supply the Feed-only default 8.
    No extra searches are issued to fill missing or duplicate results.
    """
    if (not 1 <= len(calls) <= 3 or type(remaining_budget) is not int
            or not 0 <= remaining_budget <= MAX_FEED_RETRIEVAL):
        raise ValueError("Invalid Feed search batch size")
    bounded = []
    for call in calls:
        if call.name != "search_evidence":
            raise ValueError("Feed only supports search_evidence")
        raw = json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
        if isinstance(raw, dict) and "top_k" not in raw:
            raw = {**raw, "top_k": FEED_SEARCH_TOP_K}
        args = EvidenceSearchInput.model_validate(raw)
        if args.top_k > FEED_SEARCH_TOP_K:
            raise ValueError("Feed per-search top_k cannot exceed 8")
        bounded.append(call.model_copy(deep=True, update={
            "arguments": args.model_dump()}))
    if sum(call.arguments["top_k"] for call in bounded) > remaining_budget:
        raise ValueError("Feed retrieval budget exceeded")
    return bounded
