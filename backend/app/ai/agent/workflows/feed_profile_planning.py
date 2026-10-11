"""UserProfileContext to minimized caregiver/patient demo planning input.

Consumes Backend DTOs after a trusted read; scope consistency is not authorization.
No DB calls, counts, Source Snapshot, CareLog/Summary placeholders or full V1
claims. Backend selected lists are preserved separately, not promoted to history.
"""
from datetime import date, datetime, timezone, tzinfo
from decimal import Decimal
import json
from typing import Literal, Sequence
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, StrictStr, ValidationInfo, model_validator

from app.user_schemas import CaregiverProfileOut, MedicalVisitOut, PatientContext, UserProfileContext

from ..schemas import AgentContext, AgentModel
from .feed_context import (
    CaregiverProfileContext, ClinicalAssessmentContext, MedicationContext, MedicalVisitContext, SafetyEventContext,
)


# Trusted server configuration for this mapper; never serialized as patient data.
PROFILE_PLANNING_AVAILABLE_SOURCES = frozenset({
    "caregiver_profile", "stored_patient_profile", "latest_assessments", "current_medications",
    "recent_safety_events", "recent_visits", "upcoming_visits",
})


class FeedPatientPlanningProfile(AgentModel):
    """Stored background and selected signals, without history/count claims."""

    model_config = ConfigDict(revalidate_instances="always")

    patient_ref: StrictStr = Field(min_length=1)
    dementia_stage: StrictStr | None
    diagnosis_date: date | None
    symptoms: list[StrictStr]
    interests: list[StrictStr]
    updated_at: AwareDatetime
    latest_assessments: list[ClinicalAssessmentContext]
    current_medications: list[MedicationContext]
    recent_safety_events: list[SafetyEventContext]
    recent_visits: list[MedicalVisitContext]
    upcoming_visits: list[MedicalVisitContext]


class FeedProfilePlanningInputV1(AgentModel):
    """Separate demo input, neither full Feed Context V1 nor a Lite/V2 version.

    Validate with server-only service_timezone. reference_time is a validation
    anchor; it does not certify Backend helper clocks, auth, counts or snapshot.
    No CareLog/Summary fields: unavailable sources are not successful absence.
    """

    model_config = ConfigDict(revalidate_instances="always")

    schema_version: Literal["feed-profile-planning-1"]
    reference_time: AwareDatetime
    caregiver_profile: CaregiverProfileContext | None
    managed_patient_profiles: list[FeedPatientPlanningProfile]

    @model_validator(mode="after")
    def validate_signals(self, info: ValidationInfo):
        service_timezone = (info.context.get("service_timezone")
                            if isinstance(info.context, dict) else None)
        if not isinstance(service_timezone, tzinfo):
            raise ValueError("Planning validation requires service_timezone")
        reference_date = self.reference_time.astimezone(service_timezone).date()
        patients = self.managed_patient_profiles
        if len({patient.patient_ref for patient in patients}) != len(patients):
            raise ValueError("Duplicate planning patient_ref")
        for patient in patients:
            if patient.diagnosis_date is not None and patient.diagnosis_date > reference_date:
                raise ValueError("Future diagnosis_date")
            types = [row.assessment_type for row in patient.latest_assessments]
            if len(set(types)) != len(types):
                raise ValueError("Duplicate latest assessment type")
            if any(row.assessed_at > reference_date for row in patient.latest_assessments):
                raise ValueError("Future assessed_at")
            if any(not row.is_taking for row in patient.current_medications):
                raise ValueError("Current medication lacks stored is_taking flag")
            if any(row.event_date > reference_date or not (
                    row.has_fall or row.has_wandering or row.has_missing)
                   for row in patient.recent_safety_events):
                raise ValueError("Invalid selected safety event")
            if any(not row.is_visited or row.visit_date > reference_date for row in patient.recent_visits):
                raise ValueError("Invalid selected completed visit")
            if any(row.is_visited or row.visit_date < reference_date for row in patient.upcoming_visits):
                raise ValueError("Invalid selected upcoming visit")
        return self


