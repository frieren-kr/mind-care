"""Backend Feed Sources: development aggregate and Production Snapshot adapter.

Development alone uses UNREAD CareLog/Summary placeholders and selected-list
counts. Production consumes the real FeedReadSnapshot, preserving counts/order.

Implemented Backend-owned boundary, injected explicitly for Production:
app.db.feed_read.get_feed_context_snapshot(*, user_id, reference_time,
    reference_date, recent_period_start, recent_period_end)
    -> app.feed_schemas.FeedReadSnapshot
Backend owns read-only REPEATABLE READ, eligibility E, inclusion I, stable
ordering and pre-cap total_count=|E|. FeedUserNotFound/FeedReadError must remain
failures, never empty results. Returned visit-state and stopped-medication date
violations fail closed; correcting SELECT/COUNT eligibility remains Backend work.
"""
from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TypeVar
from uuid import UUID

from app.user_schemas import PatientContext, UserProfileContext
from app.feed_schemas import (
    FeedReadCareLog, FeedReadClinicalAssessment, FeedReadCollection,
    FeedReadManagedPatient, FeedReadMedicalVisit, FeedReadMedication,
    FeedReadSafetyEvent, FeedReadSnapshot,
)

from . import feed_context_source as s
from .feed_context_loader import _validate_source_invariants


MAX_MANAGED_PATIENTS = 20

_SELECTED_FIELDS = (
    "latest_assessments", "recent_safety_events", "current_medications",
    "recent_visits", "upcoming_visits",
)
T = TypeVar("T")


class DevelopmentFeedContextSourceError(RuntimeError):
    """Fixed failure text only; never turn a failed Backend read into absence."""

    def __init__(self):
        super().__init__("Development Feed source load failed")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Aware Backend timestamp required")
    return value.astimezone(timezone.utc)


def _validated_profile(raw: UserProfileContext | None, user_id: UUID) -> UserProfileContext:
    if (not isinstance(raw, UserProfileContext)
            or not {"caregiver_profile", "patients"} <= raw.model_fields_set):
        raise ValueError("Explicit Backend profile read required")
    # All managed patients or failure; never trim to the cap or select patient_id.
    if len(raw.patients) > MAX_MANAGED_PATIENTS:
        raise ValueError("Managed patient limit exceeded")
    for patient in raw.patients:
        if (not isinstance(patient, PatientContext)
                or not {"patient", *_SELECTED_FIELDS} <= patient.model_fields_set):
            raise ValueError("Explicit Backend selected lists required")
    # Revalidate mutated/model_construct instances, including nested rows. This
    # dump is for Backend format validation ONLY; Source projection is explicit.
    profile = UserProfileContext.model_validate(raw.model_dump(warnings=False), strict=True)
    if profile.user_id != user_id:
        raise ValueError("Backend user scope mismatch")
    _aware_utc(profile.generated_at)
    # Source DTOs omit record IDs. Detect duplicates before losing that evidence,
    # including across patients and the two visit lists; never deduplicate rows.
    seen = {field: set() for field in _SELECTED_FIELDS if field != "upcoming_visits"}
    for patient in profile.patients:
        for field in _SELECTED_FIELDS:
            ids = seen["recent_visits" if field == "upcoming_visits" else field]
            for row in getattr(patient, field):
                if row.id in ids:
                    raise ValueError("Duplicate Backend record identity")
                ids.add(row.id)
    return profile


def _ordered(rows: list[T], date_field: str) -> list[T]:
    """Source baseline: date DESC NULLS LAST, created_at DESC, UUID ASC.

    Reorders only the returned selection; does not select additional history.
    Stable passes retain created_at/id ties without numeric timestamp rounding.
    """
    result = sorted(rows, key=lambda row: row.id.bytes)
    result.sort(key=lambda row: _aware_utc(row.created_at), reverse=True)
    result.sort(key=lambda row: (getattr(row, date_field) is not None,
                               getattr(row, date_field) or date.min), reverse=True)
    return result


def _collection_payload(items: list[T]) -> dict[str, object]:
    # TODO: replace selected-list length with Backend Feed Read eligible counts.
    # Development count only; not DB history coverage or evidence of all reads.
    # The owning Source DTO validates the concrete SourceCollection[T] type.
    return {"items": items, "total_count": len(items)}


