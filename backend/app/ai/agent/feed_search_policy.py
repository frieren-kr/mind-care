"""Feed-only search admission; shared Tool input and Chat remain unchanged."""
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, StrictStr, field_validator

from .schemas import AgentModel, ToolCall, ToolOutcome
from .feed_candidates import FeedReview
from .outputs import _json_object, _reject_constant
from .tools.contracts import EvidenceSearchInput

FEED_SEARCH_TOP_K = 8
MAX_FEED_SEARCH_ROUNDS = 3
MAX_FEED_SEARCHES_PER_ROUND = 3
MAX_FEED_SEARCHES = 9
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
            raise ValueError('Feed requires a JSON string array')
        return value

    @field_validator('queries')
    @classmethod
    def independent_entries(cls, value):
        keys = [query_key(q) for q in value]
        if any(not key for key in keys) or len(keys) != len(set(keys)):
            raise ValueError('Blank or repeated Feed query')
        if any(any(c in q for c in '\r\n;') for q in value):
            raise ValueError('Use separate array entries, not line or semicolon delimiters')
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
    pass


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

    def provider_outcomes(self, packages: dict) -> list[ToolOutcome]:
        return [ToolOutcome(call_id=call.id, tool_name=call.name, success=True, data={
            'candidate_list': 'See the latest untrusted_feed_candidates message',
            'searches': [{'query_index': i, 'returned_evidence_count': len(packages[s.id].evidence)}
                         for i, s in enumerate(searches)],
        }) for call, searches in self.batches]


def prepare_feed_searches(calls: list[ToolCall], *, turn: int, remaining_calls: int,
                          remaining_budget: int, previous_queries: set[str],
                          reserved_ids: set[str]) -> FeedSearchPlan:
    """Validate the WHOLE model turn before admission; never infer topics."""
    batches, keys, reviews = [], set(), []
    occupied = reserved_ids | {call.id for call in calls}
    for call_index, call in enumerate(calls):
        if call.name != 'search_evidence':
            raise ValueError('Feed only supports search_evidence')
        if isinstance(call.arguments, str):
            if len(call.arguments) > 65536:
                raise ValueError('Feed arguments too large')
            raw = json.loads(call.arguments, object_pairs_hook=_json_object, parse_constant=_reject_constant)
        else:
            raw = call.arguments
        if isinstance(raw, dict) and 'queries' in raw:
            parsed = FeedSearchInput.model_validate(raw)
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
                    reviews.append(FeedReview.model_validate(review))
            searches = bounded_feed_searches([call.model_copy(update={'arguments': raw})])
        for search in searches:
            key = query_key(search.arguments['query'])
            if key in previous_queries or key in keys:
                raise ValueError('Feed query already searched')
            keys.add(key)
        batches.append((call, searches))
    if reviews and any(review != reviews[0] for review in reviews):
        raise ValueError("Conflicting Feed reviews in one model turn")
    plan = FeedSearchPlan(batches, keys, reviews[0] if reviews else None)
    if (not 1 <= len(plan.searches) <= MAX_FEED_SEARCHES_PER_ROUND
            or len(plan.searches) > remaining_calls or
            sum(s.arguments['top_k'] for s in plan.searches) > remaining_budget):
        raise FeedSearchBudgetExceeded('Feed actual search budget exceeded')
    return plan


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
