"""Search admission, budgets and candidate selection for structured Chat."""
from copy import deepcopy
import json
import logging

from pydantic import ValidationError

from ..error_diagnostics import diagnostic_message, summarize_json_error, summarize_validation_error
from ..executor import diagnostic_call_id, tool_diagnostic_context
from ..schemas import AgentError, Message, ToolOutcome
from ..tools.contracts import EvidenceSearchInput, GuidelineSearchInput


# Keep the existing diagnostic logger used by the execution path.
logger = logging.getLogger("app.ai.agent.agent_loop_runner")

CHAT_DEFAULT_TOP_K = 3
MAX_CHAT_PAPERS = 7
MAX_CHAT_GUIDELINES = 4
MAX_CHAT_CANDIDATES = MAX_CHAT_PAPERS + MAX_CHAT_GUIDELINES
CHAT_SEARCH_TYPES = {"search_evidence": "new_research", "search_guidelines": "guideline"}


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
        kind = CHAT_SEARCH_TYPES[call.name]
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


def chat_search_budget_message(keys, store, target, closed, registry):
    counts = _chat_candidate_counts(keys, store)
    return Message(role="system", kind="agent_context", content=json.dumps({
        "context_type": "chat_search_budget", "candidate_limit": MAX_CHAT_CANDIDATES,
        "papers": {"active_count": counts["new_research"], "target_top_k": target,
            "candidate_limit": MAX_CHAT_PAPERS, "remaining_candidate_budget": target - counts["new_research"],
            "search_available": registry.get("search_evidence") is not None and "search_evidence" not in closed},
        "guidelines": {"active_count": counts["guideline"], "candidate_limit": MAX_CHAT_GUIDELINES,
            "remaining_candidate_budget": MAX_CHAT_GUIDELINES - counts["guideline"],
            "search_available": registry.get("search_guidelines") is not None and "search_guidelines" not in closed}}))


def chat_tool_definitions(definitions: list[dict], target: int, closed: set[str]) -> list[dict]:
    definitions = deepcopy(definitions)
    if closed:
        definitions = [definition for definition in definitions if definition["name"] not in closed]
    for definition in definitions:
        if definition["name"] == "search_evidence":
            definition["parameters"]["properties"]["top_k"].update(
                default=target, maximum=MAX_CHAT_PAPERS,
                description="논문 목표 전체 후보 수(유지 논문+신규 논문). 최초 생략 시 3, 이후 생략 시 현재 목표 유지. 필요한 만큼만 확장하며 7을 초과하면 서버가 7로 축소한다.")
        if definition["name"] in CHAT_SEARCH_TYPES:
            definition["parameters"]["properties"].update(
                exclude_candidates={"type": "array", "items": {"type": "integer", "minimum": 0},
                    "uniqueItems": True, "description": "근거가 부족해 제외할 최신 후보의 0-based 인덱스. 생략 시 모두 유지. 같은 Turn 첫 검색에만 제공한다."},
                candidate_version={"type": "integer", "minimum": 1,
                    "description": "exclude_candidates가 비어 있지 않으면 최신 untrusted_chat_candidates의 candidate_version을 그대로 전달한다."})
    return definitions


def chat_search_result_diagnostic(call, outcome: ToolOutcome) -> dict | None:
    if call.name not in CHAT_SEARCH_TYPES or not outcome.success:
        return None
    items = outcome.data["evidence"]
    top_k, expected_type = call.arguments["top_k"], CHAT_SEARCH_TYPES[call.name]
    exceeds_limit = len(items) > top_k
    if not exceeds_limit and all(item["evidence_type"] == expected_type for item in items):
        return None
    return dict(reason="retrieval_limit_exceeded" if exceeds_limit else "evidence_type_mismatch",
        returned_count=len(items), top_k=top_k, expected_type=expected_type,
        returned_types=sorted({item["evidence_type"] for item in items}))
