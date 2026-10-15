"""논문 추천 API (박주현 담당).

GET /recommendations/{user_id}?patient_id=&k=5   간병인(관리 환자 전체)에 맞는 논문 최대 5편

흐름:
  ① get_user_profile_context()로 사용자·환자 확인 (404 판정, 검색어 재료)   [한슬님 함수]
  ② AI 방식 (mode="ai") — 웅 님 FeedWorkflow 3단계, 정식(간병인 기준) 경로
       context_prepare → feed_execution → backend_handoff
       context_prepare = get_feed_context_snapshot() → ProductionFeedContextSource
                         → DefaultFeedContextLoader → prepare_production_feed_input()
  ③ AI 방식을 못 쓰면 검색 방식 (mode="search")으로 대신 응답
       환자마다 build_query() → search_by_queries() → get_paper_details_by_pmids()   [현서님 함수]

AI 피드 저장 기간 (10/14 회의): AI 결과는 feed_items에 묶음으로 저장하고, 기간(기본 3일) 안에는
실행기를 다시 부르지 않고 저장본을 돌려준다. refresh=true면 기간과 상관없이 새로 만든다.
검색 방식 결과는 저장하지 않는다.

로그인이 아직 없어서 user_id를 경로로 받는다 (api/users.py와 같은 방식).
"""
import logging
from datetime import datetime, timedelta
from typing import Callable, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query

from app.ai.agent.outputs import StructuredAgentResult
from app.ai.agent.schemas import AgentContext
from app.ai.agent.workflows.feed import FeedWorkflow, FeedWorkflowError
from app.ai.agent.workflows.feed_context_backend_source import ProductionFeedContextSource
from app.ai.agent.workflows.feed_context_loader import DefaultFeedContextLoader
from app.ai.agent.workflows.feed_context_prepare import prepare_production_feed_input
from app.ai.retrieval.search import search_by_queries
from app.api.evidence_grade import with_grades
from app.db import functions, user_functions
from app.db import feed_read
from app.db.feed_read import get_feed_context_snapshot
from app.feed_schemas import FeedItemIn
from app.schemas import RecommendationItem, RecommendationResponse
from app.user_schemas import APP_TIMEZONE, PatientOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

_USER_NOT_FOUND = "사용자를 찾을 수 없습니다."
_PATIENT_NOT_FOUND = "환자를 찾을 수 없습니다."

# 관리 환자가 0명일 때 검색 방식이 쓰는 일반 검색어.
GENERAL_QUERY = "치매 가족 돌봄에 도움이 되는 연구"

FeedExecution = Callable[[AgentContext, str], StructuredAgentResult]

# AI 피드 저장 기간 기본값 (10/14 회의).
FEED_CACHE_DAYS = 3


def _example(detail: str) -> dict:
    """OpenAPI 문서(/docs)에 보여줄 오류 응답 예시."""
    return {"description": detail, "content": {"application/json": {"example": {"detail": detail}}}}


def get_feed_execution() -> Optional[FeedExecution]:
    """FeedWorkflow의 2단계(feed_execution)를 돌려주는 FastAPI 의존성.

    ★ 웅 님 search_evidence 핸들러·레지스트리·LLM 클라이언트 함수로 교체 예정 ★
    지금은 None을 돌려주고, None이면 엔드포인트가 검색 방식으로 대신 응답한다.
    1단계가 정식(간병인 기준) 경로이므로, 교체할 함수는 feed_scope="caregiver_all"
    입력(관리 환자 0~20명)을 받는다. context.patient_id는 항상 None이다.
    테스트에서는 app.dependency_overrides[get_feed_execution]으로 바꿔 끼운다.
    """
    return None


def get_feed_cache_days(user_id: UUID) -> int:
    """이 사용자의 AI 피드 저장 기간(일).

    ★ 한슬 님 사용자별 설정(1~7일)이 생기면 여기서 그 값을 읽어 돌려준다 (없으면 FEED_CACHE_DAYS) ★
    """
    return FEED_CACHE_DAYS


def _now() -> datetime:
    """저장 기간을 재는 현재 시각 (tz-aware). 테스트에서 바꿔 끼운다."""
    return datetime.now(APP_TIMEZONE)


