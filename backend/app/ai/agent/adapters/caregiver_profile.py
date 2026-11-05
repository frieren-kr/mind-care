"""One existing caregiver lookup, user identity check and explicit Agent projection."""
from collections.abc import Callable
from uuid import UUID

from app.user_schemas import CaregiverProfileOut

from ..schemas import AgentContext
from ..tools.caregiver_profile import CaregiverProfileData, CaregiverProfileOutput


class CaregiverProfileAdapter:
    """Read-only, with no cached row or request-specific mutable state.

    Backend authenticates the existing user. A missing row is a normal absence;
    exceptions are not. Resolve the DB function lazily without extra queries.
    """

    def __init__(self, *, get_caregiver_profile_fn: Callable[[UUID], CaregiverProfileOut | None] | None = None):
        self.get_caregiver_profile_fn = get_caregiver_profile_fn

    def get_caregiver_profile(self, *, context: AgentContext) -> CaregiverProfileOutput:
        if not isinstance(context.user_id, UUID):
            raise LookupError("Caregiver profile unavailable")
        get_caregiver_profile = self.get_caregiver_profile_fn
        if get_caregiver_profile is None:
            from app.db.user_functions import get_caregiver_profile

        profile = get_caregiver_profile(context.user_id)
        if profile is None:
            return CaregiverProfileOutput(profile=None)
        if profile.user_id != context.user_id:
            raise LookupError("Caregiver profile unavailable")
        return CaregiverProfileOutput(profile=CaregiverProfileData(
            relationship=profile.relationship, burden_score=profile.burden_score,
            mood_score=profile.mood_score, lifestyle_tags=profile.lifestyle_tags,
            updated_at=profile.updated_at))
