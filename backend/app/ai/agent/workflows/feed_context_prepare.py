"""Stage 1 Demo selection and Production projection; no DB handlers/new DTOs.

Trusted callers inject authorized reads and time configuration. These helpers
never interpret read failure as Cold Start or expose raw narrative/history.
"""
from datetime import datetime, tzinfo
from uuid import UUID

from app.user_schemas import PatientContext, UserProfileContext

from ..schemas import AgentContext
from .feed_context import FeedPersonalizationContextV1
from .feed_context_loader import DefaultFeedContextLoader
from .feed_profile_planning import (
    FeedProfileMappingError, FeedUserProfileSourceMappingV1,
    _serialize_feed_provider_input, build_feed_provider_input,
)


def prepare_demo_feed_input(context: AgentContext, profile: UserProfileContext, *,
                            reference_time: datetime, service_timezone: tzinfo) -> str:
    """Select 0..1 owned patient, then reuse the unchanged multi-patient Mapper.

    Selection checks every candidate identity/owner; selected child/time rules
    remain the Mapper's responsibility. The supplied Backend DTO is unchanged.
    """
    try:
        if (not isinstance(profile, UserProfileContext)
                or not {"caregiver_profile", "patients"} <= profile.model_fields_set
                or profile.user_id != context.user_id):
            raise ValueError("Explicit owned Backend read required")
        ids = set()
        for row in profile.patients:
            if (not isinstance(row, PatientContext) or "patient" not in row.model_fields_set
                    or not isinstance(row.patient.id, UUID) or row.patient.id in ids
                    or row.patient.caregiver_id != context.user_id):
                raise ValueError("Invalid candidate ownership/identity")
            ids.add(row.patient.id)
        if context.patient_id is None:
            if len(profile.patients) > 1:
                raise ValueError("Ambiguous Demo patient scope")
            selected = profile.patients
        else:
            if not isinstance(context.patient_id, UUID) or context.patient_id not in ids:
                raise ValueError("Selected patient is not managed")
            selected = [row for row in profile.patients if row.patient.id == context.patient_id]
        mapped = FeedUserProfileSourceMappingV1(service_timezone=service_timezone)(
            context, profile.model_copy(update={"patients": selected}), reference_time=reference_time)
        return build_feed_provider_input(mapped)
    except Exception:
        raise FeedProfileMappingError() from None


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
        ), feed_scope="caregiver_all")
    except Exception:
        raise FeedProfileMappingError() from None


def prepare_production_feed_input(context: AgentContext, loader: DefaultFeedContextLoader) -> str:
    """Load all managed patients by user_id; patient_id never filters Production.

    Caller supplies an authorized Source and clock/timezone through the Loader.
    Loader errors keep their existing codes; no empty fallback or retry.
    """
    value = loader(context, recent_days=30)
    return build_production_feed_provider_input(value, service_timezone=loader.service_timezone)