class FeedProfileMappingError(RuntimeError):
    """Fixed failure only; no Backend data or validation input in public text."""

    def __init__(self):
        super().__init__("Feed profile mapping failed")


def _serialize_feed_provider_input(caregiver, profiles, *, feed_scope: str) -> str:
    """Sole disclosure allowlist. Callers provide already validated row views.

    A row view is (profile, safety rows, assessment rows), not a new DTO.
    Production-only logs/summary/coverage stay internal, never fabricated empty.
    """
    if feed_scope not in ("single_patient_demo", "caregiver_all"):
        raise ValueError("Invalid Feed scope")
    patients = [{
        "patient_ref": patient.patient_ref,
        "dementia_stage": patient.dementia_stage,
        "symptoms": list(patient.symptoms),
        "interests": list(patient.interests),
        "safety_events": [label for flag, label in (
            ("has_fall", "fall"), ("has_wandering", "wandering"), ("has_missing", "missing")
        ) if any(getattr(row, flag) for row in safety)],
        "assessment_types": [row.assessment_type for row in assessments],
    } for patient, safety, assessments in profiles]
    if feed_scope == "single_patient_demo" and len(patients) > 1:
        raise ValueError("Demo requires zero or one selected patient")
    return json.dumps({
        "feed_scope": feed_scope,
        "caregiver": None if caregiver is None else {
            "relationship": caregiver.relationship, "lifestyle_tags": list(caregiver.lifestyle_tags),
        },
        "patients": patients,
    }, ensure_ascii=False, allow_nan=False)


def build_feed_provider_input(planning: FeedProfilePlanningInputV1) -> str:
    """Minimize trusted, selected Demo input; Stage 1 owns validation/selection.

    No full DTO dump, ref regeneration or free-text sanitization. The execution
    layer consumes this string without projecting again.
    """
    try:
        if not isinstance(planning, FeedProfilePlanningInputV1):
            raise TypeError("Trusted Demo planning input required")
        return _serialize_feed_provider_input(planning.caregiver_profile, (
            (patient, patient.recent_safety_events, patient.latest_assessments)
            for patient in planning.managed_patient_profiles
        ), feed_scope="single_patient_demo")
    except Exception:
        raise FeedProfileMappingError() from None


def _visit_payload(row: MedicalVisitOut) -> dict[str, object]:
    # Direct legacy/free memo only; never concat or parse structured 005 fields.
    return {"visit_date": row.visit_date, "is_visited": row.is_visited,
            "department": row.department, "visit_content": row.visit_content}