def load_cached_feed(user_id: UUID) -> Optional[tuple[datetime, list[RecommendationItem]]]:
    """기간 안의 저장본 (생성 시각, 항목). 없거나 읽기에 실패하면 None (→ 새로 만든다)."""
    days = get_feed_cache_days(user_id)
    try:
        batch = feed_read.get_latest_feed_batch(user_id, max_age=timedelta(days=days), reference_time=_now())
    except Exception as exc:  # 009 미적용 등. 저장본이 없을 때처럼 새로 만든다
        logger.warning("피드 저장본 읽기 실패 → 새로 만든다 (%s)", type(exc).__name__)
        return None
    if batch is None:
        return None
    items = [
        RecommendationItem(**item.paper.model_dump(), personal_reason=item.personal_reason,
                           category=item.category, relevance_score=item.relevance_score)
        for item in batch.items
    ]
    return batch.generated_at, with_grades(items)


def save_feed(user_id: UUID, items: list[RecommendationItem]) -> Optional[datetime]:
    """AI 피드를 한 묶음으로 저장하고 생성 시각을 돌려준다. 실패하면 None (응답은 그대로 나간다)."""
    try:
        saved = feed_read.save_feed_batch(user_id, [
            FeedItemIn(paper_id=i.id, personal_reason=i.personal_reason, category=i.category,
                       relevance_score=i.relevance_score)
            for i in items
        ])
    except Exception as exc:
        logger.warning("피드 저장 실패 → 저장 없이 응답 (%s)", type(exc).__name__)
        return None
    return saved.generated_at


def build_query(patient: PatientOut) -> str:
    """환자 단계·증상·관심사로 한국어 검색어 한 줄을 만든다 (검색 방식 전용).

    값을 문장에 끼워 넣기만 한다 (간병인 자가점검, 평가 점수, 안전 기록 등은 쓰지 않는다).
    """
    subject = f"치매 환자({patient.dementia_stage})" if patient.dementia_stage else "치매 환자"
    parts = []
    if patient.symptoms:
        parts.append(f"{', '.join(patient.symptoms)} 증상")
    if patient.interests:
        parts.append(", ".join(patient.interests))
    if not parts:
        return f"{subject} 돌봄에 도움이 되는 연구"
    return f"{subject}의 {'과 '.join(parts)}에 도움이 되는 연구"


def search_recommendations(
    user_id: UUID, patient_id: Optional[UUID], patients: list[PatientOut], k: int
) -> RecommendationResponse:
    """검색 방식 (mode="search"). AI 방식을 못 쓸 때의 대신 응답이기도 하다.

    환자마다 검색어를 하나씩 만들어 search_by_queries()로 합친다 (환자별로 골고루 뽑힌다).
    환자가 0명이면 일반 검색어 하나를 쓴다.
    """
    # 프로필이 같은 환자는 검색어도 같으므로 한 번만 쓴다 (순서 유지).
    queries = list(dict.fromkeys(build_query(p) for p in patients)) or [GENERAL_QUERY]
    evidence = search_by_queries(queries, k).evidence
    details = functions.get_paper_details_by_pmids([e.pmid for e in evidence])

    items = []
    for e in evidence:
        paper = details.get(e.pmid)
        if paper is None:  # 검색과 조회 사이에 삭제된 경우
            continue
        # 이름이 같은 필드만 옮겨진다 (초록 등 나머지는 버려진다).
        items.append(RecommendationItem(**paper.model_dump(), relevance_score=e.relevance_score))
    return RecommendationResponse(
        user_id=user_id, patient_id=patient_id, mode="search", queries=queries,
        # 근거 등급 A~D는 응답을 만드는 이 계층에서 붙인다 (evidence_grade.py 설명 참고).
        items=with_grades(items),
    )


# 정식 Stage 1 조립 (app/ai/agent/README.md의 Production 조립 예시 그대로).
# clock은 호출할 때마다 tz-aware 현재 시각을 돌려준다. 조회 함수는 이 시각만 기준으로 쓴다.
_feed_context_loader = DefaultFeedContextLoader(
    source=ProductionFeedContextSource(read_snapshot=get_feed_context_snapshot),
    clock=lambda: datetime.now(APP_TIMEZONE),
    service_timezone=APP_TIMEZONE,
)


def context_prepare(context: AgentContext) -> str:
    """FeedWorkflow 1단계 (정식 경로). 간병인의 관리 환자 전체를 한 스냅샷으로 읽어
    LLM 입력(JSON 문자열)으로 바꾼다. context.patient_id로 환자를 거르지 않는다."""
    return prepare_production_feed_input(context, _feed_context_loader)


