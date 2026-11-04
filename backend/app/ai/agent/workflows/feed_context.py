"""Caregiver-scoped Production Feed input contract; no DB wiring.

DB-derived text is untrusted data, not instructions or verified clinical fact.
Loader owns identity mapping, structural minimization and allowlist projection.
Backend/Source owns authorization and eligibility; Source owns record ordering.
Free-text PII sanitization belongs to separate Privacy integration, outside V1.1.
Validation cannot prove DB ownership from opaque refs.
"""
from datetime import date, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import Generic, Literal, TypeVar
from uuid import UUID

from pydantic import (
    AwareDatetime, ConfigDict, Field, StrictBool, StrictInt, StrictStr,
    ValidationInfo, model_validator,
)

from app.feed_schemas import RECENT_FEED_LIMIT, RECENT_FEED_WINDOW_DAYS

from ..schemas import AgentModel


def _validate_recent_feed_history(records, reference_time) -> None:
    if len(records) > RECENT_FEED_LIMIT:
        raise ValueError("Recent Feed history exceeds limit")
    if len({row.paper_id for row in records}) != len(records):
        raise ValueError("Duplicate recent Feed paper identity")
    end = reference_time.astimezone(timezone.utc)
    start = end - timedelta(days=RECENT_FEED_WINDOW_DAYS)
    if any(not start < row.last_recommended_at.astimezone(timezone.utc) <= end for row in records):
        raise ValueError("Recent Feed recommendation lies outside (reference - 7 days, reference]")


class _ContextModel(AgentModel):
    model_config = ConfigDict(revalidate_instances="always")


class CollectionCoverage(_ContextModel):
    """For declared policy E/I: total=|E|, included=|I|, truncated=(|I|<|E|).

    Inclusion can select records without a SQL LIMIT. Counts describe that
    policy's eligible population, not all DB history or read completeness.
    """

    total_count: StrictInt = Field(ge=0)
    included_count: StrictInt = Field(ge=0)
    is_truncated: StrictBool

    @model_validator(mode="after")
    def validate_counts(self):
        if self.included_count > self.total_count:
            raise ValueError("included_count exceeds total_count")
        if self.is_truncated != (self.included_count < self.total_count):
            raise ValueError("is_truncated does not match counts")
        return self


T = TypeVar("T")


class ContextCollection(_ContextModel, Generic[T]):
    items: list[T]
    coverage: CollectionCoverage

    @model_validator(mode="after")
    def validate_length(self):
        if self.coverage.included_count != len(self.items):
            raise ValueError("included_count does not match items")
        return self


class CaregiverProfileContext(_ContextModel):
    """Stored caregiver profile; raw scores have no guaranteed clinical scale."""

    relationship: StrictStr | None
    burden_score: StrictInt | None
    mood_score: StrictInt | None
    lifestyle_tags: list[StrictStr]
    updated_at: AwareDatetime


class ClinicalAssessmentContext(_ContextModel):
    """Recorded assessment, not guaranteed current clinical state.

    Unknown types remain valid. Codebook/direction metadata is not diagnosis,
    cross-test score comparison, version normalization or a severity engine.
    """

    assessment_type: StrictStr
    score: Decimal | None
    result_detail: StrictStr | None
    assessed_at: date


class SafetyEventContext(_ContextModel):
    event_date: date
    has_fall: StrictBool
    has_wandering: StrictBool
    has_missing: StrictBool
    note: StrictStr | None


class MedicationContext(_ContextModel):
    """Preserve is_taking/date conflicts; only reversed intervals are invalid."""

    drug_name: StrictStr
    is_taking: StrictBool
    start_date: date | None
    end_date: date | None
    note: StrictStr | None

    @model_validator(mode="after")
    def validate_interval(self):
        if (self.start_date is not None and self.end_date is not None
                and self.start_date > self.end_date):
            raise ValueError("medication start_date exceeds end_date")
        return self


class MedicalVisitContext(_ContextModel):
    visit_date: date
    is_visited: StrictBool
    department: StrictStr | None
    visit_content: StrictStr | None


class ManagedPatientContext(_ContextModel):
    """Stored patient background; patient_ref is opaque and Context-local."""

    patient_ref: StrictStr = Field(min_length=1)
    dementia_stage: StrictStr | None
    diagnosis_date: date | None
    symptoms: list[StrictStr]
    interests: list[StrictStr]
    updated_at: AwareDatetime
    clinical_assessments: ContextCollection[ClinicalAssessmentContext]
    safety_events: ContextCollection[SafetyEventContext]
    medications: ContextCollection[MedicationContext]
    medical_visits: ContextCollection[MedicalVisitContext]


