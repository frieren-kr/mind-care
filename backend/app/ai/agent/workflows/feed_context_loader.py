"""Context Loader Policy V1.1: assembly only, with no production DB wiring."""
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Callable, Literal, TypeVar
from uuid import UUID

from ..schemas import AgentContext
from .feed_context import FeedPersonalizationContextV1
from . import feed_context_source as s


FeedContextLoadCode = Literal[
    "invalid_request", "invalid_clock", "source_load_failed",
    "invalid_source_snapshot", "context_assembly_failed", "context_validation_failed",
]
_ERROR_CODES = frozenset((
    "invalid_request", "invalid_clock", "source_load_failed",
    "invalid_source_snapshot", "context_assembly_failed", "context_validation_failed",
))


class FeedContextLoadError(RuntimeError):
    """Only a fixed code is exposed; no Source, validation input or exception text.

    Suppressed chaining is not an APM/traceback-locals privacy guarantee.
    """

    def __init__(self, code: FeedContextLoadCode):
        if code not in _ERROR_CODES:
            raise ValueError("Unknown Feed Context load error code")
        self.code = code
        super().__init__(f"Feed Context load failed: {code}")


def _validate_source_invariants(snapshot: s.FeedContextSourceSnapshot, user_id: UUID) -> None:
    if snapshot.caregiver_profile is not None and snapshot.caregiver_profile.user_id != user_id:
        raise ValueError("Caregiver scope mismatch")
    if snapshot.long_term_summary is not None and snapshot.long_term_summary.user_id != user_id:
        raise ValueError("Summary scope mismatch")
    patients = snapshot.managed_patients
    if patients.total_count != len(patients.items):
        raise ValueError("Managed patients must be complete")
    ids = {patient.patient_id for patient in patients.items}
    if len(ids) != len(patients.items):
        raise ValueError("Duplicate managed patient identity")
    for patient in patients.items:
        if patient.caregiver_id != user_id:
            raise ValueError("Managed patient scope mismatch")
        for records in (patient.clinical_assessments, patient.safety_events,
                        patient.medications, patient.medical_visits):
            if any(record.patient_id != patient.patient_id for record in records.items):
                raise ValueError("Child ownership mismatch")
    for log in snapshot.recent_care_logs.items:
        if log.user_id != user_id:
            raise ValueError("CareLog scope mismatch")
        if log.patient_id is not None and log.patient_id not in ids:
            raise ValueError("Dangling CareLog patient identity")


T = TypeVar("T")


def _project_collection(source: s.SourceCollection[T],
                        project: Callable[[T], dict[str, object]]) -> dict[str, object]:
    items = [project(record) for record in source.items]
    return _collection_payload(source.total_count, items)


def _collection_payload(total: int, items: list[dict[str, object]]) -> dict[str, object]:
    # Source supplies |E|; returned items are I. Selection need not be SQL LIMIT.
    return {"items": items, "coverage": {
        "total_count": total, "included_count": len(items), "is_truncated": len(items) < total,
    }}


def _project_assessment(record: s.SourceClinicalAssessment) -> dict[str, object]:
    return {"assessment_type": record.assessment_type, "score": record.score,
            "result_detail": record.result_detail, "assessed_at": record.assessed_at}


def _project_safety(record: s.SourceSafetyEvent) -> dict[str, object]:
    return {"event_date": record.event_date, "has_fall": record.has_fall,
            "has_wandering": record.has_wandering, "has_missing": record.has_missing,
            "note": record.note}


def _project_medication(record: s.SourceMedication) -> dict[str, object]:
    return {"drug_name": record.drug_name, "is_taking": record.is_taking,
            "start_date": record.start_date, "end_date": record.end_date, "note": record.note}


def _project_visit(record: s.SourceMedicalVisit) -> dict[str, object]:
    return {"visit_date": record.visit_date, "is_visited": record.is_visited,
            "department": record.department, "visit_content": record.visit_content}


def _project_patient(patient: s.SourceManagedPatient, ref: str) -> dict[str, object]:
    return {
        "patient_ref": ref, "dementia_stage": patient.dementia_stage,
        "diagnosis_date": patient.diagnosis_date, "symptoms": patient.symptoms,
        "interests": patient.interests, "updated_at": patient.updated_at,
        "clinical_assessments": _project_collection(patient.clinical_assessments, _project_assessment),
        "safety_events": _project_collection(patient.safety_events, _project_safety),
        "medications": _project_collection(patient.medications, _project_medication),
        "medical_visits": _project_collection(patient.medical_visits, _project_visit),
    }


