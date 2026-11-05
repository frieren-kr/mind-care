"""Existing authorized patient/date safety query projected without filtering days."""
from collections.abc import Callable
from datetime import date
from uuid import UUID

from app.user_schemas import PatientOut, SafetyEventOut

from ..schemas import AgentContext
from ..tools.safety_events import PatientSafetyEventsOutput, SafetyEventItem, SafetyEventsPeriod


class PatientSafetyEventsAdapter:
    """Read-only and stateless. DB uniqueness plus a 31-day period permits limit=31.

    Actual query completeness and ownership/query snapshot consistency require
    DB integration validation. No Feed filter, SQL or fallback rows are added.
    """

    def __init__(self, *, get_patient_fn: Callable[[UUID], PatientOut | None] | None = None,
                 list_safety_events_fn: Callable[..., list[SafetyEventOut]] | None = None):
        self.get_patient_fn = get_patient_fn
        self.list_safety_events_fn = list_safety_events_fn

    def get_patient_safety_events(self, *, context: AgentContext,
                                  start_date: date, end_date: date) -> PatientSafetyEventsOutput:
        if not isinstance(context.user_id, UUID) or not isinstance(context.patient_id, UUID):
            raise LookupError("Patient safety events unavailable")
        period = SafetyEventsPeriod(start_date=start_date, end_date=end_date)
        get_patient = self.get_patient_fn
        if get_patient is None:
            from app.db.user_functions import get_patient
        patient = get_patient(context.patient_id)
        if (patient is None or patient.id != context.patient_id
                or patient.caregiver_id != context.user_id):
            raise LookupError("Patient safety events unavailable")
        list_safety_events = self.list_safety_events_fn
        if list_safety_events is None:
            from app.db.user_functions import list_safety_events
        raw = list_safety_events(context.patient_id,
            start_date=period.start_date, end_date=period.end_date, limit=31)
        if not isinstance(raw, list) or len(raw) > 31:
            raise ValueError("Safety events query returned an invalid list or exceeded limit")
        events = []
        for row in raw:
            fields = row.model_fields_set if isinstance(row, SafetyEventOut) else row.keys() if isinstance(row, dict) else ()
            if not set(SafetyEventItem.model_fields).issubset(fields):
                raise ValueError("Safety events query omitted required projection fields")
            row = SafetyEventOut.model_validate(
                row.model_dump(warnings=False) if isinstance(row, SafetyEventOut) else row, strict=True)
            if row.patient_id != context.patient_id:
                raise LookupError("Patient safety events unavailable")
            events.append(SafetyEventItem(event_date=row.event_date, has_fall=row.has_fall,
                has_wandering=row.has_wandering, has_missing=row.has_missing, note=row.note))
        return PatientSafetyEventsOutput(period=period, events=events)
