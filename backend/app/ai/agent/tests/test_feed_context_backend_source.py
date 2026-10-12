"""Offline development adapter tests; no DB completeness/authentication claims.

CareLog/Summary assertions describe UNREAD placeholders, never actual absence.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import traceback
import unittest
from unittest.mock import Mock
from uuid import UUID

from app.user_schemas import APP_TIMEZONE, CaregiverProfileOut, PatientContext, UserProfileContext
from app.ai.agent.schemas import AgentContext
from app.ai.agent.outputs import StructuredAgentResult
from app.ai.agent.workflows.feed import FeedWorkflow, FeedWorkflowError
from app.ai.agent.workflows.feed_context_backend_source import (
    DevelopmentFeedContextSource, DevelopmentFeedContextSourceError,
    MAX_MANAGED_PATIENTS,
)
from app.ai.agent.workflows.feed_context_loader import DefaultFeedContextLoader, FeedContextLoadError
from app.ai.agent.workflows.feed_context_prepare import prepare_production_feed_input


NOW = datetime(2026, 10, 11, 16, tzinfo=timezone.utc)
TODAY = date(2026, 10, 12)  # KST date, distinct from UTC date.
USER, P1, P2, OTHER = (UUID(int=value) for value in (10, 1, 2, 99))
CONTEXT = AgentContext(request_id="PRIVATE_REQUEST", user_id=USER, patient_id=OTHER)
SELECTED = ("latest_assessments", "recent_safety_events", "current_medications",
            "recent_visits", "upcoming_visits")


def patient(identifier=P1):
    base = identifier.int * 1000
    common = {"patient_id": identifier, "created_at": NOW}
    return PatientContext.model_validate({
        "patient": {"id": identifier, "caregiver_id": USER, "name": "PRIVATE_PATIENT_NAME",
                    "dementia_stage": "stored legacy stage", "diagnosis_date": TODAY,
                    "symptoms": [f"stored symptom {identifier.int}"], "interests": ["stored interest"],
                    "created_at": NOW, "updated_at": NOW},
        "latest_assessments": [{**common, "id": UUID(int=base+1), "assessment_type": "stored scale",
                                "score": 12.3, "result_detail": "stored assessment narrative",
                                "assessed_at": TODAY, "assessed_by": "PRIVATE_ASSESSOR"}],
        "recent_safety_events": [{**common, "id": UUID(int=base+2), "event_date": TODAY,
                                  "has_fall": True, "has_wandering": False, "has_missing": False,
                                  "note": "stored safety narrative"}],
        "current_medications": [{**common, "id": UUID(int=base+3), "drug_name": "stored drug",
                                 "is_taking": True, "start_date": TODAY, "end_date": None,
                                 "note": "stored medication narrative", "dosage": "PRIVATE_DOSAGE",
                                 "frequency": "PRIVATE_FREQUENCY", "updated_at": NOW}],
        "recent_visits": [{**common, "id": UUID(int=base+4), "visit_date": TODAY,
                           "is_visited": True, "department": "stored department", "updated_at": NOW,
                           "hospital_name": "PRIVATE_HOSPITAL", "visit_content": "stored visit narrative",
                           **{field: "PRIVATE_VISIT_DETAIL" for field in (
                               "visit_reason", "diagnosis", "treatment_content", "test_summary",
                               "medication_change", "doctor_note", "follow_up_plan")}}],
        "upcoming_visits": [{**common, "id": UUID(int=base+5), "visit_date": TODAY+timedelta(days=1),
                             "is_visited": False, "department": None, "visit_content": None,
                             "updated_at": NOW}],
    })


def profile(rows=None, *, caregiver=True):
    return UserProfileContext(
        user_id=USER, name="PRIVATE_CAREGIVER_NAME", generated_at=NOW,
        caregiver_profile=CaregiverProfileOut(
            user_id=USER, relationship="stored relationship", burden_score=20, mood_score=4,
            lifestyle_tags=["stored tag"], updated_at=NOW,
        ) if caregiver else None,
        patients=[patient()] if rows is None else rows,
    )


def load(source):
    return source.load_snapshot(user_id=USER, reference_time=NOW, reference_date=TODAY,
                                recent_period_start=NOW-timedelta(days=30), recent_period_end=NOW)


def loader(source):
    return DefaultFeedContextLoader(source=source, clock=lambda: NOW, service_timezone=APP_TIMEZONE)


class DevelopmentFeedContextSourceTests(unittest.TestCase):
    def source(self, value):
        read = Mock(return_value=value)
        return DevelopmentFeedContextSource(read_profile=read), read

    def assert_rejected(self, value):
        source, read = self.source(value)
        with self.assertRaises(DevelopmentFeedContextSourceError) as caught:
            load(source)
        read.assert_called_once_with(USER)
        self.assertEqual(str(caught.exception), "Development Feed source load failed")
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn("PRIVATE", "".join(traceback.format_exception(caught.exception)))

    def test_zero_patients_and_missing_self_check_are_valid_development_cold_start(self):
        source, read = self.source(profile([], caregiver=False))
        prepared = json.loads(prepare_production_feed_input(CONTEXT, loader(source)))
        self.assertEqual(prepared, {"feed_scope": "caregiver_all", "caregiver": None, "patients": []})
        read.assert_called_once_with(USER)

    def test_one_patient_explicit_projection_preserves_stored_data_and_decimal(self):
        original = profile()
        before = original.model_dump()
        source, read = self.source(original)
        snapshot = load(source)
        read.assert_called_once_with(USER)
        caregiver = snapshot.caregiver_profile
        self.assertEqual(caregiver.user_id, USER)
        self.assertEqual((caregiver.relationship, caregiver.burden_score, caregiver.mood_score,
                          caregiver.lifestyle_tags, caregiver.updated_at),
                         ("stored relationship", 20, 4, ["stored tag"], NOW))
        row = snapshot.managed_patients.items[0]
        self.assertEqual((row.patient_id, row.caregiver_id), (P1, USER))
        self.assertEqual(row.diagnosis_date, TODAY)
        self.assertEqual(row.dementia_stage, "stored legacy stage")
        self.assertEqual(row.clinical_assessments.items[0].score, Decimal("12.3"))
        self.assertEqual(row.clinical_assessments.items[0].result_detail, "stored assessment narrative")
        self.assertTrue(row.safety_events.items[0].has_fall)
        self.assertEqual(row.safety_events.items[0].note, "stored safety narrative")
        self.assertEqual(row.medications.items[0].drug_name, "stored drug")
        self.assertTrue(row.medications.items[0].is_taking)
        self.assertEqual(row.medications.items[0].note, "stored medication narrative")
        self.assertEqual([v.is_visited for v in row.medical_visits.items], [False, True])
        self.assertEqual(row.medical_visits.items[1].visit_content, "stored visit narrative")
        source_json = snapshot.model_dump_json()
        for forbidden in ("PRIVATE_PATIENT_NAME", "PRIVATE_CAREGIVER_NAME", "PRIVATE_ASSESSOR",
                          "PRIVATE_DOSAGE", "PRIVATE_FREQUENCY", "PRIVATE_HOSPITAL", "PRIVATE_VISIT_DETAIL"):
            self.assertNotIn(forbidden, source_json)
        self.assertEqual(original.model_dump(), before)
        row.symptoms.append("isolated source change")
        self.assertEqual(original.model_dump(), before)

    def test_all_patients_and_child_provenance_survive_any_context_patient_id(self):
        data = profile([patient(P2), patient(P1)])
        for index, row in enumerate(data.patients):
            row.latest_assessments[0].assessment_type = f"patient scale {index}"
            row.current_medications[0].drug_name = f"patient drug {index}"
            row.recent_safety_events[0].note = f"patient safety {index}"
            row.recent_visits[0].visit_content = f"patient visit {index}"
        for selected in (None, P1, P2, OTHER):
            with self.subTest(patient_id=selected):
                source, read = self.source(data)
                value = loader(source)(CONTEXT.model_copy(update={"patient_id": selected}), recent_days=30)
                read.assert_called_once_with(USER)
                self.assertEqual(value.managed_patient_profiles.coverage.total_count, 2)
                first, second = value.managed_patient_profiles.items
                self.assertEqual([first.patient_ref, second.patient_ref], ["patient_1", "patient_2"])
                for row, index in ((first, 1), (second, 0)):
                    self.assertEqual(row.clinical_assessments.items[0].assessment_type, f"patient scale {index}")
                    self.assertEqual(row.medications.items[0].drug_name, f"patient drug {index}")
                    self.assertEqual(row.safety_events.items[0].note, f"patient safety {index}")
                    self.assertEqual(row.medical_visits.items[1].visit_content, f"patient visit {index}")

    def test_provider_input_keeps_only_existing_allowlist_for_all_patients(self):
        source, _ = self.source(profile([patient(P2), patient(P1)]))
        text = prepare_production_feed_input(CONTEXT, loader(source))
        payload = json.loads(text)
        self.assertEqual(set(payload), {"feed_scope", "caregiver", "patients"})
        self.assertEqual(payload["feed_scope"], "caregiver_all")
        self.assertEqual(set(payload["caregiver"]), {"relationship", "lifestyle_tags"})
        self.assertEqual(len(payload["patients"]), 2)
        self.assertEqual(payload["patients"][0]["symptoms"], ["stored symptom 1"])
        for row in payload["patients"]:
            self.assertEqual(set(row), {"patient_ref", "dementia_stage", "symptoms", "interests",
                                        "safety_events", "assessment_types"})
            self.assertEqual(row["safety_events"], ["fall"])
        for forbidden in (str(USER), str(P1), str(P2), str(OTHER), CONTEXT.request_id,
                          "PRIVATE_", "narrative", "score", "coverage", "care_logs", "long_term_summary"):
            self.assertNotIn(forbidden, text)

    def test_unread_placeholders_and_selected_counts_are_development_only(self):
        source, read = self.source(profile())
        snapshot = load(source)
        read.assert_called_once_with(USER)  # No pretend CareLog/Summary reads.
        self.assertEqual(snapshot.recent_care_logs.items, [])
        self.assertEqual(snapshot.recent_care_logs.total_count, 0)
        self.assertIsNone(snapshot.long_term_summary)
        self.assertIn("UNREAD placeholders", DevelopmentFeedContextSource.__doc__)
        self.assertIn("NOT Production", DevelopmentFeedContextSource.__doc__)
        value = loader(source)(CONTEXT, recent_days=30)
        for collection in (snapshot.managed_patients,
                           *[getattr(snapshot.managed_patients.items[0], field) for field in (
                               "clinical_assessments", "safety_events", "medications", "medical_visits")]):
            self.assertEqual(collection.total_count, len(collection.items))
        self.assertFalse(value.managed_patient_profiles.items[0].medical_visits.coverage.is_truncated)
        self.assertEqual(value.recent_care_context.period_start, NOW-timedelta(days=30))
        self.assertEqual(value.recent_care_context.period_end, NOW)

    def test_none_and_wrong_backend_dto_fail(self):
        for data in (None, {}, profile().model_dump()):
            with self.subTest(kind=type(data).__name__):
                self.assert_rejected(data)

    def test_read_exception_propagates_as_failure_and_loader_never_returns_empty(self):
        source = DevelopmentFeedContextSource(read_profile=Mock(side_effect=RuntimeError("PRIVATE DB failure")))
        with self.assertRaises(DevelopmentFeedContextSourceError):
            load(source)
        with self.assertRaises(FeedContextLoadError) as caught:
            prepare_production_feed_input(CONTEXT, loader(source))
        self.assertEqual(caught.exception.code, "source_load_failed")

    def test_user_caregiver_and_patient_owner_mismatch_fail(self):
        for mutate in (lambda p: setattr(p, "user_id", OTHER),
                       lambda p: setattr(p.caregiver_profile, "user_id", OTHER),
                       lambda p: setattr(p.patients[0].patient, "caregiver_id", OTHER)):
            data = profile(); mutate(data)
            self.assert_rejected(data)

    def test_child_owner_mismatch_fails_for_each_selected_list(self):
        for field in SELECTED:
            with self.subTest(field=field):
                data = profile()
                getattr(data.patients[0], field)[0].patient_id = OTHER
                self.assert_rejected(data)

    def test_duplicate_patient_ids_fail_without_deduplication(self):
        data = profile([patient(), patient()])
        # Use otherwise disjoint child IDs to exercise Loader's patient identity check.
        for field in SELECTED:
            getattr(data.patients[1], field)[0].id = UUID(int=getattr(data.patients[1], field)[0].id.int+100)
        self.assert_rejected(data)

    def test_duplicate_record_ids_fail_within_lists_and_across_patients(self):
        for field in SELECTED:
            with self.subTest(field=field):
                data = profile()
                getattr(data.patients[0], field).append(deepcopy(getattr(data.patients[0], field)[0]))
                self.assert_rejected(data)
                data = profile([patient(P1), patient(P2)])
                getattr(data.patients[1], field)[0].id = getattr(data.patients[0], field)[0].id
                self.assert_rejected(data)

    def test_visit_id_overlap_between_completed_and_upcoming_fails(self):
        data = profile()
        data.patients[0].upcoming_visits[0].id = data.patients[0].recent_visits[0].id
        self.assert_rejected(data)

    def test_omitted_backend_reads_do_not_become_successful_empty_lists(self):
        self.assert_rejected(UserProfileContext(user_id=USER, name="PRIVATE", generated_at=NOW))
        for field in ("caregiver_profile", "patients"):
            data = profile(); data.__pydantic_fields_set__.discard(field)
            self.assert_rejected(data)
        for field in SELECTED:
            data = profile(); data.patients[0].__pydantic_fields_set__.discard(field)
            self.assert_rejected(data)

    def test_mutated_or_constructed_invalid_backend_values_fail(self):
        mutations = (
            lambda p: setattr(p.patients[0].patient, "diagnosis_date", "not-a-date"),
            lambda p: setattr(p.patients[0].patient, "updated_at", NOW.replace(tzinfo=None)),
            lambda p: setattr(p.patients[0].current_medications[0], "is_taking", "true"),
            lambda p: setattr(p.patients[0].latest_assessments[0], "score", float("nan")),
            lambda p: setattr(p.patients[0].recent_visits[0], "created_at", NOW.replace(tzinfo=None)),
            lambda p: setattr(p.patients[0].recent_safety_events[0], "event_date", "2026-10-12"),
        )
        for mutate in mutations:
            data = profile(); mutate(data)
            self.assert_rejected(data)
        data = profile()
        row = data.patients[0].latest_assessments[0]
        data.patients[0].latest_assessments = [type(row).model_construct(
            **{k: v for k, v in row.model_dump().items() if k != "assessed_at"})]
        self.assert_rejected(data)

    def test_current_selection_errors_fail_without_repair_or_filter(self):
        changes = (
            lambda row: setattr(row.current_medications[0], "is_taking", False),
            lambda row: setattr(row.recent_safety_events[0], "has_fall", False),
            lambda row: setattr(row.recent_visits[0], "is_visited", False),
            lambda row: setattr(row.upcoming_visits[0], "is_visited", True),
            lambda row: setattr(row.upcoming_visits[0], "visit_date", TODAY-timedelta(days=1)),
        )
        for mutate in changes:
            data = profile(); mutate(data.patients[0])
            self.assert_rejected(data)

    def test_temporal_failures_still_belong_to_unchanged_loader_validation(self):
        changes = (
            lambda row: setattr(row.patient, "diagnosis_date", TODAY+timedelta(days=1)),
            lambda row: setattr(row.latest_assessments[0], "assessed_at", TODAY+timedelta(days=1)),
            lambda row: setattr(row.recent_safety_events[0], "event_date", TODAY+timedelta(days=1)),
            lambda row: setattr(row.recent_visits[0], "visit_date", TODAY+timedelta(days=1)),
            lambda row: setattr(row.current_medications[0], "end_date", TODAY-timedelta(days=1)),
        )
        for mutate in changes:
            data = profile(); mutate(data.patients[0])
            source, _ = self.source(data)
            load(source)  # Source shapes are valid; full Context owns these rules.
            with self.assertRaises(FeedContextLoadError) as caught:
                prepare_production_feed_input(CONTEXT, loader(source))
            self.assertEqual(caught.exception.code, "context_validation_failed")

    def test_null_assessment_score_is_preserved(self):
        data = profile(); data.patients[0].latest_assessments[0].score = None
        source, _ = self.source(data)
        self.assertIsNone(load(source).managed_patients.items[0].clinical_assessments.items[0].score)

    def test_visits_sort_by_date_created_at_and_id_without_mutating_backend(self):
        data = profile()
        base = data.patients[0].upcoming_visits[0]
        a = base.model_copy(update={"id": UUID(int=701), "visit_content": "first tied ID"})
        b = base.model_copy(update={"id": UUID(int=702), "visit_content": "second tied ID"})
        c = base.model_copy(update={"id": UUID(int=703), "created_at": NOW+timedelta(seconds=1),
                                    "visit_content": "newer created"})
        d = base.model_copy(update={"id": UUID(int=704), "visit_date": TODAY+timedelta(days=2),
                                    "visit_content": "newest visit date"})
        data.patients[0].upcoming_visits = [b, a, d, c]
        before = data.model_dump()
        source, _ = self.source(data)
        visits = load(source).managed_patients.items[0].medical_visits.items
        self.assertEqual([v.visit_content for v in visits], [
            "newest visit date", "newer created", "first tied ID", "second tied ID", "stored visit narrative"])
        self.assertEqual(data.model_dump(), before)

    def test_medications_null_start_dates_sort_last_then_created_at_and_id(self):
        data = profile()
        base = data.patients[0].current_medications[0]
        data.patients[0].current_medications = [
            base.model_copy(update={"id": UUID(int=800), "start_date": None, "drug_name": "null start"}),
            base.model_copy(update={"id": UUID(int=802), "drug_name": "second tied ID"}),
            base.model_copy(update={"id": UUID(int=801), "drug_name": "first tied ID"}),
            base.model_copy(update={"id": UUID(int=803), "created_at": NOW+timedelta(seconds=1),
                                    "drug_name": "newer created"}),
        ]
        source, _ = self.source(data)
        self.assertEqual([m.drug_name for m in load(source).managed_patients.items[0].medications.items],
                         ["newer created", "first tied ID", "second tied ID", "null start"])

    def test_invalid_reader_is_rejected(self):
        with self.assertRaises(DevelopmentFeedContextSourceError):
            DevelopmentFeedContextSource(read_profile=None)

    def test_explicit_empty_child_lists_preserve_managed_patient(self):
        row = patient()
        for field in SELECTED:
            setattr(row, field, [])
        source, _ = self.source(profile([row], caregiver=False))
        value = loader(source)(CONTEXT, recent_days=30)
        self.assertEqual(len(value.managed_patient_profiles.items), 1)
        for field in ("clinical_assessments", "safety_events", "medications", "medical_visits"):
            collection = getattr(value.managed_patient_profiles.items[0], field)
            self.assertEqual(collection.items, [])
            self.assertEqual(collection.coverage.total_count, 0)

    def test_selected_assessment_type_and_safety_date_conflicts_fail(self):
        for field in ("latest_assessments", "recent_safety_events"):
            data = profile()
            records = getattr(data.patients[0], field)
            records.append(records[0].model_copy(update={"id": UUID(int=909)}))
            self.assert_rejected(data)

    def test_latest_assessment_selection_is_preserved_but_source_order_is_date_first(self):
        data = profile()
        base = data.patients[0].latest_assessments[0]
        data.patients[0].latest_assessments = [
            base.model_copy(update={"id": UUID(int=901), "assessment_type": "older-date",
                                    "assessed_at": TODAY-timedelta(days=2)}),
            base.model_copy(update={"id": UUID(int=903), "assessment_type": "same-date-larger-ID"}),
            base.model_copy(update={"id": UUID(int=902), "assessment_type": "same-date-smaller-ID"}),
            base.model_copy(update={"id": UUID(int=904), "assessment_type": "same-date-newer-created",
                                    "created_at": NOW+timedelta(seconds=1)}),
        ]
        source, _ = self.source(data)
        rows = load(source).managed_patients.items[0].clinical_assessments
        self.assertEqual(rows.total_count, 4)  # Selected latest types, not assessment history count.
        self.assertEqual([row.assessment_type for row in rows.items], [
            "same-date-newer-created", "same-date-smaller-ID", "same-date-larger-ID", "older-date"])

    def test_safety_events_follow_date_order_without_clinical_reinterpretation(self):
        data = profile()
        base = data.patients[0].recent_safety_events[0]
        data.patients[0].recent_safety_events = [
            base.model_copy(update={"id": UUID(int=909), "event_date": TODAY-timedelta(days=1),
                                    "has_fall": False, "has_missing": True}), base,
        ]
        source, _ = self.source(data)
        rows = load(source).managed_patients.items[0].safety_events.items
        self.assertEqual([row.event_date for row in rows], [TODAY, TODAY-timedelta(days=1)])
        self.assertTrue(rows[0].has_fall)
        self.assertTrue(rows[1].has_missing)

    def test_stage_one_composes_with_existing_three_stage_workflow(self):
        source, read = self.source(profile([patient(P2), patient(P1)]))
        context_loader = loader(source)
        events = []
        def prepare(context):
            events.append("context_prepare")
            return prepare_production_feed_input(context, context_loader)
        def execute(context, prepared):
            events.append("feed_execution")
            self.assertEqual(json.loads(prepared)["feed_scope"], "caregiver_all")
            self.assertEqual(len(json.loads(prepared)["patients"]), 2)
            return StructuredAgentResult(
                execution={"status": "completed", "request_id": context.request_id,
                           "final_answer": None, "tool_rounds": 0, "total_tool_calls": 0,
                           "called_tools": [], "errors": []},
                output={"schema_version": "1", "response_type": "feed", "items": []}, sources=[],
            )
        receipt = object()
        def handoff(context, result):
            events.append("backend_handoff")
            self.assertEqual(result.execution.request_id, context.request_id)
            return receipt
        workflow = FeedWorkflow(context_prepare=prepare, feed_execution=execute, backend_handoff=handoff)
        self.assertIs(workflow.run(CONTEXT), receipt)
        self.assertEqual(events, ["context_prepare", "feed_execution", "backend_handoff"])
        read.assert_called_once_with(USER)

    def test_source_failure_stops_workflow_before_execution_and_handoff(self):
        source, _ = self.source(None)
        context_loader = loader(source)
        execution, handoff = Mock(), Mock()
        workflow = FeedWorkflow(
            context_prepare=lambda context: prepare_production_feed_input(context, context_loader),
            feed_execution=execution, backend_handoff=handoff,
        )
        with self.assertRaises(FeedWorkflowError) as caught:
            workflow.run(CONTEXT)
        self.assertEqual(caught.exception.stage, "context_prepare")
        execution.assert_not_called()
        handoff.assert_not_called()

    def test_twenty_patients_preserve_full_snapshot_refs_and_provider_scope(self):
        self.assertEqual(MAX_MANAGED_PATIENTS, 20)
        rows = [patient(UUID(int=index)) for index in range(20, 0, -1)]
        data = profile(rows)
        before = data.model_dump()
        source, read = self.source(data)
        snapshot = load(source)
        read.assert_called_once_with(USER)
        self.assertEqual(snapshot.managed_patients.total_count, 20)
        self.assertEqual([p.patient_id for p in snapshot.managed_patients.items],
                         [row.patient.id for row in rows])
        for selected in (None, P1, UUID(int=20), OTHER):
            with self.subTest(patient_id=selected):
                source, read = self.source(data)
                context = CONTEXT.model_copy(update={"patient_id": selected})
                text = prepare_production_feed_input(context, loader(source))
                read.assert_called_once_with(USER)
                payload = json.loads(text)
                self.assertEqual(payload["feed_scope"], "caregiver_all")
                self.assertEqual([p["patient_ref"] for p in payload["patients"]],
                                 [f"patient_{index}" for index in range(1, 21)])
                self.assertEqual([p["symptoms"] for p in payload["patients"]],
                                 [[f"stored symptom {index}"] for index in range(1, 21)])
        self.assertEqual(data.model_dump(), before)

    def test_twenty_one_patients_fail_without_partial_source_or_context(self):
        data = profile([patient(UUID(int=index)) for index in range(1, 22)])
        before = data.model_dump()
        self.assert_rejected(data)
        for selected in (None, P1, UUID(int=21), OTHER):
            with self.subTest(patient_id=selected):
                source, read = self.source(data)
                context = CONTEXT.model_copy(update={"patient_id": selected})
                with self.assertRaises(FeedContextLoadError) as caught:
                    prepare_production_feed_input(context, loader(source))
                self.assertEqual(caught.exception.code, "source_load_failed")
                read.assert_called_once_with(USER)
        self.assertEqual(data.model_dump(), before)

    def test_over_patient_cap_stops_workflow_before_execution_and_handoff(self):
        source, read = self.source(profile([patient(UUID(int=index)) for index in range(1, 22)]))
        context_loader = loader(source)
        execution, handoff = Mock(), Mock()
        workflow = FeedWorkflow(
            context_prepare=lambda context: prepare_production_feed_input(context, context_loader),
            feed_execution=execution, backend_handoff=handoff,
        )
        with self.assertRaises(FeedWorkflowError) as caught:
            workflow.run(CONTEXT)
        self.assertEqual(caught.exception.stage, "context_prepare")
        read.assert_called_once_with(USER)
        execution.assert_not_called()
        handoff.assert_not_called()

    def test_development_does_not_reselect_records_using_future_period_or_cap(self):
        data = profile()
        base = data.patients[0].latest_assessments[0]
        data.patients[0].latest_assessments = [base.model_copy(update={
            "id": UUID(int=9000+index), "assessment_type": f"stored legacy scale {index}",
            "assessed_at": TODAY-timedelta(days=366),
        }) for index in range(25)]
        source, read = self.source(data)
        value = loader(source)(CONTEXT, recent_days=30)
        read.assert_called_once_with(USER)
        records = value.managed_patient_profiles.items[0].clinical_assessments
        self.assertEqual(len(records.items), 25)  # No future 365-day/20-record filter here.
        self.assertEqual(records.coverage.total_count, 25)  # Temporary selected-list count.
        self.assertEqual(records.coverage.included_count, 25)
        self.assertFalse(records.coverage.is_truncated)
        self.assertTrue(all(row.assessed_at == TODAY-timedelta(days=366) for row in records.items))

    def test_backend_null_arrays_normalized_to_lists_remain_valid(self):
        data = profile()
        row = data.patients[0]
        row.patient = type(row.patient).model_validate({
            **row.patient.model_dump(), "symptoms": None, "interests": None,
        })
        data.caregiver_profile = CaregiverProfileOut.model_validate({
            **data.caregiver_profile.model_dump(), "lifestyle_tags": None,
        })
        source, read = self.source(data)
        payload = json.loads(prepare_production_feed_input(CONTEXT, loader(source)))
        read.assert_called_once_with(USER)
        self.assertEqual(payload["patients"][0]["symptoms"], [])
        self.assertEqual(payload["patients"][0]["interests"], [])
        self.assertEqual(payload["caregiver"]["lifestyle_tags"], [])
