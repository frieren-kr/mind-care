"""논문 추천 API (박주현 담당).

GET /recommendations/{user_id}?patient_id=&k=5   환자 프로필에 맞는 논문 최대 5편

흐름:
  ① get_user_profile_context()로 간병인·환자 정보 읽기      [한슬님 함수]
  ② AI 방식 (mode="ai") — 웅 님 FeedWorkflow 3단계
       context_prepare → feed_execution → backend_handoff
  ③ AI 방식을 못 쓰면 검색 방식 (mode="search")으로 대신 응답
       build_query() → search() → get_paper_details_by_pmids()   [현서님 함수]

로그인이 아직 없어서 user_id를 경로로 받는다 (api/users.py와 같은 방식).
"""
import logging
from datetime import datetime
from typing import Callable, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query

from app.ai.agent.outputs import StructuredAgentResult
from app.ai.agent.schemas import AgentContext
from app.ai.agent.workflows.feed import FeedWorkflow, FeedWorkflowError
from app.ai.agent.workflows.feed_context_prepare import prepare_demo_feed_input
from app.ai.retrieval.search import search
from app.api.evidence_grade import with_grades
from app.db import functions, user_functions
from app.schemas import RecommendationItem, RecommendationResponse
from app.user_schemas import APP_TIMEZONE, PatientOut, UserProfileContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

_USER_NOT_FOUND = "사용자를 찾을 수 없습니다."
_PATIENT_NOT_FOUND = "환자를 찾을 수 없습니다."
_PATIENT_REQUIRED = "patient_id를 지정해주세요"

FeedExecution = Callable[[AgentContext, str], StructuredAgentResult]


def _example(detail: str) -> dict:
    """OpenAPI 문서(/docs)에 보여줄 오류 응답 예시."""
    return {"description": detail, "content": {"application/json": {"example": {"detail": detail}}}}


def get_feed_execution() -> Optional[FeedExecution]:
    """FeedWorkflow의 2단계(feed_execution)를 돌려주는 FastAPI 의존성.

    ★ 웅 님 search_evidence 핸들러·레지스트리·LLM 클라이언트 함수로 교체 예정 ★
    지금은 None을 돌려주고, None이면 엔드포인트가 검색 방식으로 대신 응답한다.
    테스트에서는 app.dependency_overrides[get_feed_execution]으로 바꿔 끼운다.
    """
    return None


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


def search_recommendations(user_id: UUID, patient: PatientOut, k: int) -> RecommendationResponse:
    """검색 방식 (mode="search"). AI 방식을 못 쓸 때의 대신 응답이기도 하다."""
    query = build_query(patient)
    evidence = search(query, k).evidence
    details = functions.get_paper_details_by_pmids([e.pmid for e in evidence])

    items = []
    for e in evidence:
        paper = details.get(e.pmid)
        if paper is None:  # 검색과 조회 사이에 삭제된 경우
            continue
        # 이름이 같은 필드만 옮겨진다 (초록 등 나머지는 버려진다).
        items.append(RecommendationItem(**paper.model_dump(), relevance_score=e.relevance_score))
    return RecommendationResponse(
        user_id=user_id, patient_id=patient.id, mode="search", query=query,
        # 근거 등급 A~D는 응답을 만드는 이 계층에서 붙인다 (evidence_grade.py 설명 참고).
        items=with_grades(items),
    )


def context_prepare(context: AgentContext, profile: UserProfileContext) -> str:
    """FeedWorkflow 1단계. 이미 읽어 둔 프로필을 LLM 입력(JSON 문자열)으로 바꾼다."""
    return prepare_demo_feed_input(
        context, profile, reference_time=datetime.now(APP_TIMEZONE), service_timezone=APP_TIMEZONE
    )


def backend_handoff(result: StructuredAgentResult) -> list[RecommendationItem]:
    """FeedWorkflow 3단계. AI 결과에 DB의 논문 정보를 붙인다.

    headline / summary_bullets / personal_reason / category는 AI 결과에서,
    나머지(id, title, journal, … 요약 3칸)는 DB에서 채운다.
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
            headline=item.headline,
            summary_bullets=item.summary_bullets,
            personal_reason=item.personal_reason,
            category=item.category,
        ))
    # 근거 등급 A~D는 응답을 만드는 이 계층에서 붙인다 (evidence_grade.py 설명 참고).
    return with_grades(items)


@router.get(
    "/{user_id}",
    response_model=RecommendationResponse,
    summary="환자 맞춤 논문 추천",
    responses={
        400: _example(_PATIENT_REQUIRED),
        404: _example(_USER_NOT_FOUND),
    },
)
def get_recommendations(
    user_id: UUID,
    patient_id: Optional[UUID] = Query(None, description="추천 기준 환자. 환자가 1명이면 생략 가능"),
    k: int = Query(5, ge=1, le=5, description="추천 논문 수 (최대 5)"),
    feed_execution: Optional[FeedExecution] = Depends(get_feed_execution),
) -> RecommendationResponse:
    """환자 프로필에 맞는 논문을 최대 `k`편 돌려준다.

    - `mode="ai"`: AI가 고른 논문 + `headline` / `summary_bullets` / `personal_reason` / `category`.
    - `mode="search"`: 검색어(`query`)와 가까운 논문 + `relevance_score`. AI 방식을 아직 못 쓰거나
      실패했거나 결과가 0건일 때의 대신 응답이다.
    - 환자가 2명 이상인데 `patient_id`를 생략하면 400.
    - 없는 사용자, 이 사용자의 환자가 아닌 `patient_id`(없는 환자 포함)는 404.
    - 서버를 띄운 뒤 첫 요청은 임베딩 모델(bge-m3)을 불러오느라 오래 걸린다.
    """
    profile = user_functions.get_user_profile_context(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=_USER_NOT_FOUND)

    patients = {row.patient.id: row.patient for row in profile.patients}
    if patient_id is None:
        if len(patients) > 1:
            raise HTTPException(status_code=400, detail=_PATIENT_REQUIRED)
        if not patients:
            raise HTTPException(status_code=404, detail=_PATIENT_NOT_FOUND)
        patient_id = next(iter(patients))
    elif patient_id not in patients:
        raise HTTPException(status_code=404, detail=_PATIENT_NOT_FOUND)

    if feed_execution is not None:
        try:
            context = AgentContext(request_id=str(uuid4()), user_id=user_id, patient_id=patient_id)
            items = FeedWorkflow(
                context_prepare=lambda ctx: context_prepare(ctx, profile),
                feed_execution=feed_execution,
                backend_handoff=lambda _ctx, result: backend_handoff(result),
            ).run(context)[:k]
            if items:
                return RecommendationResponse(
                    user_id=user_id, patient_id=patient_id, mode="ai", items=items
                )
            logger.warning("추천 AI 결과 0건 → 검색 방식으로 응답")
        except FeedWorkflowError as exc:
            # FeedWorkflow가 원래 예외를 감싸므로 종류는 __context__에서 읽는다 (메시지는 남기지 않는다).
            logger.warning("추천 AI 실패 → 검색 방식으로 응답 (stage=%s, %s)",
                           exc.stage, type(exc.__context__).__name__)
        except Exception as exc:
            logger.warning("추천 AI 실패 → 검색 방식으로 응답 (stage=없음, %s)", type(exc).__name__)

    return search_recommendations(user_id, patients[patient_id], k)
