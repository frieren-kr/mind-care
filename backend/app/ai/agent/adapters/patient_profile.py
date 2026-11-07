"""One existing patient lookup, ownership check and explicit Agent projection."""
from collections.abc import Callable
from uuid import UUID

from app.user_schemas import PatientOut

from ..schemas import AgentContext
from ..tools.contracts import PatientProfileOutput


class PatientProfileAdapter:
    """Read-only, with no per-request cache or mutable patient state.

    Backend supplies authenticated Context. This check verifies the returned
    patient's ownership; it does not authenticate the user. Resolve the real
    get_patient only when called, so construction performs no DB access.
    """

    def __init__(self, *, get_patient_fn: Callable[[UUID], PatientOut | None] | None = None):
        self.get_patient_fn = get_patient_fn

    def get_patient_profile(self, *, context: AgentContext) -> PatientProfileOutput:
        if not isinstance(context.patient_id, UUID) or not isinstance(context.user_id, UUID):
            raise LookupError("Patient profile unavailable")
        get_patient = self.get_patient_fn
        if get_patient is None:
            from app.db.user_functions import get_patient

        patient = get_patient(context.patient_id)
        if (patient is None or patient.id != context.patient_id
                or patient.caregiver_id != context.user_id):
            raise LookupError("Patient profile unavailable")
        # Existing Output validates the five values and copies the lists.
        # Never dump the DB row: identifiers and timestamps remain server-side.
        return PatientProfileOutput(name=patient.name, dementia_stage=patient.dementia_stage,
            diagnosis_date=patient.diagnosis_date, symptoms=patient.symptoms, interests=patient.interests)
