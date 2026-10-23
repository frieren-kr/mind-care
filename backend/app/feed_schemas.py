"""피드 개인화 Context 조회 스키마 (박주현 담당).

`app/db/feed_read.py`의 입출력 정본이다 (논문 쪽 functions.py ↔ schemas.py,
사용자 쪽 user_functions.py ↔ user_schemas.py와 같은 관계).

★ 이 DTO는 AI 쪽 Source DTO와 1:1로 맞춘 계약이다 ★
`app/ai/agent/workflows/feed_context_source.py`의 `Source*` DTO와 **필드 이름·타입이 같다.**
AI 담당(허웅)의 adapter가 필드를 그대로 옮기면 되고, 아래 "추가 칸"만 골라 쓰면 된다.
- 추가 칸 (Source DTO에는 없음): 방문 7칸(visit_reason, diagnosis, treatment_content,
  test_summary, medication_change, doctor_note, follow_up_plan) + 복약 2칸(dosage, frequency).
- 일부러 넣지 않은 칸: hospital_name, assessed_by, 환자 이름, 사용자 이름·이메일·전화번호.
  (LLM에 보낼 이유가 없는 식별 정보라서 조회 단계에서 아예 빼둔다.)

이 파일은 `app/ai/`를 import 하지 않는다 (db → ai 방향 의존은 만들지 않는다).
계약이 바뀌면 AGENTS.md의 "데이터 접근 계약"도 같이 고친다.
"""
from datetime import date
from decimal import Decimal
from typing import Generic, Literal, Optional, TypeVar
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

from app.schemas import PaperDetailResponse

# 관리 환자 수 상한. 넘으면 자르지 않고 FeedPatientLimitExceeded를 던진다
# (환자 목록은 Loader가 "완전해야 한다"고 요구해서, 자르면 조용히 틀린 결과가 된다).
MAX_MANAGED_PATIENTS = 20

# 컬렉션별 기본 상한 (I). total_count(E)는 이 상한과 무관하게 센다.
DEFAULT_CAPS = {
    "assessments": 20,
    "safety": 50,
    "medications": 30,
    "visits": 20,
    "visits_upcoming": 5,  # 방문 20건 중 '앞으로의 예약'에 먼저 주는 자리
    "care_logs": 200,
}

# 적격 기간 (reference_date 기준, 일 단위)
ASSESSMENT_WINDOW_DAYS = 365
SAFETY_WINDOW_DAYS = 180
MEDICATION_STOPPED_WITHIN_DAYS = 90
VISIT_PAST_WINDOW_DAYS = 365
VISIT_FUTURE_WINDOW_DAYS = 90