class CareLogContext(_ContextModel):
    """Null ref means unlinked here, not necessarily caregiver self-care."""

    patient_ref: StrictStr | None = Field(min_length=1)
    log_type: StrictStr
    content: StrictStr | None
    mood_tag: StrictStr | None
    logged_at: AwareDatetime


class RecentCareContext(_ContextModel):
    period_start: AwareDatetime
    period_end: AwareDatetime
    care_logs: ContextCollection[CareLogContext]

    @model_validator(mode="after")
    def validate_log_window(self):
        start = self.period_start.astimezone(timezone.utc)
        end = self.period_end.astimezone(timezone.utc)
        if start >= end:
            raise ValueError("recent care period must be increasing")
        for log in self.care_logs.items:
            if not start <= log.logged_at.astimezone(timezone.utc) < end:
                raise ValueError("care log lies outside [period_start, period_end)")
        return self


class LongTermPersonalizationSummary(_ContextModel):
    """User-scoped profile_summaries projection, not patient clinical memory."""

    period_start: date
    period_end: date
    summary: StrictStr | None
    updated_tags: list[StrictStr]
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_interval(self):
        if self.period_start > self.period_end:
            raise ValueError("summary period_start exceeds period_end")
        return self


class RecentFeedRecommendationContext(_ContextModel):
    paper_id: UUID
    pmid: StrictStr | None
    title: StrictStr
    last_recommended_at: AwareDatetime
    times_recommended: StrictInt = Field(ge=1)


class FeedPersonalizationContextV1(_ContextModel):
    """Validate with server-only context={\"service_timezone\": tzinfo}.

    No default service timezone is selected. Nullable/list fields are required:
    successful absence is explicit, never a representation of Loader failure.
    """

    schema_version: Literal["1"]
    reference_time: AwareDatetime
    caregiver_profile: CaregiverProfileContext | None
    managed_patient_profiles: ContextCollection[ManagedPatientContext]
    recent_care_context: RecentCareContext
    long_term_summary: LongTermPersonalizationSummary | None
    recently_recommended: list[RecentFeedRecommendationContext]

    @model_validator(mode="after")
    def validate_context(self, info: ValidationInfo):
        service_timezone = (info.context.get("service_timezone")
                            if isinstance(info.context, dict) else None)
        if not isinstance(service_timezone, tzinfo):
            raise ValueError("server validation context must supply service_timezone (tzinfo)")
        reference_date = self.reference_time.astimezone(service_timezone).date()
        reference_utc = self.reference_time.astimezone(timezone.utc)
        _validate_recent_feed_history(self.recently_recommended, self.reference_time)
        recent = self.recent_care_context
        if recent.period_end.astimezone(timezone.utc) != reference_utc:
            raise ValueError("recent period_end must equal reference_time")
        if recent.period_start.astimezone(timezone.utc) != reference_utc - timedelta(days=30):
            raise ValueError("recent period must span exactly 30 x 24 hours")

        patients = self.managed_patient_profiles.items
        refs = {patient.patient_ref for patient in patients}
        if len(refs) != len(patients):
            raise ValueError("duplicate patient_ref")
        for log in recent.care_logs.items:
            if log.patient_ref is not None and log.patient_ref not in refs:
                raise ValueError("care log references an unselected patient")
        for patient in patients:
            if patient.diagnosis_date is not None and patient.diagnosis_date > reference_date:
                raise ValueError("future diagnosis_date")
            for assessment in patient.clinical_assessments.items:
                if assessment.assessed_at > reference_date:
                    raise ValueError("future assessed_at")
            for event in patient.safety_events.items:
                if event.event_date > reference_date:
                    raise ValueError("future event_date")
            for visit in patient.medical_visits.items:
                if visit.is_visited and visit.visit_date > reference_date:
                    raise ValueError("future visit cannot be marked visited")

        summary = self.long_term_summary
        if summary is not None:
            if summary.period_end > reference_date:
                raise ValueError("future summary period_end")
            if summary.generated_at.astimezone(timezone.utc) > reference_utc:
                raise ValueError("future summary generated_at")
            if summary.period_end > summary.generated_at.astimezone(service_timezone).date():
                raise ValueError("summary period_end exceeds generated_at service date")
        return self
