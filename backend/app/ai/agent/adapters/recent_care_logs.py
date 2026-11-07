"""Validate an injected authorized care-log query and project the existing DTOs."""
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.feed_schemas import FeedReadCareLog, FeedReadCollection
from ..schemas import AgentContext
from ..tools.contracts import CareLogItem, RecentCareLogsInput, RecentCareLogsOutput, RecentCareLogsPeriod


class RecentCareLogsAdapter:
    """No SQL, fallback query or cached rows. A missing binding is an error.

    query_fn must accept user_id, patient_id, start_at, end_at, limit keywords
    and return FeedReadCollection[FeedReadCareLog]. It must check patient
    ownership even for empty results, select patient_care only, order by
    logged_at DESC/id DESC, and count before limit in one authorized snapshot.
    Row checks here cannot establish ownership or snapshot/count correctness.
    """

    def __init__(self, *, query_fn: Callable[..., FeedReadCollection[FeedReadCareLog]] | None = None):
        self.query_fn = query_fn

    def get_recent_care_logs(self, *, context: AgentContext, start_at: datetime,
                             end_at: datetime, limit: int) -> RecentCareLogsOutput:
        if not isinstance(context.user_id, UUID) or not isinstance(context.patient_id, UUID):
            raise LookupError("Patient care logs unavailable")
        period = RecentCareLogsPeriod(start_at=start_at, end_at=end_at)
        limit = RecentCareLogsInput(limit=limit).limit
        if self.query_fn is None:
            raise NotImplementedError("Patient care log query is not configured")
        raw = self.query_fn(user_id=context.user_id, patient_id=context.patient_id,
                            start_at=period.start_at, end_at=period.end_at, limit=limit)
        raw = raw.model_dump(warnings=False) if isinstance(raw, BaseModel) else raw
        result = FeedReadCollection[FeedReadCareLog].model_validate(raw)
        if len(result.items) > limit:
            raise ValueError("Care log query exceeded the requested limit")
        if any(row.user_id != context.user_id or row.patient_id != context.patient_id
               or row.log_type != "patient_care" for row in result.items):
            raise LookupError("Patient care logs unavailable")
        return RecentCareLogsOutput(period=period, total_count=result.total_count,
            logs=[CareLogItem(logged_at=row.logged_at, content=row.content, mood_tag=row.mood_tag)
                  for row in result.items])
