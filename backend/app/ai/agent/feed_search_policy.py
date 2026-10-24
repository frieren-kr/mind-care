"""Feed-only search admission; shared Tool input and Chat remain unchanged."""
import json

from .schemas import ToolCall
from .tools.contracts import EvidenceSearchInput

FEED_SEARCH_TOP_K = 8
MAX_FEED_RETRIEVAL = 24


def bounded_feed_searches(calls: list[ToolCall], *, remaining_budget: int = MAX_FEED_RETRIEVAL
                          ) -> list[ToolCall]:
    """Validate the entire turn before I/O: <=8 per search, <=24 per request.

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
