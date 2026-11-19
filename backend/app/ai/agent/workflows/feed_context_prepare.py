"""Production Stage 1 minimization and serialization for all managed patients.

Trusted callers inject authorized reads and time configuration. Loader errors
retain their codes.
"""
from datetime import tzinfo
import json

from app.user_schemas import CARE_LOG_MAX_CHARS

from ..schemas import AgentContext
from .chat_history_context import chat_history_token_counter, serialize_chat_data
from .feed_context import ChatDailySummaryContext, FeedPersonalizationContextV1
from .feed_context_loader import DefaultFeedContextLoader


MAX_FEED_HISTORY_TITLES = 20
MAX_FEED_HISTORY_TITLE_CHARS = 300
MAX_FEED_HISTORY_TITLES_BYTES = 8192
FEED_CHAT_SUMMARY_TOKEN_LIMIT = 1000


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


def _project_chat_daily_summaries(records: list[ChatDailySummaryContext]) -> list[dict[str, str]]:
    if not records:
        return []
    # The tokenizer uses the same agent-model/summary-model precedence as Feed execution.
    count_tokens = chat_history_token_counter(cached_only=True)
    selected = []
    for record in sorted(records, key=lambda row: row.summary_date, reverse=True):
        row = {"summary_date": record.summary_date.isoformat(), "summary": record.summary}
        # Charge the exact chronological JSON order, preserving input order within a date.
        proposed = sorted([*selected, row], key=lambda item: item["summary_date"])
        if count_tokens(serialize_chat_data({"chat_daily_summaries": proposed})) > FEED_CHAT_SUMMARY_TOKEN_LIMIT:
            break
        selected = proposed
    return selected


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


def _serialize_feed_provider_input(caregiver, profiles, recent_feed_titles, care_logs, chat_daily_summaries) -> str:
    """Production disclosure allowlist. Callers provide validated row views.

    A row view is (profile, safety rows, assessment rows), not a new DTO.
    Monthly summaries and coverage stay internal.
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
    projected_logs = []
    for log in care_logs:
        if log.content is not None and len(log.content) > CARE_LOG_MAX_CHARS:
            raise ValueError("Care log content exceeds the length limit")
        projected_logs.append({
            "patient_ref": log.patient_ref,
            "log_type": log.log_type,
            "content": log.content,
            "mood_tag": log.mood_tag,
        })
    return json.dumps({
        "feed_scope": "caregiver_all",
        "caregiver": None if caregiver is None else {
            "relationship": caregiver.relationship, "lifestyle_tags": list(caregiver.lifestyle_tags),
        },
        "patients": patients,
        "recent_feed_titles": list(recent_feed_titles),
        "care_logs": projected_logs,
        "chat_daily_summaries": chat_daily_summaries,
    }, ensure_ascii=False, allow_nan=False)


def build_production_feed_provider_input(value: FeedPersonalizationContextV1, *,
                                         service_timezone: tzinfo) -> str:
    """Revalidate the full V1 before sharing only the approved profile signals.

    Source identity checks belong to DefaultFeedContextLoader, not opaque refs.
    Monthly summaries, profile scores/dates, medication/visit narrative and
    coverage stay internal. Daily chat summaries have a separate token budget.
    """
    try:
        if not isinstance(value, FeedPersonalizationContextV1):
            raise TypeError("Production Context required")
        value = FeedPersonalizationContextV1.model_validate(
            value, context={"service_timezone": service_timezone})
        if value.managed_patient_profiles.coverage.is_truncated:
            raise ValueError("Managed patients must be complete")
        profiles = (
            (patient, patient.safety_events.items, patient.clinical_assessments.items)
            for patient in value.managed_patient_profiles.items
        )
        return _serialize_feed_provider_input(
            value.caregiver_profile, profiles, _project_recent_feed_titles(value.recently_recommended),
            value.recent_care_context.care_logs.items,
            _project_chat_daily_summaries(value.recent_chat_summaries.items),
        )
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
