"""논문 추천 API (박주현 담당) — 첫 버전.

GET /recommendations/{user_id}?patient_id=&k=5   환자 프로필에 맞는 논문 Top-k

흐름:
  ① user_functions로 간병인·환자 정보 읽기          [한슬님 함수]
  ② build_query()로 프로필 → 검색어 한 줄            (임시)
  ③ search(검색어, k)로 관련 논문 Top-k              [현서님 함수]
  ④ get_paper_details_by_pmids()로 paper_id + 저장된 요약 붙이기

로그인이 아직 없어서 user_id를 경로로 받는다 (api/users.py와 같은 방식).
"""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from app.ai.retrieval.search import search
from app.db import functions, user_functions
from app.schemas import RecommendationItem, RecommendationResponse
from app.user_schemas import PatientOut

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

_USER_NOT_FOUND = "사용자를 찾을 수 없습니다."
_PATIENT_NOT_FOUND = "환자를 찾을 수 없습니다."
_PATIENT_REQUIRED = "patient_id를 지정해주세요"


def _example(detail: str) -> dict:
    """OpenAPI 문서(/docs)에 보여줄 오류 응답 예시."""
    return {"description": detail, "content": {"application/json": {"example": {"detail": detail}}}}


def build_query(patient: PatientOut) -> str:
    """환자 단계·증상·관심사로 한국어 검색어 한 줄을 만든다.

    ★ 임시 ★ 웅 님 피드 흐름으로 교체 예정. 지금은 값을 문장에 끼워 넣기만 한다
    (간병인 자가점검, 평가 점수, 안전 기록 등은 쓰지 않는다). 교체할 때는 이 함수만 바꾸면 된다.
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
    k: int = Query(5, ge=1, le=20, description="추천 논문 수 (최대 20)"),
) -> RecommendationResponse:
    """환자 프로필(단계·증상·관심사)로 검색어를 만들어 관련 논문 Top-k를 돌려준다.

    - 환자가 2명 이상인데 `patient_id`를 생략하면 400.
    - 없는 사용자, 이 사용자의 환자가 아닌 `patient_id`(없는 환자 포함)는 404.
    - 서버를 띄운 뒤 첫 요청은 임베딩 모델(bge-m3)을 불러오느라 오래 걸린다.
    """
    caregiver = user_functions.get_caregiver_context(user_id)                    # ①
    if caregiver is None:
        raise HTTPException(status_code=404, detail=_USER_NOT_FOUND)

    if patient_id is None:
        if len(caregiver.patient_ids) > 1:
            raise HTTPException(status_code=400, detail=_PATIENT_REQUIRED)
        if not caregiver.patient_ids:
            raise HTTPException(status_code=404, detail=_PATIENT_NOT_FOUND)
        patient_id = caregiver.patient_ids[0]
    elif patient_id not in caregiver.patient_ids:
        raise HTTPException(status_code=404, detail=_PATIENT_NOT_FOUND)

    patient = user_functions.get_patient(patient_id)
    if patient is None:  # 확인과 조회 사이에 삭제된 경우
        raise HTTPException(status_code=404, detail=_PATIENT_NOT_FOUND)

    query = build_query(patient)                                                 # ②
    evidence = search(query, k).evidence                                         # ③
    details = functions.get_paper_details_by_pmids([e.pmid for e in evidence])   # ④

    items = []
    for e in evidence:
        paper = details.get(e.pmid)
        if paper is None:  # 검색과 조회 사이에 삭제된 경우
            continue
        # 이름이 같은 필드만 옮겨진다 (초록 등 나머지는 버려진다).
        # personal_reason은 웅 님 피드 흐름 연결 후 채운다 (지금은 기본값 null).
        items.append(RecommendationItem(**paper.model_dump(), relevance_score=e.relevance_score))
    return RecommendationResponse(user_id=user_id, patient_id=patient_id, query=query, items=items)
