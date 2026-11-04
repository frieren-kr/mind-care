"""Production Stage 1 minimization and serialization for all managed patients.

Trusted callers inject authorized reads and time configuration. Loader errors
retain their codes.
"""
from datetime import tzinfo
import json

from ..schemas import AgentContext
from .feed_context import FeedPersonalizationContextV1
from .feed_context_loader import DefaultFeedContextLoader


MAX_FEED_HISTORY_TITLES = 20
MAX_FEED_HISTORY_TITLE_CHARS = 300
MAX_FEED_HISTORY_TITLES_BYTES = 8192


def _project_recent_feed_titles(recently_recommended) -> list[str]:
    titles = []
    for paper in recently_recommended[:MAX_FEED_HISTORY_TITLES]:
        title = paper.title
        if len(title) > MAX_FEED_HISTORY_TITLE_CHARS:
            continue
        encoded_size = len(json.dumps([*titles, title], ensure_ascii=False,
                                      allow_nan=False).encode("utf-8"))
        if encoded_size > MAX_FEED_HISTORY_TITLES_BYTES:
            break
        titles.append(title)
    return titles


class PreparedFeedInput(str):
    """Provider JSON text with immutable request-local, server-only PMID metadata."""

    __slots__ = ("_previously_recommended_pmids",)

    def __new__(cls, value: str, previously_recommended_pmids: frozenset[str] = frozenset()):
        instance = super().__new__(cls, value)
        object.__setattr__(instance, "_previously_recommended_pmids", frozenset(previously_recommended_pmids))
        return instance

    @property
    def previously_recommended_pmids(self) -> frozenset[str]:
        return self._previously_recommended_pmids

    def __setattr__(self, name, value):
        raise AttributeError("Prepared Feed input is immutable")

    def __delattr__(self, name):
        raise AttributeError("Prepared Feed input is immutable")

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        memo[id(self)] = self
        return self


class FeedProfileMappingError(RuntimeError):
    """Fixed failure only; no Backend data or validation input in public text."""

    def __init__(self):
        super().__init__("Feed profile mapping failed")


def _serialize_feed_provider_input(caregiver, profiles, recent_feed_titles) -> str:
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
        "recent_feed_titles": list(recent_feed_titles),
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
        ), _project_recent_feed_titles(value.recently_recommended))
    except Exception:
        raise FeedProfileMappingError() from None


def prepare_production_feed_input(context: AgentContext, loader: DefaultFeedContextLoader) -> str:
    """Load all managed patients by user_id; patient_id never filters Production.

    Caller supplies an authorized Source and clock/timezone through the Loader.
    Loader errors keep their existing codes; no empty fallback or retry.
    """
    value = loader(context, recent_days=30)
    text = build_production_feed_provider_input(value, service_timezone=loader.service_timezone)
    return PreparedFeedInput(text, frozenset(
        row.pmid for row in value.recently_recommended if row.pmid is not None and row.pmid.strip()
    ))
