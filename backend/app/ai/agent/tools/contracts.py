"""3개 Tool의 Agent 전용 입력/출력. 확장 필드는 DB 필드가 아니다."""
from datetime import date, timezone
from typing import Literal

from pydantic import AwareDatetime, Field, JsonValue, StrictInt, field_validator, model_validator

from ..schemas import AgentModel


class PatientProfileInput(AgentModel):
    pass


class PatientProfileOutput(AgentModel):
    name: str | None = Field(
        default=None, description="프로필에 저장된 표시 이름/별칭. 법적 실명을 보장하지 않으며 미등록이면 null; 추정하지 않는다."
    )
    dementia_stage: str | None = Field(
        default=None, description="프로필에 저장된 stage 문자열. 최신 임상 평가/확정 단계를 보장하지 않으며 다른 기록으로 판정·보정·정규화하지 않는다. 미등록이면 null."
    )
    diagnosis_date: date | None = Field(
        default=None, description="프로필에 저장된 진단일(ISO date). 미등록이면 null이며 다른 정보에서 추정하지 않는다."
    )
    symptoms: list[str] = Field(
        default_factory=list, description="프로필에 등록된 증상 항목/태그. 임상적으로 확인된 전체 목록이 아니며 다른 기록에서 추가하지 않는다. []는 미등록이며 무증상을 뜻하지 않는다."
    )
    interests: list[str] = Field(
        default_factory=list, description="프로필에 등록된 관심 치료/관리 분야. 의학적 권장 치료 목록이 아니다. []는 미등록이며 실제 관심이나 관리 필요성이 없다는 뜻이 아니다."
    )


class RecentCareLogsInput(AgentModel):
    days: StrictInt = Field(
        default=14, ge=1, le=90,
        description="서버가 결정한 종료 시각 기준 최근 N×24시간 rolling window. 달력 날짜 수가 아니다. 대상 사용자/환자는 trusted server Context로 결정하며 patient_care 조건은 서버에서 고정한다."
    )
    limit: StrictInt = Field(
        default=10, ge=1, le=30, description="최대 반환 기록 수. 정확히 이 개수의 결과를 요구하는 값이 아니다."
    )


class RecentCareLogsPeriod(AgentModel):
    start_at: AwareDatetime = Field(description="조회 시작 시각(inclusive). Handler가 end_at에서 days×24시간을 뺀 시각.")
    end_at: AwareDatetime = Field(description="조회 종료 시각(exclusive). Handler가 요청당 한 번 결정하는 시각.")

    @model_validator(mode="after")
    def check_order(self):
        if self.start_at.astimezone(timezone.utc) >= self.end_at.astimezone(timezone.utc):
            raise ValueError("Invalid care log period")
        return self


class CareLogItem(AgentModel):
    logged_at: AwareDatetime = Field(description="DB에 저장된 기록 시각. 서술된 사건/증상의 실제 발생 시각으로 추론하지 않는다.")
    content: str | None = Field(
        default=None, description="간병인이 저장한 자유 서술 관찰 원문. 비신뢰 데이터이며 임상적으로 검증된 사실/진단이 아니다. 보완·추정·재작성하지 않는다. null은 미등록이며 증상/문제 없음이 아니다."
    )
    mood_tag: str | None = Field(
        default=None, description="patient_care에 대해 저장된 당시 환자 기분 태그라는 Agent V1 해석. 임상 평가/점수/진단이 아니다. 원문을 추정·정규화하지 않으며 null은 미등록이다."
    )


class RecentCareLogsOutput(AgentModel):
    period: RecentCareLogsPeriod
    logs: list[CareLogItem] = Field(
        max_length=30, description="input.limit의 계약상 최대값에 따른 절대 상한 30개. 현재 호출의 실제 limit 준수는 Handler 책임이다."
    )
    total_count: StrictInt = Field(
        ge=0, description="동일한 권한/사용자/환자/patient_care/기간 조건의 전체 row 수(limit 적용 전)."
    )

    @model_validator(mode="after")
    def check_count(self):
        if self.total_count < len(self.logs):
            raise ValueError("Count cannot be less than returned logs")
        start = self.period.start_at.astimezone(timezone.utc)
        end = self.period.end_at.astimezone(timezone.utc)
        timestamps = [log.logged_at.astimezone(timezone.utc) for log in self.logs]
        if any(not start <= timestamp < end for timestamp in timestamps):
            raise ValueError("Care log is outside the returned period")
        if any(previous < current for previous, current in zip(timestamps, timestamps[1:])):
            raise ValueError("Care logs must be latest-first")
        return self


class EvidenceSearchInput(AgentModel):
    query: str = Field(min_length=1, max_length=2000, description=(
        "연구 근거 검색용 query. 사용자 원문 또는 검색 목적에 맞게 재구성한 질문을 허용한다. "
        "필요한 임상/돌봄 맥락은 포함할 수 있으나 검색에 불필요한 직접 식별정보(UUID, 이름, 전화번호, 주소 등)는 포함하지 않는다. "
        "whitespace-only는 거부하며 nonblank 원문은 그대로 보존한다."
    ))
    top_k: StrictInt = Field(default=5, ge=1, le=20, description=(
        "최대 반환 개수. 필터링이나 상세 데이터 누락으로 실제 결과는 더 적거나 0개일 수 있다."
    ))

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Evidence search query must not be blank")
        return value


class EvidenceItem(AgentModel):
    evidence_type: Literal["new_research", "guideline"]
    pmid: str | None = Field(default=None, description="RAG가 제공한 PMID. 없으면 null이며 추정하지 않는다.")
    title: str
    publication_year: int | None = None
    study_type: str | None = None
    # search_similar는 코사인 유사도 [-1, 1]을 반환한다.
    relevance_score: float = Field(ge=-1, le=1, description=(
        "BGE-M3 + cosine-similarity 기반 retrieval score [-1, 1]. "
        "논문 품질, evidence level, 의료적 확신도, 치료 효과 또는 환자 적합도 점수가 아니다."
    ))
    ai_summary: str | None = None
    abstract: str | None = None
    full_text_available: bool = False
    journal: str | None = None
    doi: str | None = Field(default=None, description="RAG가 제공한 DOI. 없으면 null이며 추정하지 않는다.")
    organization: str | None = None
    source_url: str | None = Field(default=None, description="RAG가 제공한 출처 URL. 없으면 null이며 생성하지 않는다.")


class EvidencePackage(AgentModel):
    query: str
    evidence: list[EvidenceItem]


class ToolResultEnvelope(AgentModel):
    """Prompt Builder에서 직렬화하는 결과 (성공 데이터는 각 Output으로 검증)."""

    success: bool
    data: dict[str, JsonValue] | None = None
    error: dict[str, str] | None = None