class FeedReadModel(BaseModel):
    """AI 쪽 AgentModel과 같은 설정 — 모르는 필드는 거부한다."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


T = TypeVar("T")


class FeedReadCollection(FeedReadModel, Generic[T]):
    """기록 묶음 하나.

    - `items` = 실제로 담은 기록(I). 상한으로 잘렸을 수 있다.
    - `total_count` = 적격 조건을 모두 적용한 뒤, **상한으로 자르기 전** 건수(E).

    `total_count`는 항상 별도 집계 쿼리로 센다. `len(items)`로 대체하지 않는다
    (조회가 실패했는지 데이터가 없는지를 구분할 수 없게 되기 때문).
    """

    items: list[T]
    total_count: StrictInt = Field(ge=0, description="상한으로 자르기 전 적격 건수")

    @model_validator(mode="after")
    def validate_count(self):
        if len(self.items) > self.total_count:
            raise ValueError("total_count가 items 개수보다 작습니다")
        return self

    @property
    def is_truncated(self) -> bool:
        """상한 때문에 잘렸는지."""
        return len(self.items) < self.total_count


class FeedReadCaregiverProfile(FeedReadModel):
    """간병인 자가점검 (caregiver_profiles). 사용자당 1행."""

    user_id: UUID
    relationship: Optional[StrictStr]
    burden_score: Optional[StrictInt]
    mood_score: Optional[StrictInt]
    lifestyle_tags: list[StrictStr]
    updated_at: AwareDatetime


class FeedReadClinicalAssessment(FeedReadModel):
    """치매 평가 1회 (clinical_assessments)."""

    patient_id: UUID
    assessment_type: StrictStr
    # NUMERIC(5,2) → Decimal. float로 바꾸지 않는다 (점수 비교가 흔들린다).
    score: Optional[Decimal]
    result_detail: Optional[StrictStr]
    assessed_at: date


class FeedReadSafetyEvent(FeedReadModel):
    """안전·행동 이벤트 (safety_events). 환자당 하루 1행."""

    patient_id: UUID
    event_date: date
    has_fall: StrictBool
    has_wandering: StrictBool
    has_missing: StrictBool
    note: Optional[StrictStr]


class FeedReadMedication(FeedReadModel):
    """복약 정보 (medications). 약 1종 = 1행."""

    patient_id: UUID
    drug_name: StrictStr
    is_taking: StrictBool
    start_date: Optional[date]
    end_date: Optional[date]
    note: Optional[StrictStr]
    # --- 추가 칸 (Source DTO에는 없다) ---
    dosage: Optional[StrictStr] = Field(default=None, description="1회 복용량 (예: 1정, 5mg)")
    frequency: Optional[StrictStr] = Field(default=None, description="복약 주기 (예: 1일 1회 취침 전)")


class FeedReadMedicalVisit(FeedReadModel):
    """병원 방문 1회 (medical_visits). is_visited=false + 미래 날짜면 '예약'이다."""

    patient_id: UUID
    visit_date: date
    is_visited: StrictBool
    department: Optional[StrictStr]
    visit_content: Optional[StrictStr] = Field(
        default=None,
        description="기타 자유 메모. ★005에서 '진료 내용'이 아래 항목별 칸으로 분리되면서 "
        "이 칸의 뜻이 '기타 메모'로 바뀌었다",
    )
    # --- 추가 칸 (Source DTO에는 없다). 005_medical_visits_detail.sql ---
    visit_reason: Optional[StrictStr] = Field(default=None, description="방문 목적")
    diagnosis: Optional[StrictStr] = Field(default=None, description="진단 / 의사 소견")
    treatment_content: Optional[StrictStr] = Field(default=None, description="진료 / 처치 내용")
    test_summary: Optional[StrictStr] = Field(default=None, description="검사 내용 요약")
    medication_change: Optional[StrictStr] = Field(default=None, description="약 처방 / 변경 사항")
    doctor_note: Optional[StrictStr] = Field(default=None, description="의사 전달사항")
    follow_up_plan: Optional[StrictStr] = Field(default=None, description="다음 진료 계획")


class FeedReadManagedPatient(FeedReadModel):
    """간병인이 관리하는 환자 1명 + 그 환자의 기록 묶음 4개."""

    patient_id: UUID
    caregiver_id: UUID
    dementia_stage: Optional[StrictStr]
    diagnosis_date: Optional[date]
    symptoms: list[StrictStr]
    interests: list[StrictStr]
    updated_at: AwareDatetime
    clinical_assessments: FeedReadCollection[FeedReadClinicalAssessment]
    safety_events: FeedReadCollection[FeedReadSafetyEvent]
    medications: FeedReadCollection[FeedReadMedication]
    medical_visits: FeedReadCollection[FeedReadMedicalVisit]


class FeedReadCareLog(FeedReadModel):
    """돌봄·자기돌봄 기록 1건 (care_logs).

    patient_id가 None이면 '환자 미연결 기록'(간병인 본인 자기돌봄)이다. 그대로 담는다.
    """

    user_id: UUID
    patient_id: Optional[UUID]
    log_type: StrictStr
    content: Optional[StrictStr]
    mood_tag: Optional[StrictStr]
    logged_at: AwareDatetime


class FeedReadLongTermSummary(FeedReadModel):
    """월간 요약 1건 (profile_summaries)."""

    user_id: UUID
    period_start: date
    period_end: date
    summary: Optional[StrictStr]
    updated_tags: list[StrictStr]
    generated_at: AwareDatetime


class FeedReadSnapshot(FeedReadModel):
    """get_feed_context_snapshot()이 돌려주는 한 벌.

    모든 칸이 **같은 읽기 전용 REPEATABLE READ 트랜잭션**에서 읽힌 값이다.
    None / 빈 목록 / 0은 **모든 조회가 성공한 뒤에만** 나온다 (실패는 예외로 올린다).
    """

    caregiver_profile: Optional[FeedReadCaregiverProfile]
    managed_patients: FeedReadCollection[FeedReadManagedPatient]
    recent_care_logs: FeedReadCollection[FeedReadCareLog]
    long_term_summary: Optional[FeedReadLongTermSummary]


# ============================================================
# 피드 저장본 (feed_items, 009) — save_feed_batch() / get_latest_feed_batch()
# ============================================================

FeedCategory = Literal["treatment", "care", "prevention", "diagnosis"]  # RecommendationItem.category와 같다


class FeedItemIn(BaseModel):
    """저장할 피드 한 편. 묶음 안 순서(rank)는 목록 순서대로 1부터 붙는다."""

    paper_id: UUID
    personal_reason: Optional[str] = Field(default=None, description="feed_items.personalized_explanation에 저장")
    category: Optional[FeedCategory] = None
    relevance_score: Optional[float] = None


class SavedFeedBatch(BaseModel):
    """save_feed_batch() 반환값."""

    batch_id: UUID
    generated_at: AwareDatetime = Field(description="묶음의 생성 시각 (feed_items.created_at, 묶음 안에서 모두 같다)")


class FeedBatchItem(BaseModel):
    """저장된 피드 한 편 + 논문 상세 (get_paper_detail()과 같은 형식)."""

    rank: int
    personal_reason: Optional[str]
    category: Optional[FeedCategory]
    relevance_score: Optional[float]
    is_saved: bool = Field(description="사용자가 북마크했는지")
    paper: PaperDetailResponse


class FeedBatch(BaseModel):
    """get_latest_feed_batch() 반환값. items는 rank 순."""

    batch_id: UUID
    generated_at: AwareDatetime
    items: list[FeedBatchItem]
