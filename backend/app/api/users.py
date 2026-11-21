"""피드 입력용 사용자·환자 API (김한슬 담당. 돌보는 분 추가·수정은 명석희 — 11/18 한슬 님 동의, 돌봄 기록·검사·약·진료 입력도 명석희).

AI 피드 함수가 받는 개인화 입력(사용자·환자 프로필)을 돌려준다.
GET /feed_user/{user_id}                     간병인 정보만 (이름 + 자가점검 + 담당 환자 id 목록)
GET /feed_patient/{user_id}/{patient_id}     환자 한 명의 현재 상태 (이 간병인의 환자만)
GET /feed/{user_id}                          간병인 + 담당 환자 전원 (AI 피드 함수 입력 형식 그대로)
POST  /users/{user_id}/patients              돌보는 분 추가 (담당 간병인 = 로그인한 본인)
PATCH /users/{user_id}/patients/{patient_id} 돌보는 분 정보 수정 (보낸 칸만, 이 간병인의 환자만)
GET   /users/{user_id}/patients/{patient_id}/care-logs  최근 돌봄 기록 (챗봇 Tool과 같은 조회)
POST  /users/{user_id}/patients/{patient_id}/care-logs  돌봄 기록 추가 (patient_care)
POST  /users/{user_id}/patients/{patient_id}/assessments  검사 결과 추가 (앱 자가점검 KDSQ-C 저장)
POST  /users/{user_id}/patients/{patient_id}/medications  복용 약 추가
PATCH /users/{user_id}/patients/{patient_id}/medications/{medication_id}  약 정보 수정·복용 중단
POST  /users/{user_id}/patients/{patient_id}/visits       진료 예약 추가
PATCH /users/{user_id}/patients/{patient_id}/visits/{visit_id}  진료 수정·다녀옴 표시
GET   /users/{user_id}/patients/{patient_id}/visits/pending      지난 날짜인데 다녀옴·건너뜀 표시가 없는 진료
POST  /users/{user_id}/patients/{patient_id}/visits/{visit_id}/skip  가지 않은 진료로 표시 (건너뜀)

경로의 user_id와 로그인 사용자가 같은지 공통 의존성으로 먼저 확인한다.
사용자 목록(user_functions.list_users)은 전체 회원 정보가 나오므로 API로 열지 않는다.
응답에는 email / phone_number가 들어 있지 않다.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.dependencies import require_user_access
from app.db import feed_read, user_functions
from app.user_schemas import (
    CARE_LOG_MAX_DAYS, AssessmentCreate, AssessmentOut, CareLogCreate, CareLogList, CareLogOut, CaregiverContext,
    FeedSettingsOut, FeedSettingsUpdate, MedicalVisitCreate, MedicalVisitOut, MedicalVisitUpdate, MedicationCreate,
    MedicationOut, MedicationUpdate, PatientContext, PatientCreate, PatientIn, PatientOut, PatientUpdate,
    UserProfileContext, today,
)

# 돌봄 기록 종류. 챗봇 돌봄 기록 Tool은 patient_care만 읽는다 (간병인 자기돌봄은 caregiver_selfcare).
CARE_LOG_TYPE = "patient_care"

router = APIRouter(tags=["feed"], dependencies=[Depends(require_user_access)])

_USER_NOT_FOUND = "사용자를 찾을 수 없습니다."
_PATIENT_NOT_FOUND = "환자를 찾을 수 없습니다."
_MEDICATION_NOT_FOUND = "약 정보를 찾을 수 없습니다."
_VISIT_NOT_FOUND = "진료 기록을 찾을 수 없습니다."

# 가지 않은 진료 표시. 상태 칸이 없어서(DB 변경 없이) 기타 메모(visit_content) 맨 앞에 붙인다.
# 챗봇 진료 Tool이 visit_content를 그대로 읽으므로 "건너뛴 진료"로 설명할 수 있다. is_visited는 false 그대로.
VISIT_SKIPPED_PREFIX = "건너뜀 — 가지 않은 진료"
PENDING_VISITS_LIMIT = 10


def _is_skipped(visit_content: Optional[str]) -> bool:
    return bool(visit_content) and visit_content.startswith(VISIT_SKIPPED_PREFIX)


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


@router.get(
    "/users/{user_id}/feed-settings",
    response_model=FeedSettingsOut,
    summary="피드 갱신 주기 조회",
    responses=_not_found(_USER_NOT_FOUND),
)
def get_feed_settings(user_id: UUID) -> FeedSettingsOut:
    """사용자별 피드 갱신 주기(1~7일, 기본 3일)를 조회한다."""
    settings = user_functions.get_user_feed_settings(user_id)
    if settings is None:
        raise HTTPException(status_code=404, detail=_USER_NOT_FOUND)
    return settings


@router.patch(
    "/users/{user_id}/feed-settings",
    response_model=FeedSettingsOut,
    summary="피드 갱신 주기 변경",
    responses=_not_found(_USER_NOT_FOUND),
)
def update_feed_settings(user_id: UUID, data: FeedSettingsUpdate) -> FeedSettingsOut:
    """주기를 저장하고 반환한다. 다음 AI 피드 요청부터 기존 생성 시각에 새 기간을 적용한다.

    즉시 피드를 생성하거나 주기 작업을 예약하지 않는다. 검색 피드는 저장하지 않는다.
    """
    settings = user_functions.update_user_feed_settings(user_id, data)
    if settings is None:
        raise HTTPException(status_code=404, detail=_USER_NOT_FOUND)
    return settings


@router.post(
    "/users/{user_id}/patients",
    response_model=PatientOut,
    status_code=201,
    summary="돌보는 분 추가",
)
def create_patient(user_id: UUID, data: PatientCreate) -> PatientOut:
    """로그인한 간병인에게 환자를 등록하고 저장된 행을 돌려준다 (201).

    담당 간병인은 본문이 아니라 주소의 user_id로 정한다 (본문에 caregiver_id를 넣으면 422).
    새 환자는 created_at이 피드 저장본보다 늦어서, 다음 피드 요청 때 맞춤 추천을 새로 만든다.
    """
    try:
        return user_functions.create_patient(PatientIn(caregiver_id=user_id, **data.model_dump()))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@router.patch(
    "/users/{user_id}/patients/{patient_id}",
    response_model=PatientOut,
    summary="돌보는 분 정보 수정",
    responses=_not_found(_PATIENT_NOT_FOUND),
)
def update_patient(user_id: UUID, patient_id: UUID, data: PatientUpdate) -> PatientOut:
    """보낸 칸만 바꾼다 (규칙은 PatientUpdate). 수정되면 다음 피드 요청 때 맞춤 추천을 새로 만든다.

    이 간병인의 환자가 아니면 (다른 간병인의 환자, 없는 환자 모두) 404 — 환자 조회 API와 같다.
    """
    if not user_functions.is_caregiver_of(user_id, patient_id):
        raise HTTPException(status_code=404, detail=_PATIENT_NOT_FOUND)
    try:
        patient = user_functions.update_patient(patient_id, data)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    if patient is None:  # 확인과 수정 사이에 삭제된 경우
        raise HTTPException(status_code=404, detail=_PATIENT_NOT_FOUND)
    return patient


@router.get(
    "/users/{user_id}/patients/{patient_id}/care-logs",
    response_model=CareLogList,
    summary="최근 돌봄 기록",
    responses=_not_found(_PATIENT_NOT_FOUND),
)
def list_care_logs(
    user_id: UUID,
    patient_id: UUID,
    days: int = Query(14, ge=1, le=CARE_LOG_MAX_DAYS, description="최근 며칠 (1~90, 기본 14 — 챗봇과 같음)"),
    limit: int = Query(30, ge=1, le=30, description="최대 건수 (1~30)"),
) -> CareLogList:
    """이 환자의 patient_care 기록을 최신순으로. 챗봇 돌봄 기록 Tool과 같은 함수(get_recent_care_logs)로 읽는다.

    이 간병인의 환자가 아니면 404. total_count는 limit으로 자르기 전 건수다.
    """
    now = datetime.now(timezone.utc)
    try:
        logs = user_functions.get_recent_care_logs(
            user_id=user_id, patient_id=patient_id, start_at=now - timedelta(days=days),
            # 방금 저장한 기록(폰 시계가 조금 빠른 경우 포함)도 보이도록 끝을 조금 넉넉히
            end_at=now + timedelta(minutes=10), limit=limit,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail=_PATIENT_NOT_FOUND) from None
    items = [CareLogOut(patient_id=log.patient_id, content=log.content, mood_tag=log.mood_tag,
                        logged_at=log.logged_at) for log in logs.items]
    return CareLogList(items=items, total_count=logs.total_count)


@router.post(
    "/users/{user_id}/patients/{patient_id}/care-logs",
    response_model=CareLogOut,
    status_code=201,
    summary="돌봄 기록 추가",
    responses=_not_found(_PATIENT_NOT_FOUND),
)
def create_care_log(user_id: UUID, patient_id: UUID, data: CareLogCreate) -> CareLogOut:
    """환자 돌봄 기록(patient_care) 1건을 저장한다 (201). 저장 함수는 feed_read.save_care_log().

    챗봇 돌봄 기록 Tool과 피드 개인화가 이 기록을 그대로 읽는다. 이 간병인의 환자가 아니면 404.
    """
    if not user_functions.is_caregiver_of(user_id, patient_id):
        raise HTTPException(status_code=404, detail=_PATIENT_NOT_FOUND)
    # 시각을 비우면 서버 시각으로 정해서 응답에도 같은 값을 돌려준다
    logged_at = data.logged_at or datetime.now(timezone.utc)
    try:
        feed_read.save_care_log(
            user_id=user_id, log_type=CARE_LOG_TYPE, content=data.content, mood_tag=data.mood_tag,
            patient_id=patient_id, logged_at=logged_at,
        )
    except ValueError:
        # 확인과 저장 사이에 환자가 지워지거나 담당이 바뀐 경우
        raise HTTPException(status_code=404, detail=_PATIENT_NOT_FOUND) from None
    return CareLogOut(patient_id=patient_id, content=data.content, mood_tag=data.mood_tag, logged_at=logged_at)


# ---------- 검사 결과·약·진료 입력 (앱) ----------
# 저장·검증은 한슬 님 user_functions 함수 그대로. 주소의 환자가 이 간병인의 환자인지 먼저 확인하고(아니면 404),
# 약·진료 수정은 그 기록이 주소의 환자 것인지도 확인한다. 입력 오류(ValueError)는 422.


def _require_patient(user_id: UUID, patient_id: UUID) -> None:
    if not user_functions.is_caregiver_of(user_id, patient_id):
        raise HTTPException(status_code=404, detail=_PATIENT_NOT_FOUND)


def _save(func, *args):
    """user_functions 저장 함수 호출. 입력 오류는 422, 저장 사이에 기록이 사라졌으면(None) 호출한 쪽이 404."""
    try:
        return func(*args)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@router.post(
    "/users/{user_id}/patients/{patient_id}/assessments",
    response_model=AssessmentOut,
    status_code=201,
    summary="검사 결과 추가",
    responses=_not_found(_PATIENT_NOT_FOUND),
)
def create_assessment(user_id: UUID, patient_id: UUID, data: AssessmentCreate) -> AssessmentOut:
    """검사 결과 1건 저장 (201). 앱 자가점검은 {"assessment_type": "KDSQ-C", "score": 7, "assessed_at": "…"}.

    검사명·점수 범위 검사는 create_assessment()의 ASSESSMENT_TYPES 규칙 그대로. 챗봇 검사 기록 Tool과 /feed가 읽는다.
    """
    _require_patient(user_id, patient_id)
    return _save(user_functions.create_assessment, {**data.model_dump(exclude_unset=True), "patient_id": patient_id})


@router.post(
    "/users/{user_id}/patients/{patient_id}/medications",
    response_model=MedicationOut,
    status_code=201,
    summary="복용 약 추가",
    responses=_not_found(_PATIENT_NOT_FOUND),
)
def create_medication(user_id: UUID, patient_id: UUID, data: MedicationCreate) -> MedicationOut:
    """복용 약 1종 저장 (201). drug_name 필수."""
    _require_patient(user_id, patient_id)
    return _save(user_functions.create_medication, {**data.model_dump(exclude_unset=True), "patient_id": patient_id})


@router.patch(
    "/users/{user_id}/patients/{patient_id}/medications/{medication_id}",
    response_model=MedicationOut,
    summary="약 정보 수정·복용 중단",
    responses=_not_found(_MEDICATION_NOT_FOUND),
)
def update_medication(user_id: UUID, patient_id: UUID, medication_id: UUID, data: MedicationUpdate) -> MedicationOut:
    """보낸 칸만 바꾼다. 복용 중단은 {"is_taking": false, "end_date": "오늘"} (기록은 지우지 않는다)."""
    _require_patient(user_id, patient_id)
    current = user_functions.get_medication(medication_id)
    if current is None or current.patient_id != patient_id:
        raise HTTPException(status_code=404, detail=_MEDICATION_NOT_FOUND)
    medication = _save(user_functions.update_medication, medication_id, data)
    if medication is None:
        raise HTTPException(status_code=404, detail=_MEDICATION_NOT_FOUND)
    return medication


@router.post(
    "/users/{user_id}/patients/{patient_id}/visits",
    response_model=MedicalVisitOut,
    status_code=201,
    summary="진료 예약 추가",
    responses=_not_found(_PATIENT_NOT_FOUND),
)
def create_visit(user_id: UUID, patient_id: UUID, data: MedicalVisitCreate) -> MedicalVisitOut:
    """진료 1건 저장 (201). visit_date 필수, is_visited 기본 false(예약). 미래 날짜를 다녀옴으로는 못 넣는다."""
    _require_patient(user_id, patient_id)
    return _save(user_functions.create_medical_visit, {**data.model_dump(exclude_unset=True), "patient_id": patient_id})


@router.patch(
    "/users/{user_id}/patients/{patient_id}/visits/{visit_id}",
    response_model=MedicalVisitOut,
    summary="진료 수정·다녀옴 표시",
    responses=_not_found(_VISIT_NOT_FOUND),
)
def update_visit(user_id: UUID, patient_id: UUID, visit_id: UUID, data: MedicalVisitUpdate) -> MedicalVisitOut:
    """보낸 칸만 바꾼다. 다녀온 뒤에는 {"is_visited": true, "doctor_note": "…", "medication_change": "…"}."""
    _require_patient(user_id, patient_id)
    current = user_functions.get_medical_visit(visit_id)
    if current is None or current.patient_id != patient_id:
        raise HTTPException(status_code=404, detail=_VISIT_NOT_FOUND)
    visit = _save(user_functions.update_medical_visit, visit_id, data)
    if visit is None:
        raise HTTPException(status_code=404, detail=_VISIT_NOT_FOUND)
    return visit


@router.get(
    "/users/{user_id}/patients/{patient_id}/visits/pending",
    response_model=list[MedicalVisitOut],
    summary="기록이 필요한 지난 진료",
    responses=_not_found(_PATIENT_NOT_FOUND),
)
def list_pending_visits(user_id: UUID, patient_id: UUID) -> list[MedicalVisitOut]:
    """오늘(한국 시간) 이전 날짜인데 다녀옴(is_visited)도 건너뜀 표시도 없는 진료. 최근 날짜부터 최대 10건.

    /feed의 upcoming_visits(오늘 이후)·recent_visits(다녀옴)에 들어가지 않아 앱에서 고칠 수 없던 기록이다.
    """
    _require_patient(user_id, patient_id)
    yesterday = today() - timedelta(days=1)
    visits = user_functions.list_medical_visits(patient_id, is_visited=False, end_date=yesterday, limit=50)
    return [v for v in visits if not _is_skipped(v.visit_content)][:PENDING_VISITS_LIMIT]


@router.post(
    "/users/{user_id}/patients/{patient_id}/visits/{visit_id}/skip",
    response_model=MedicalVisitOut,
    summary="가지 않은 진료로 표시",
    responses=_not_found(_VISIT_NOT_FOUND),
)
def skip_visit(user_id: UUID, patient_id: UUID, visit_id: UUID) -> MedicalVisitOut:
    """기타 메모 맨 앞에 '건너뜀' 표시를 붙인다 (기존 메모는 뒤에 남긴다). 이미 표시돼 있으면 그대로 돌려준다.

    다녀온 것으로 등록된 진료는 422 (다녀옴을 되돌리려면 PATCH로 is_visited=false).
    """
    _require_patient(user_id, patient_id)
    current = user_functions.get_medical_visit(visit_id)
    if current is None or current.patient_id != patient_id:
        raise HTTPException(status_code=404, detail=_VISIT_NOT_FOUND)
    if current.is_visited:
        raise HTTPException(status_code=422, detail="다녀온 것으로 등록된 진료는 건너뜀으로 바꿀 수 없습니다.")
    if _is_skipped(current.visit_content):
        return current
    note = f"{VISIT_SKIPPED_PREFIX}\n{current.visit_content}" if current.visit_content else VISIT_SKIPPED_PREFIX
    visit = _save(user_functions.update_medical_visit, visit_id, {"visit_content": note})
    if visit is None:
        raise HTTPException(status_code=404, detail=_VISIT_NOT_FOUND)
    return visit
