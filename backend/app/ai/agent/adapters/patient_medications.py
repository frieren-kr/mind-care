"""Existing patient ownership lookup and complete medication list projection."""
from collections.abc import Callable
from typing import Literal
from uuid import UUID

from app.user_schemas import MedicationOut, PatientOut

from ..schemas import AgentContext
from ..tools.medications import MedicationItem, PatientMedicationsInput, PatientMedicationsOutput


class PatientMedicationsAdapter:
    """Read-only, without cached request state or Agent-side sorting.

    The two existing DB calls do not share a transaction snapshot. Backend
    authenticates Context; DB integration must assess concurrent ownership
    changes and full-list query performance. Resolve defaults only on use.
    """

    def __init__(self, *, get_patient_fn: Callable[[UUID], PatientOut | None] | None = None,
                 list_medications_fn: Callable[..., list[MedicationOut]] | None = None):
        self.get_patient_fn = get_patient_fn
        self.list_medications_fn = list_medications_fn

    def get_patient_medications(self, *, context: AgentContext,
                                status: Literal["taking", "stopped", "all"],
                                limit: int) -> PatientMedicationsOutput:
        if not isinstance(context.user_id, UUID) or not isinstance(context.patient_id, UUID):
            raise LookupError("Patient medications unavailable")
        args = PatientMedicationsInput(status=status, limit=limit)
        get_patient = self.get_patient_fn
        if get_patient is None:
            from app.db.user_functions import get_patient
        patient = get_patient(context.patient_id)
        if (patient is None or patient.id != context.patient_id
                or patient.caregiver_id != context.user_id):
            raise LookupError("Patient medications unavailable")
        list_medications = self.list_medications_fn
        if list_medications is None:
            from app.db.user_functions import list_medications
        taking = {"taking": True, "stopped": False, "all": None}[args.status]
        raw = list_medications(context.patient_id, is_taking=taking)
        if not isinstance(raw, list):
            raise TypeError("Medication query must return a complete list")
        # Validate every row, including those beyond limit; never hide a bad row
        # by filtering or truncation. Recheck even unchecked/mutated DB models.
        rows = [MedicationOut.model_validate(
            row.model_dump(warnings=False) if isinstance(row, MedicationOut) else row, strict=True)
            for row in raw]
        if any(row.patient_id != context.patient_id
               or (taking is not None and row.is_taking is not taking) for row in rows):
            raise LookupError("Patient medications unavailable")
        return PatientMedicationsOutput(total_count=len(rows), medications=[
            MedicationItem(drug_name=row.drug_name, dosage=row.dosage, frequency=row.frequency,
                is_taking=row.is_taking, start_date=row.start_date, end_date=row.end_date, note=row.note)
            for row in rows[:args.limit]])
