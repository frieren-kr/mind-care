"""Authorized visit projection using existing DB DTOs and an upcoming query seam."""
from collections.abc import Callable
from datetime import date
from typing import Literal, Protocol
from uuid import UUID

from app.user_schemas import MedicalVisitOut, PatientOut

from ..schemas import AgentContext
from ..tools.medical_visits import MedicalVisitItem, PatientMedicalVisitsInput, PatientMedicalVisitsOutput


class UpcomingMedicalVisitsQuery(Protocol):
    def __call__(self, *, user_id: UUID, patient_id: UUID, start_date: date,
                 end_date: date | None, limit: int) -> list[MedicalVisitOut]:
        """Authorize user/patient; select unvisited rows, inclusive dates, then limit.

        Order by visit_date ASC, created_at DESC, id ASC BEFORE limiting.
        No SQL implementation or fallback is supplied by the Agent.
        """
        ...


class PatientMedicalVisitsAdapter:
    """Read-only, with no cached rows, filtering, sorting or silent truncation.

    Ownership lookup and legacy list query are separate DB calls; transaction
    consistency is a DB integration concern. The upcoming query must itself
    authorize user_id even when it returns no rows.
    """

    def __init__(self, *, get_patient_fn: Callable[[UUID], PatientOut | None] | None = None,
                 list_medical_visits_fn: Callable[..., list[MedicalVisitOut]] | None = None,
                 upcoming_query_fn: UpcomingMedicalVisitsQuery | None = None):
        self.get_patient_fn = get_patient_fn
        self.list_medical_visits_fn = list_medical_visits_fn
        self.upcoming_query_fn = upcoming_query_fn

    def get_patient_medical_visits(self, *, context: AgentContext,
                                   status: Literal["completed", "upcoming", "unvisited", "all"],
                                   start_date: date | None, end_date: date | None,
                                   limit: int, today_kst: date | None) -> PatientMedicalVisitsOutput:
        if not isinstance(context.user_id, UUID) or not isinstance(context.patient_id, UUID):
            raise LookupError("Patient medical visits unavailable")
        args = PatientMedicalVisitsInput(status=status, start_date=start_date, end_date=end_date, limit=limit)
        get_patient = self.get_patient_fn
        if get_patient is None:
            from app.db.user_functions import get_patient
        patient = get_patient(context.patient_id)
        if (patient is None or patient.id != context.patient_id
                or patient.caregiver_id != context.user_id):
            raise LookupError("Patient medical visits unavailable")
        start, end = args.start_date, args.end_date
        if args.status == "upcoming":
            if type(today_kst) is not date:
                raise ValueError("Medical visit reference must be a server KST date")
            start = max(start or today_kst, today_kst)
            if end is not None and end < start:
                return PatientMedicalVisitsOutput(visits=[])
            if self.upcoming_query_fn is None:
                raise NotImplementedError("Upcoming medical visits query is not configured")
            raw = self.upcoming_query_fn(user_id=context.user_id, patient_id=context.patient_id,
                start_date=start, end_date=end, limit=args.limit)
            visited = False
        else:
            list_medical_visits = self.list_medical_visits_fn
            if list_medical_visits is None:
                from app.db.user_functions import list_medical_visits
            visited = {"completed": True, "unvisited": False, "all": None}[args.status]
            raw = list_medical_visits(context.patient_id, is_visited=visited,
                start_date=start, end_date=end, limit=args.limit)
        if not isinstance(raw, list) or len(raw) > args.limit:
            raise ValueError("Medical visit query returned an invalid list or exceeded limit")
        rows = []
        for row in raw:
            # Nullable defaults must not hide a missing SELECT column. Check the
            # original field set before model_dump fills defaults in DB models.
            fields = row.model_fields_set if isinstance(row, MedicalVisitOut) else row.keys() if isinstance(row, dict) else ()
            if not set(MedicalVisitItem.model_fields).issubset(fields):
                raise ValueError("Medical visit query omitted required projection fields")
            row = MedicalVisitOut.model_validate(
                row.model_dump(warnings=False) if isinstance(row, MedicalVisitOut) else row, strict=True)
            if (row.patient_id != context.patient_id or (visited is not None and row.is_visited is not visited)
                    or (start is not None and row.visit_date < start)
                    or (end is not None and row.visit_date > end)):
                raise LookupError("Patient medical visits unavailable")
            rows.append(row)
        for previous, current in zip(rows, rows[1:]):
            wrong_date = (previous.visit_date > current.visit_date if args.status == "upcoming"
                          else previous.visit_date < current.visit_date)
            wrong_tie = previous.visit_date == current.visit_date and (
                previous.created_at < current.created_at
                or (previous.created_at == current.created_at and previous.id.int > current.id.int))
            if wrong_date or wrong_tie:
                raise ValueError("Medical visit query returned invalid ordering")
        return PatientMedicalVisitsOutput(visits=[MedicalVisitItem(
            visit_date=row.visit_date, is_visited=row.is_visited, hospital_name=row.hospital_name,
            department=row.department, visit_reason=row.visit_reason, diagnosis=row.diagnosis,
            treatment_content=row.treatment_content, test_summary=row.test_summary,
            medication_change=row.medication_change, doctor_note=row.doctor_note,
            follow_up_plan=row.follow_up_plan, visit_content=row.visit_content) for row in rows])
