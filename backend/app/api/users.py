"""피드 입력용 사용자·환자 API (김한슬 담당).

AI 피드 함수가 받는 개인화 입력(사용자·환자 프로필)을 돌려준다.
GET /feed_user/{user_id}                     간병인 정보만 (이름 + 자가점검 + 담당 환자 id 목록)
GET /feed_patient/{user_id}/{patient_id}     환자 한 명의 현재 상태 (이 간병인의 환자만)
GET /feed/{user_id}                          간병인 + 담당 환자 전원 (AI 피드 함수 입력 형식 그대로)

로그인이 아직 없어서 user_id를 경로로 받는다. 인증을 붙이면 토큰에서 꺼낸 user_id와
경로의 user_id가 같은지(본인인지) 확인하도록 바꾼다.
사용자 목록(user_functions.list_users)은 전체 회원 정보가 나오므로 API로 열지 않는다.
응답에는 email / phone_number가 들어 있지 않다.
"""
from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.db import user_functions
from app.user_schemas import CaregiverContext, PatientContext, UserProfileContext

router = APIRouter(tags=["feed"])

_USER_NOT_FOUND = "사용자를 찾을 수 없습니다."
_PATIENT_NOT_FOUND = "환자를 찾을 수 없습니다."


def _not_found(detail: str) -> dict:
    """OpenAPI 문서(/docs)에 보여줄 404 응답 예시."""
    return {404: {"description": detail, "content": {"application/json": {"example": {"detail": detail}}}}}


@router.get(
    "/feed_user/{user_id}",
    response_model=CaregiverContext,
    summary="간병인 정보",
    responses=_not_found(_USER_NOT_FOUND),
)
def get_caregiver(user_id: UUID) -> CaregiverContext:
    """이름 + 간병인 자가점검 + 담당 환자 id 목록. 환자 상세는 아래 환자 API로 받는다."""
    caregiver = user_functions.get_caregiver_context(user_id)
    if caregiver is None:
        raise HTTPException(status_code=404, detail=_USER_NOT_FOUND)
    return caregiver


@router.get(
    "/feed_patient/{user_id}/{patient_id}",
    response_model=PatientContext,
    summary="환자 정보",
    responses=_not_found(_PATIENT_NOT_FOUND),
)
def get_patient(user_id: UUID, patient_id: UUID) -> PatientContext:
    """환자 기본 정보 + 검사별 최신 평가 · 복용 중인 약 · 최근 안전 기록 · 진료/예약.

    이 간병인의 환자가 아니면 (다른 간병인의 환자, 없는 환자 모두) 404 — 환자가 있는지 여부도 알려주지 않는다.
    """
    if not user_functions.is_caregiver_of(user_id, patient_id):
        raise HTTPException(status_code=404, detail=_PATIENT_NOT_FOUND)
    patient = user_functions.get_patient_context(patient_id)
    if patient is None:  # 확인과 조회 사이에 삭제된 경우
        raise HTTPException(status_code=404, detail=_PATIENT_NOT_FOUND)
    return patient


@router.get(
    "/feed/{user_id}",
    response_model=UserProfileContext,
    summary="간병인 + 담당 환자 전원",
    responses=_not_found(_USER_NOT_FOUND),
)
def get_profile(user_id: UUID) -> UserProfileContext:
    """간병인 정보 + 담당 환자 전원의 현재 상태. AI 피드 함수가 받는 입력과 같은 형식이다."""
    profile = user_functions.get_user_profile_context(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=_USER_NOT_FOUND)
    return profile