def _project_managed_patient_profiles(
    context: AgentContext, profiles: Sequence[PatientContext],
) -> list[FeedPatientPlanningProfile]:
    """Reuse patient/child scope checks and allowlists; never assemble user input.

    Selected fields must be explicit. Keep UUID-byte ordering, per-patient
    provenance and all rows; final planning validation owns temporal semantics.
    AgentContext.patient_id does not filter this caregiver-scoped collection.
    """
    ids = set()
    selected = {"latest_assessments", "current_medications", "recent_safety_events",
                "recent_visits", "upcoming_visits"}
    for item in profiles:
        if not isinstance(item, PatientContext) or not {"patient", *selected} <= item.model_fields_set:
            raise ValueError("Explicit Backend patient read required")
        patient = item.patient
        if (not isinstance(patient.id, UUID) or patient.id in ids
                or patient.caregiver_id != context.user_id):
            raise ValueError("Invalid patient ownership/identity")
        ids.add(patient.id)
        visit_ids = set()
        for field in selected:
            row_ids = set()
            for row in getattr(item, field):
                if row.patient_id != patient.id or not isinstance(row.id, UUID) or row.id in row_ids:
                    raise ValueError("Invalid child ownership/identity")
                row_ids.add(row.id)
                if field in ("recent_visits", "upcoming_visits"):
                    if row.id in visit_ids:
                        raise ValueError("Duplicate visit across selected lists")
                    visit_ids.add(row.id)
    payload = []
    for index, item in enumerate(sorted(profiles, key=lambda row: row.patient.id.bytes), start=1):
        patient = item.patient
        payload.append({
            "patient_ref": f"patient_{index}", "dementia_stage": patient.dementia_stage,
            "diagnosis_date": patient.diagnosis_date, "symptoms": patient.symptoms,
            "interests": patient.interests, "updated_at": patient.updated_at,
            "latest_assessments": [{
                "assessment_type": row.assessment_type,
                "score": None if row.score is None else Decimal(str(row.score)),
                "result_detail": row.result_detail, "assessed_at": row.assessed_at,
            } for row in item.latest_assessments],
            "current_medications": [{
                "drug_name": row.drug_name, "is_taking": row.is_taking,
                "start_date": row.start_date, "end_date": row.end_date, "note": row.note,
            } for row in item.current_medications],
            "recent_safety_events": [{
                "event_date": row.event_date, "has_fall": row.has_fall,
                "has_wandering": row.has_wandering, "has_missing": row.has_missing, "note": row.note,
            } for row in item.recent_safety_events],
            "recent_visits": [_visit_payload(row) for row in item.recent_visits],
            "upcoming_visits": [_visit_payload(row) for row in item.upcoming_visits],
        })
    return [FeedPatientPlanningProfile.model_validate(item) for item in payload]


class FeedUserProfileSourceMappingV1:
    """Assemble demo input from an explicit Backend UserProfileContext.

    Identity/scope consistency and field omission checks are not authorization
    or evidence of DB read completeness. Trusted caller owns auth/read success.
    reference_time is caller-supplied, not generated_at or a DB AS-OF timestamp.
    Raw identity is minimized, but free text is not sanitized for external LLMs.
    No production Source, fresh clock, retry or partial-success fallback.
    """

    def __init__(self, *, service_timezone: tzinfo):
        self.service_timezone = service_timezone

    def __call__(self, context: AgentContext, profile: UserProfileContext, *,
                 reference_time: datetime) -> FeedProfilePlanningInputV1:
        try:
            if (not isinstance(reference_time, datetime) or reference_time.tzinfo is None
                    or reference_time.utcoffset() is None):
                raise ValueError("Aware reference_time required")
            if not isinstance(self.service_timezone, tzinfo):
                raise ValueError("Service timezone required")
            reference = reference_time.astimezone(timezone.utc)
            # Reject bad timezone before reading/projecting supplied rows.
            reference.astimezone(self.service_timezone).date()
            if (not isinstance(profile, UserProfileContext)
                    or not {"caregiver_profile", "patients"} <= profile.model_fields_set):
                raise ValueError("Explicit Backend user profile fields required")
            if profile.user_id != context.user_id:
                raise ValueError("Invalid user scope")
            caregiver = profile.caregiver_profile
            caregiver_payload = None
            if caregiver is not None:
                if (not isinstance(caregiver, CaregiverProfileOut)
                        or caregiver.user_id != profile.user_id):
                    raise ValueError("Invalid caregiver scope")
                caregiver_payload = CaregiverProfileContext.model_validate({
                    "relationship": caregiver.relationship,
                    "burden_score": caregiver.burden_score,
                    "mood_score": caregiver.mood_score,
                    "lifestyle_tags": caregiver.lifestyle_tags,
                    "updated_at": caregiver.updated_at,
                })
            patients = _project_managed_patient_profiles(context, profile.patients)
            return FeedProfilePlanningInputV1.model_validate({
                "schema_version": "feed-profile-planning-1", "reference_time": reference,
                "caregiver_profile": caregiver_payload,
                "managed_patient_profiles": patients,
            }, context={"service_timezone": self.service_timezone})
        except Exception:
            raise FeedProfileMappingError() from None
