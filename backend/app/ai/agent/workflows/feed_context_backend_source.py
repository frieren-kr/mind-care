"""Production Backend Snapshot adapter; the only official Feed Backend Source.

Inject get_feed_context_snapshot with its five keyword arguments unchanged.
Backend owns read-only REPEATABLE READ, eligibility, counts and record order.
No legacy aggregate, placeholders, re-selection, SQL, retries or fallback.
"""
from collections.abc import Callable
from datetime import date, datetime
from typing import TypeVar
from uuid import UUID

from app.feed_schemas import (
    FeedReadCareLog, FeedReadClinicalAssessment, FeedReadCollection,
    FeedReadManagedPatient, FeedReadMedicalVisit, FeedReadMedication,
    FeedReadSafetyEvent, FeedReadSnapshot,
)

from . import feed_context_source as s
from .feed_context_loader import _validate_source_invariants


MAX_MANAGED_PATIENTS = 20
T = TypeVar("T")


class ProductionFeedContextSourceError(RuntimeError):
    """Safe fixed text for Adapter conversion/validation failures only."""

    def __init__(self):
        super().__init__("Production Feed source validation failed")


def _read_collection(collection: FeedReadCollection[T],
                     project: Callable[[T], s.FeedSourceModel]) -> dict[str, object]:
    # Backend owns E/I and ordering. Never infer eligible counts from item length.
    return {"items": [project(row) for row in collection.items],
            "total_count": collection.total_count}


def _read_assessment(row: FeedReadClinicalAssessment) -> s.SourceClinicalAssessment:
    return s.SourceClinicalAssessment(
        patient_id=row.patient_id, assessment_type=row.assessment_type,
        score=row.score, result_detail=row.result_detail, assessed_at=row.assessed_at,
    )


def _read_safety(row: FeedReadSafetyEvent) -> s.SourceSafetyEvent:
    return s.SourceSafetyEvent(
        patient_id=row.patient_id, event_date=row.event_date, has_fall=row.has_fall,
        has_wandering=row.has_wandering, has_missing=row.has_missing, note=row.note,
    )


def _read_medication(row: FeedReadMedication) -> s.SourceMedication:
    return s.SourceMedication(
        patient_id=row.patient_id, drug_name=row.drug_name, is_taking=row.is_taking,
        start_date=row.start_date, end_date=row.end_date, note=row.note,
    )


def _read_visit(row: FeedReadMedicalVisit) -> s.SourceMedicalVisit:
    return s.SourceMedicalVisit(
        patient_id=row.patient_id, visit_date=row.visit_date, is_visited=row.is_visited,
        department=row.department, visit_content=row.visit_content,
    )


def _read_patient(row: FeedReadManagedPatient) -> s.SourceManagedPatient:
    return s.SourceManagedPatient(
        patient_id=row.patient_id, caregiver_id=row.caregiver_id,
        dementia_stage=row.dementia_stage, diagnosis_date=row.diagnosis_date,
        symptoms=list(row.symptoms), interests=list(row.interests), updated_at=row.updated_at,
        clinical_assessments=_read_collection(row.clinical_assessments, _read_assessment),
        safety_events=_read_collection(row.safety_events, _read_safety),
        medications=_read_collection(row.medications, _read_medication),
        medical_visits=_read_collection(row.medical_visits, _read_visit),
    )


def _read_log(row: FeedReadCareLog) -> s.SourceCareLog:
    return s.SourceCareLog(
        user_id=row.user_id, patient_id=row.patient_id, log_type=row.log_type,
        content=row.content, mood_tag=row.mood_tag, logged_at=row.logged_at,
    )


