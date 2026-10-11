"""AI-side server-only Source DTOs; not public Backend/DB or LLM contracts.

Production Source owns authorization, eligibility, snapshot/count consistency
and stable record ordering. Free text remains untrusted data.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Generic, Protocol, TypeVar
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from ..schemas import AgentModel


class FeedSourceModel(AgentModel):
    model_config = ConfigDict(revalidate_instances="always")


T = TypeVar("T")


class SourceCollection(FeedSourceModel, Generic[T]):
    """Count of declared eligible E, before inclusion/selection I (I subset E).

    items represent I, including selection without LIMIT. Never infer |E| from
    an aggregate's returned length or missing history; required reads must all
    succeed independently of these numeric invariants.
    """

    items: list[T]
    total_count: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_count(self):
        if len(self.items) > self.total_count:
            raise ValueError("Source total_count is smaller than items")
        return self


class SourceCaregiverProfile(FeedSourceModel):
    user_id: UUID
    relationship: StrictStr | None
    burden_score: StrictInt | None
    mood_score: StrictInt | None
    lifestyle_tags: list[StrictStr]
    updated_at: AwareDatetime


class SourceClinicalAssessment(FeedSourceModel):
    patient_id: UUID
    assessment_type: StrictStr
    score: Decimal | None
    result_detail: StrictStr | None
    assessed_at: date


class SourceSafetyEvent(FeedSourceModel):
    patient_id: UUID
    event_date: date
    has_fall: StrictBool
    has_wandering: StrictBool
    has_missing: StrictBool
    note: StrictStr | None


class SourceMedication(FeedSourceModel):
    patient_id: UUID
    drug_name: StrictStr
    is_taking: StrictBool
    start_date: date | None
    end_date: date | None
    note: StrictStr | None


class SourceMedicalVisit(FeedSourceModel):
    patient_id: UUID
    visit_date: date
    is_visited: StrictBool
    department: StrictStr | None
    visit_content: StrictStr | None


class SourceManagedPatient(FeedSourceModel):
    patient_id: UUID
    caregiver_id: UUID
    dementia_stage: StrictStr | None
    diagnosis_date: date | None
    symptoms: list[StrictStr]
    interests: list[StrictStr]
    updated_at: AwareDatetime
    clinical_assessments: SourceCollection[SourceClinicalAssessment]
    safety_events: SourceCollection[SourceSafetyEvent]
    medications: SourceCollection[SourceMedication]
    medical_visits: SourceCollection[SourceMedicalVisit]


class SourceCareLog(FeedSourceModel):
    user_id: UUID
    patient_id: UUID | None
    log_type: StrictStr
    content: StrictStr | None
    mood_tag: StrictStr | None
    logged_at: AwareDatetime


class SourceLongTermSummary(FeedSourceModel):
    user_id: UUID
    period_start: date
    period_end: date
    summary: StrictStr | None
    updated_tags: list[StrictStr]
    generated_at: AwareDatetime


class FeedContextSourceSnapshot(FeedSourceModel):
    caregiver_profile: SourceCaregiverProfile | None
    managed_patients: SourceCollection[SourceManagedPatient]
    recent_care_logs: SourceCollection[SourceCareLog]
    long_term_summary: SourceLongTermSummary | None


class FeedContextSource(Protocol):
    def load_snapshot(self, *, user_id: UUID, reference_time: datetime,
                      reference_date: date,
                      recent_period_start: datetime,
                      recent_period_end: datetime) -> FeedContextSourceSnapshot:
        """Return a complete successful Feed read, not a partial/demo aggregate.

        reference_time is the Loader's UTC-normalized instant; reference_date
        is that same instant's service-timezone calendar date. reference_date
        is server-only selection metadata, not a new Snapshot/Context field.
        The supplied CareLog window is [recent_period_start, recent_period_end).
        Use them for time-based eligibility; never generate datetime.now() or
        date.today(), or recompute eligibility against an independent clock.

        Backend/Source owns user existence, trusted-scope authorization and
        explicit Feed Read eligibility. Backend/DB must complete every required
        read and pre-inclusion count in one consistent logical snapshot. That means
        reads/counts observe the same DB state, not historical as-of recovery of
        mutable profiles. Sequential helpers or merely sharing a transaction
        do not establish snapshot consistency; isolation is Backend/DB-owned.
        reference_time anchors temporal selection/validation, not DB AS-OF.
        Source/Loader/Planner must share declared collection-specific E/I
        policies; assessment/safety/medication/visit history scope is unresolved.

        Only after successful reads can None/empty/zero mean eligible data is
        absent. Missing users, denied access, unimplemented paths, query/count/
        snapshot/temporal-selection failures and partial reads must fail the
        call, never become an empty Snapshot or total_count=len(items) fallback.
        Returned invalid records must fail validation, not be silently dropped.

        Stable ordering baseline (created_at/id are server-only tie-breaks):
        assessment: assessed_at DESC, created_at DESC, id;
        safety: event_date DESC (one row per patient/date);
        medication: is_taking DESC, start_date DESC NULLS LAST, created_at DESC, id;
        visit: visit_date DESC, created_at DESC, id;
        CareLog: logged_at DESC plus a future Feed Read deterministic tie-break.
        Ordering is not eligibility; current-only medications/latest-only
        assessments/event-only safety are not the final Feed selection policy.
        Loader preserves child/log order and canonicalizes patients by UUID.bytes.

        UserProfileContext/PatientContext/get_user_profile_context() are
        current-state/demo aggregates, not FeedContextSourceSnapshot,
        SourceManagedPatient or FeedPersonalizationContextV1. They select latest
        assessments, current medication and some safety/visits, omit CareLogs/
        Summary and use their own clock. A future Production Source must use an
        internal Backend Feed Read Contract and explicit DTO field projection,
        not those aggregates or /feed HTTP APIs. CareLog/Summary reads, pre-limit
        counts, snapshot consistency and read success/failure implementation
        remain integration blockers; no Production Source is implemented here.
        """
        ...
