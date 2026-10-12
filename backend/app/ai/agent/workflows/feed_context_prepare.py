"""Production Stage 1 minimization and serialization for all managed patients.

Trusted callers inject authorized reads and time configuration. Loader errors
retain their codes. The legacy recommendations API shim is isolated and lazy;
it is not an alternative official Stage 1 or a Production fallback.
"""
from datetime import datetime, tzinfo
import json

from app.user_schemas import UserProfileContext

from ..schemas import AgentContext
from .feed_context import FeedPersonalizationContextV1
from .feed_context_loader import DefaultFeedContextLoader


class FeedProfileMappingError(RuntimeError):
    """Fixed failure only; no Backend data or validation input in public text."""

    def __init__(self):
        super().__init__("Feed profile mapping failed")


def _serialize_feed_provider_input(caregiver, profiles) -> str:
    """Production disclosure allowlist. Callers provide validated row views.

    A row view is (profile, safety rows, assessment rows), not a new DTO.
    Production-only logs/summary/coverage stay internal, never fabricated empty.
    """
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
    return json.dumps({
        "feed_scope": "caregiver_all",
        "caregiver": None if caregiver is None else {
            "relationship": caregiver.relationship, "lifestyle_tags": list(caregiver.lifestyle_tags),
        },
        "patients": patients,
    }, ensure_ascii=False, allow_nan=False)


def build_production_feed_provider_input(value: FeedPersonalizationContextV1, *,
                                         service_timezone: tzinfo) -> str:
    """Revalidate the full V1 before sharing only the approved profile signals.

    Source identity checks belong to DefaultFeedContextLoader, not opaque refs.
    No CareLog/summary, raw scores/dates, medication/visit narrative or coverage
    is disclosed. Exclusion means unavailable to this projection, not absence.
    """
    try:
        if not isinstance(value, FeedPersonalizationContextV1):
            raise TypeError("Production Context required")
        value = FeedPersonalizationContextV1.model_validate(
            value, context={"service_timezone": service_timezone})
        if value.managed_patient_profiles.coverage.is_truncated:
            raise ValueError("Managed patients must be complete")
        return _serialize_feed_provider_input(value.caregiver_profile, (
            (patient, patient.safety_events.items, patient.clinical_assessments.items)
            for patient in value.managed_patient_profiles.items
        ))
    except Exception:
        raise FeedProfileMappingError() from None


def prepare_production_feed_input(context: AgentContext, loader: DefaultFeedContextLoader) -> str:
    """Load all managed patients by user_id; patient_id never filters Production.

    Caller supplies an authorized Source and clock/timezone through the Loader.
    Loader errors keep their existing codes; no empty fallback or retry.
    """
    value = loader(context, recent_days=30)
    return build_production_feed_provider_input(value, service_timezone=loader.service_timezone)


def prepare_demo_feed_input(context: AgentContext, profile: UserProfileContext, *,
                            reference_time: datetime, service_timezone: tzinfo) -> str:
    """Temporary import-compatible entry point for recommendations.py only.

    Remove together with feed_demo_compat after the API owner switches its read
    and Stage 1 to the Production Snapshot/Loader contract. Never use internally.
    """
    from .feed_demo_compat import prepare_demo_feed_input as prepare_legacy
    return prepare_legacy(context, profile, reference_time=reference_time,
                          service_timezone=service_timezone)