def _project_read_snapshot(raw: FeedReadSnapshot) -> s.FeedContextSourceSnapshot:
    caregiver, summary = raw.caregiver_profile, raw.long_term_summary
    return s.FeedContextSourceSnapshot(
        caregiver_profile=None if caregiver is None else s.SourceCaregiverProfile(
            user_id=caregiver.user_id, relationship=caregiver.relationship,
            burden_score=caregiver.burden_score, mood_score=caregiver.mood_score,
            lifestyle_tags=list(caregiver.lifestyle_tags), updated_at=caregiver.updated_at,
        ),
        managed_patients=_read_collection(raw.managed_patients, _read_patient),
        recent_care_logs=_read_collection(raw.recent_care_logs, _read_log),
        long_term_summary=None if summary is None else s.SourceLongTermSummary(
            user_id=summary.user_id, period_start=summary.period_start, period_end=summary.period_end,
            summary=summary.summary, updated_tags=list(summary.updated_tags), generated_at=summary.generated_at,
        ),
    )


def _validate_returned_read_policy(snapshot: s.FeedContextSourceSnapshot, reference_date: date) -> None:
    # Fail, do not filter: fixing a returned row cannot recover displaced valid
    # records or count-only violations. Backend must fix both SELECT and COUNT.
    for patient in snapshot.managed_patients.items:
        for event in patient.safety_events.items:
            if not (event.has_fall or event.has_wandering or event.has_missing):
                raise ValueError("Safety event has no incident flag")
        for visit in patient.medical_visits.items:
            if ((visit.is_visited and visit.visit_date > reference_date)
                    or (not visit.is_visited and visit.visit_date < reference_date)):
                raise ValueError("Visit state/date violates Feed policy")
        for medication in patient.medications.items:
            if (not medication.is_taking and medication.end_date is not None
                    and medication.end_date > reference_date):
                raise ValueError("Stopped medication has future end_date")


class ProductionFeedContextSource:
    """Project a real Backend FeedReadSnapshot into the existing Source contract.

    Inject app.db.feed_read.get_feed_context_snapshot. Backend owns read-only
    REPEATABLE READ, user existence, eligibility/selection, counts and ordering.
    Scope consistency is not authentication: callers must authorize user_id.
    Loader still owns reference/time validation, patient_ref and Coverage; the
    existing Provider Mapper keeps CareLog/Summary/narratives server-side.

    No SQL, extra reads, own clock, sorting, slicing, count correction or fallback.
    Backend exceptions retain their types; only Adapter validation gets fixed text.
    Returned visit/stopped-medication policy violations fail the whole Snapshot.
    Missing/unreturned eligibility and count errors cannot be repaired here.
    """

    def __init__(self, *, read_snapshot: Callable[..., FeedReadSnapshot]) -> None:
        if not callable(read_snapshot):
            raise ProductionFeedContextSourceError()
        self.read_snapshot = read_snapshot

    def load_snapshot(self, *, user_id: UUID, reference_time: datetime,
                      reference_date: date, recent_period_start: datetime,
                      recent_period_end: datetime) -> s.FeedContextSourceSnapshot:
        # Outside the validation wrapper: preserve FeedUserNotFound, FeedReadError
        # and its FeedPatientLimitExceeded subclass, including their identities.
        raw = self.read_snapshot(
            user_id=user_id, reference_time=reference_time, reference_date=reference_date,
            recent_period_start=recent_period_start, recent_period_end=recent_period_end,
        )
        try:
            if not isinstance(raw, FeedReadSnapshot):
                raise TypeError("Backend FeedReadSnapshot required")
            # Revalidate nested mutations/model_construct, without loose UUID,
            # bool, date or Decimal coercion. Never use this dump for projection.
            validated = FeedReadSnapshot.model_validate(raw.model_dump(warnings=False), strict=True)
            if len(validated.managed_patients.items) > MAX_MANAGED_PATIENTS:
                raise ValueError("Managed patient limit exceeded")
            snapshot = _project_read_snapshot(validated)
            _validate_source_invariants(snapshot, user_id)
            _validate_returned_read_policy(snapshot, reference_date)
            return snapshot
        except Exception:
            raise ProductionFeedContextSourceError() from None
