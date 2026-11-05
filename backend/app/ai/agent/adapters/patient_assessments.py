"""Existing assessment queries and scale metadata projected to the Agent Contract."""
from collections.abc import Callable
from math import isfinite
from typing import Literal
from uuid import UUID

from app.user_schemas import AssessmentOut, AssessmentTypeInfo, PatientOut, normalize_assessment_type

from ..schemas import AgentContext
from ..tools.assessments import AssessmentItem, PatientAssessmentsInput, PatientAssessmentsOutput


class PatientAssessmentsAdapter:
    """No SQL, cached patient state, clinical correction or duplicate score rules.

    DB selects latest-per-type or limited history. Ownership and query calls
    do not share a transaction; actual snapshot consistency is a DB integration
    concern. Defaults are resolved on use, not during construction.
    """

    def __init__(self, *, get_patient_fn: Callable[[UUID], PatientOut | None] | None = None,
                 get_latest_assessments_fn: Callable[[UUID], list[AssessmentOut]] | None = None,
                 list_assessments_fn: Callable[..., list[AssessmentOut]] | None = None,
                 list_assessment_types_fn: Callable[[], list[AssessmentTypeInfo]] | None = None):
        self.get_patient_fn = get_patient_fn
        self.get_latest_assessments_fn = get_latest_assessments_fn
        self.list_assessments_fn = list_assessments_fn
        self.list_assessment_types_fn = list_assessment_types_fn

    def get_patient_assessments(self, *, context: AgentContext,
                                mode: Literal["latest_by_type", "history"],
                                assessment_type: str | None, limit: int) -> PatientAssessmentsOutput:
        if not isinstance(context.user_id, UUID) or not isinstance(context.patient_id, UUID):
            raise LookupError("Patient assessments unavailable")
        args = PatientAssessmentsInput(mode=mode, assessment_type=assessment_type, limit=limit)
        get_patient = self.get_patient_fn
        if get_patient is None:
            from app.db.user_functions import get_patient
        patient = get_patient(context.patient_id)
        if (patient is None or patient.id != context.patient_id
                or patient.caregiver_id != context.user_id):
            raise LookupError("Patient assessments unavailable")
        canonical = normalize_assessment_type(args.assessment_type) if args.assessment_type is not None else None
        if args.mode == "latest_by_type":
            get_latest_assessments = self.get_latest_assessments_fn
            if get_latest_assessments is None:
                from app.db.user_functions import get_latest_assessments
            raw = get_latest_assessments(context.patient_id)
        else:
            list_assessments = self.list_assessments_fn
            if list_assessments is None:
                from app.db.user_functions import list_assessments
            raw = list_assessments(context.patient_id, assessment_type=canonical, limit=args.limit)
        if not isinstance(raw, list) or (args.mode == "history" and len(raw) > args.limit):
            raise ValueError("Assessment query returned an invalid list or exceeded limit")
        rows = []
        for row in raw:
            fields = row.model_fields_set if isinstance(row, AssessmentOut) else row.keys() if isinstance(row, dict) else ()
            if not {"assessment_type", "score", "result_detail", "assessed_at", "assessed_by"}.issubset(fields):
                raise ValueError("Assessment query omitted required projection fields")
            row = AssessmentOut.model_validate(
                row.model_dump(warnings=False) if isinstance(row, AssessmentOut) else row, strict=True)
            if row.patient_id != context.patient_id:
                raise LookupError("Patient assessments unavailable")
            if row.score is not None and not isfinite(row.score):
                raise ValueError("Assessment score must be finite")
            if args.mode == "history" and canonical is not None and row.assessment_type != canonical:
                raise LookupError("Patient assessments unavailable")
            rows.append(row)
        if args.mode == "latest_by_type":
            kinds = [row.assessment_type for row in rows]
            if len(set(kinds)) != len(kinds):
                raise ValueError("Latest assessments must have unique types")
            # get_latest_assessments owns name ordering under DB collation.
            # Python string ordering can reject valid SQL name order.
            selected = [row for row in rows if canonical is None or row.assessment_type == canonical][:args.limit]
        else:
            for previous, current in zip(rows, rows[1:]):
                wrong_tie = previous.assessed_at == current.assessed_at and (
                    previous.created_at < current.created_at
                    or (previous.created_at == current.created_at and previous.id.int > current.id.int))
                if previous.assessed_at < current.assessed_at or wrong_tie:
                    raise ValueError("Assessment history returned invalid ordering")
            selected = rows
        if not selected:
            return PatientAssessmentsOutput(assessments=[])
        list_assessment_types = self.list_assessment_types_fn
        if list_assessment_types is None:
            from app.db.user_functions import list_assessment_types
        raw_metadata = list_assessment_types()
        if not isinstance(raw_metadata, list):
            raise ValueError("Assessment metadata must be a list")
        metadata = {}
        for info in raw_metadata:
            info = AssessmentTypeInfo.model_validate(
                info.model_dump(warnings=False) if isinstance(info, AssessmentTypeInfo) else info, strict=True)
            if info.assessment_type in metadata:
                raise ValueError("Duplicate assessment metadata")
            if any(value is not None and not isfinite(value) for value in (info.min, info.max)):
                raise ValueError("Assessment metadata bounds must be finite")
            metadata[info.assessment_type] = info
        items = []
        for row in selected:
            info = metadata.get(row.assessment_type)
            items.append(AssessmentItem(assessment_type=row.assessment_type,
                assessment_name=info.name if info else None, score=row.score,
                score_min=info.min if info else None, score_max=info.max if info else None,
                higher_is_worse=info.higher_is_worse if info else None,
                result_detail=row.result_detail, assessed_at=row.assessed_at, assessed_by=row.assessed_by))
        return PatientAssessmentsOutput(assessments=items)