def _project_patient(item: PatientContext, reference_date: date) -> s.SourceManagedPatient:
    patient = item.patient
    types = [row.assessment_type for row in item.latest_assessments]
    if len(set(types)) != len(types):
        raise ValueError("Duplicate latest assessment type")
    days = [row.event_date for row in item.recent_safety_events]
    if len(set(days)) != len(days):
        raise ValueError("Duplicate safety event date")
    if any(not row.is_taking for row in item.current_medications):
        raise ValueError("Invalid current medication selection")
    if any(not (row.has_fall or row.has_wandering or row.has_missing)
           for row in item.recent_safety_events):
        raise ValueError("Invalid selected safety event")
    if any(not row.is_visited for row in item.recent_visits):
        raise ValueError("Invalid completed visit selection")
    if any(row.is_visited or row.visit_date < reference_date for row in item.upcoming_visits):
        raise ValueError("Invalid upcoming visit selection")
    return s.SourceManagedPatient(
        patient_id=patient.id, caregiver_id=patient.caregiver_id,
        dementia_stage=patient.dementia_stage, diagnosis_date=patient.diagnosis_date,
        symptoms=list(patient.symptoms), interests=list(patient.interests), updated_at=patient.updated_at,
        clinical_assessments=_collection_payload([s.SourceClinicalAssessment(
            patient_id=row.patient_id, assessment_type=row.assessment_type,
            score=None if row.score is None else Decimal(str(row.score)),
            result_detail=row.result_detail, assessed_at=row.assessed_at,
        ) for row in _ordered(item.latest_assessments, "assessed_at")]),
        safety_events=_collection_payload([s.SourceSafetyEvent(
            patient_id=row.patient_id, event_date=row.event_date, has_fall=row.has_fall,
            has_wandering=row.has_wandering, has_missing=row.has_missing, note=row.note,
        ) for row in _ordered(item.recent_safety_events, "event_date")]),
        medications=_collection_payload([s.SourceMedication(
            patient_id=row.patient_id, drug_name=row.drug_name, is_taking=row.is_taking,
            start_date=row.start_date, end_date=row.end_date, note=row.note,
        ) for row in _ordered(item.current_medications, "start_date")]),
        medical_visits=_collection_payload([s.SourceMedicalVisit(
            patient_id=row.patient_id, visit_date=row.visit_date, is_visited=row.is_visited,
            department=row.department, visit_content=row.visit_content,
        ) for row in _ordered(item.recent_visits + item.upcoming_visits, "visit_date")]),
    )


class DevelopmentFeedContextSource:
    """Inject get_user_profile_context for development Stage 1, for all patients.

    Structurally compatible with FeedContextSource, semantically NOT Production:
    CareLogs and monthly summary are UNREAD placeholders, not successful absence.
    total_count=len(items) describes current/latest selected lists only. Backend
    helpers use their own clock and separate reads, not a consistent Feed snapshot.
    Data ownership checks are NOT authentication; trusted callers must supply an
    authenticated user_id. No SQL, Tools, patient selection or API wiring here.
    Zero patients is valid; 1..20 are kept completely; over 20 fails, not truncates.

    The Backend Snapshot now exists; use the separate ProductionFeedContextSource
    for that boundary. Keep this development-only compatibility path unchanged.
    Production passes all supplied reference/window arguments unchanged. Backend
    owns record-specific period/cap selection and exact eligible counts; Adapter
    must not slice/reselect or infer missing records. Keep explicit projection,
    existing Loader validation/ref/coverage and Mapper disclosure responsibilities.
    Replace UNREAD CareLog/Summary placeholders, verify snapshot/time consistency
    and Production integration. Do not invent Backend DTO fields or exceptions.
    """

    def __init__(self, *, read_profile: Callable[[UUID], UserProfileContext | None]) -> None:
        if not callable(read_profile):
            raise DevelopmentFeedContextSourceError()
        self.read_profile = read_profile

    def load_snapshot(self, *, user_id: UUID, reference_time: datetime,
                      reference_date: date, recent_period_start: datetime,
                      recent_period_end: datetime) -> s.FeedContextSourceSnapshot:
        """Read the legacy aggregate once; it cannot accept Feed time bounds.

        reference_date checks upcoming selection; Loader validates full Context
        dates/windows. The aggregate's own clock is not replaced or certified.
        """
        try:
            if not isinstance(user_id, UUID):
                raise ValueError("Trusted UUID required")
            profile = _validated_profile(self.read_profile(user_id), user_id)
            caregiver = profile.caregiver_profile
            snapshot = s.FeedContextSourceSnapshot(
                caregiver_profile=None if caregiver is None else s.SourceCaregiverProfile(
                    user_id=caregiver.user_id, relationship=caregiver.relationship,
                    burden_score=caregiver.burden_score, mood_score=caregiver.mood_score,
                    lifestyle_tags=list(caregiver.lifestyle_tags), updated_at=caregiver.updated_at,
                ),
                managed_patients=_collection_payload([_project_patient(row, reference_date)
                                                      for row in profile.patients]),
                # TODO: real CareLog read/count for [recent_period_start, end).
                # UNREAD development placeholder: NOT a DB result of zero logs.
                recent_care_logs=s.SourceCollection[s.SourceCareLog](items=[], total_count=0),
                # TODO: real monthly personalization summary read.
                # UNREAD development placeholder: NOT proof that no summary exists.
                long_term_summary=None,
            )
            # Reuse Loader's ownership/identity invariants, not a second policy.
            # Final temporal validation and patient_ref generation stay in Loader.
            _validate_source_invariants(snapshot, user_id)
            return snapshot
        except Exception:
            raise DevelopmentFeedContextSourceError() from None


class ProductionFeedContextSourceError(RuntimeError):
    """Safe fixed text for Adapter conversion/validation failures only."""

    def __init__(self):
        super().__init__("Production Feed source validation failed")


def _read_collection(collection: FeedReadCollection[T],
                     project: Callable[[T], s.FeedSourceModel]) -> dict[str, object]:
    # Backend owns E/I and ordering. Never use the development length-count helper.
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