def _project_snapshot(snapshot: s.FeedContextSourceSnapshot, reference: datetime,
                      start: datetime) -> dict[str, object]:
    # Canonical identity order; not clinical ranking. Refs can change with the set.
    patients = sorted(snapshot.managed_patients.items, key=lambda patient: patient.patient_id.bytes)
    refs = {patient.patient_id: f"patient_{index}" for index, patient in enumerate(patients, start=1)}
    caregiver = snapshot.caregiver_profile
    summary = snapshot.long_term_summary

    def project_log(log: s.SourceCareLog) -> dict[str, object]:
        # Only Source null stays null; dangling IDs have already failed validation.
        return {"patient_ref": None if log.patient_id is None else refs[log.patient_id],
                "log_type": log.log_type, "content": log.content,
                "mood_tag": log.mood_tag, "logged_at": log.logged_at}

    return {
        "schema_version": "1", "reference_time": reference,
        "caregiver_profile": None if caregiver is None else {
            "relationship": caregiver.relationship, "burden_score": caregiver.burden_score,
            "mood_score": caregiver.mood_score, "lifestyle_tags": caregiver.lifestyle_tags,
            "updated_at": caregiver.updated_at,
        },
        "managed_patient_profiles": _collection_payload(snapshot.managed_patients.total_count, [
            _project_patient(patient, refs[patient.patient_id]) for patient in patients
        ]),
        "recent_care_context": {
            "period_start": start, "period_end": reference,
            "care_logs": _project_collection(snapshot.recent_care_logs, project_log),
        },
        "long_term_summary": None if summary is None else {
            "period_start": summary.period_start, "period_end": summary.period_end,
            "summary": summary.summary, "updated_tags": summary.updated_tags,
            "generated_at": summary.generated_at,
        },
    }


class DefaultFeedContextLoader:
    """Inject Source, clock and service timezone; no defaults, retry or selection.

    Source owns real authorization/eligibility and stable ordering. Loader only
    checks DTO consistency; structural minimization does not sanitize free text.
    """

    def __init__(self, *, source: s.FeedContextSource, clock: Callable[[], datetime],
                 service_timezone: tzinfo) -> None:
        self.source = source
        self.clock = clock
        self.service_timezone = service_timezone

    def __call__(self, context: AgentContext, *, recent_days: int) -> FeedPersonalizationContextV1:
        if type(recent_days) is not int or recent_days != 30:
            raise FeedContextLoadError("invalid_request")
        try:
            value = self.clock()
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Clock must return an aware datetime")
            reference = value.astimezone(timezone.utc)
            start = reference - timedelta(hours=720)
        except Exception:
            raise FeedContextLoadError("invalid_clock") from None
        try:
            # Keep service-timezone failures in the existing Context error category.
            if not isinstance(self.service_timezone, tzinfo):
                raise ValueError("Service timezone must be supplied")
            reference_date = reference.astimezone(self.service_timezone).date()
        except Exception:
            raise FeedContextLoadError("context_validation_failed") from None
        try:
            raw_snapshot = self.source.load_snapshot(
                user_id=context.user_id, reference_time=reference,
                reference_date=reference_date,
                recent_period_start=start, recent_period_end=reference,
            )
        except Exception:
            raise FeedContextLoadError("source_load_failed") from None
        try:
            # Revalidate even a mutated/model_construct Source instance, including children.
            snapshot = s.FeedContextSourceSnapshot.model_validate(raw_snapshot)
            _validate_source_invariants(snapshot, context.user_id)
        except Exception:
            raise FeedContextLoadError("invalid_source_snapshot") from None
        try:
            payload = _project_snapshot(snapshot, reference, start)
        except Exception:
            raise FeedContextLoadError("context_assembly_failed") from None
        try:
            # The sole final Context boundary owns all existing temporal/reference rules.
            return FeedPersonalizationContextV1.model_validate(
                payload, context={"service_timezone": self.service_timezone},
            )
        except Exception:
            raise FeedContextLoadError("context_validation_failed") from None