def backend_handoff(result: StructuredAgentResult) -> list[RecommendationItem]:
    """FeedWorkflow 3단계. AI 결과에 DB의 논문 정보를 붙인다.

    personal_reason / category는 AI 결과에서,
    논문 제목·요약 3칸을 비롯한 나머지(id, title, journal, …)는 DB에서 채운다.
    pmid가 없거나(가이드라인 등) DB에 없는 항목은 뺀다.
    """
    pairs = [(item, source) for item, source in zip(result.output.items, result.sources) if source.pmid]
    details = functions.get_paper_details_by_pmids([source.pmid for _, source in pairs])

    items = []
    for item, source in pairs:
        paper = details.get(source.pmid)
        if paper is None:
            continue
        items.append(RecommendationItem(
            **paper.model_dump(),
            personal_reason=item.personal_reason,
            category=item.category,
        ))
    # 근거 등급 A~D는 응답을 만드는 이 계층에서 붙인다 (evidence_grade.py 설명 참고).
    return with_grades(items)


@router.get(
    "/{user_id}",
    response_model=RecommendationResponse,
    summary="간병인 맞춤 논문 추천",
    responses={404: _example(_USER_NOT_FOUND)},
)
def get_recommendations(
    user_id: UUID,
    patient_id: Optional[UUID] = Query(
        None, description="선택. 주면 검색 방식 응답만 그 환자 기준으로 좁힌다 (AI 방식은 항상 환자 전체)"
    ),
    k: int = Query(5, ge=1, le=5, description="추천 논문 수 (최대 5)"),
    refresh: bool = Query(False, description="true면 저장 기간과 상관없이 AI 피드를 새로 만든다"),
    feed_execution: Optional[FeedExecution] = Depends(get_feed_execution),
) -> RecommendationResponse:
    """간병인의 관리 환자 전체를 기준으로 논문을 최대 `k`편 돌려준다.

    - `mode="ai"`: AI가 고른 논문 + `personal_reason` / `category`. 제목·요약은 DB에서 온다.
      `patient_id`와 관계없이 관리 환자 전체가 기준이다.
    - `mode="search"`: 검색어(`queries`)와 가까운 논문 + `relevance_score`. AI 방식을 아직 못 쓰거나
      실패했거나 결과가 0건일 때의 대신 응답이다. 환자마다 검색어를 하나씩 만들고,
      `patient_id`를 주면 그 환자 검색어 하나만 쓴다. 환자가 0명이면 일반 검색어 하나.
    - AI 결과는 저장되고, 저장 기간(기본 3일) 안에는 저장본을 그대로 준다 (`generated_at` = 처음 만든 시각).
      `refresh=true`면 새로 만든다. 검색 방식 결과는 저장하지 않는다 (`generated_at`=null).
    - `patient_id`는 선택이다. 이 사용자의 환자가 아니면(없는 환자 포함) 404.
    - 없는 사용자는 404.
    - 서버를 띄운 뒤 첫 요청은 임베딩 모델(bge-m3)을 불러오느라 오래 걸린다.
    """
    profile = user_functions.get_user_profile_context(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=_USER_NOT_FOUND)

    patients = {row.patient.id: row.patient for row in profile.patients}
    if patient_id is not None and patient_id not in patients:
        raise HTTPException(status_code=404, detail=_PATIENT_NOT_FOUND)

    if feed_execution is not None:
        if not refresh and (cached := load_cached_feed(user_id)):
            generated_at, items = cached
            return RecommendationResponse(
                user_id=user_id, patient_id=patient_id, mode="ai", items=items[:k], generated_at=generated_at
            )
        try:
            # 정식 경로는 간병인 기준이라 patient_id를 넘기지 않는다.
            context = AgentContext(request_id=str(uuid4()), user_id=user_id, patient_id=None)
            items = FeedWorkflow(
                context_prepare=context_prepare,
                feed_execution=feed_execution,
                backend_handoff=lambda _ctx, result: backend_handoff(result),
            ).run(context)
            if items:
                # k와 상관없이 전부 저장한다 (다음에 더 큰 k로 불러도 저장본에서 줄 수 있게).
                return RecommendationResponse(
                    user_id=user_id, patient_id=patient_id, mode="ai", items=items[:k],
                    generated_at=save_feed(user_id, items),
                )
            logger.warning("추천 AI 결과 0건 → 검색 방식으로 응답")
        except FeedWorkflowError as exc:
            # FeedWorkflow가 원래 예외를 감싸므로 종류는 __context__에서 읽는다 (메시지는 남기지 않는다).
            logger.warning("추천 AI 실패 → 검색 방식으로 응답 (stage=%s, %s)",
                           exc.stage, type(exc.__context__).__name__)
        except Exception as exc:
            logger.warning("추천 AI 실패 → 검색 방식으로 응답 (stage=없음, %s)", type(exc).__name__)

    selected = [patients[patient_id]] if patient_id is not None else list(patients.values())
    return search_recommendations(user_id, patient_id, selected, k)
