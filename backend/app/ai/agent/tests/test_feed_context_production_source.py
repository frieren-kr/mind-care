"""Production Source/Stage 1 offline tests against real Backend DTOs/function.

Mock DB I/O in the real Backend function test; do not claim live DB integration.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import inspect
import json
import traceback
import unittest
from unittest.mock import MagicMock, Mock, patch
from uuid import UUID

from psycopg import IsolationLevel

from app.db import feed_read
from app.feed_schemas import FeedReadSnapshot
from app.user_schemas import APP_TIMEZONE
from app.ai.agent.outputs import StructuredAgentResult
from app.ai.agent.schemas import AgentContext
from app.ai.agent.workflows.feed import FeedWorkflow, FeedWorkflowError
from app.ai.agent.workflows.feed_context import FeedPersonalizationContextV1
from app.ai.agent.workflows.feed_context_backend_source import (
    ProductionFeedContextSource, ProductionFeedContextSourceError,
)
from app.ai.agent.workflows.feed_context_loader import DefaultFeedContextLoader, FeedContextLoadError
from app.ai.agent.workflows.feed_context_prepare import prepare_production_feed_input
from app.ai.agent.workflows import feed_context_prepare as prepare_module
from app.ai.agent.workflows.feed_context_source import FeedContextSource


NOW = datetime(2026, 10, 11, 16, tzinfo=timezone.utc)
TODAY = date(2026, 10, 12)
USER, P1, P2, OTHER = (UUID(int=value) for value in (10, 1, 2, 99))
CONTEXT = AgentContext(request_id="PRIVATE_REQUEST", user_id=USER, patient_id=OTHER)
ARGS = dict(user_id=USER, reference_time=NOW, reference_date=TODAY,
            recent_period_start=NOW-timedelta(days=30), recent_period_end=NOW)
CHILDREN = ("clinical_assessments", "safety_events", "medications", "medical_visits")


def collection(items=(), *, total=None):
    rows = list(items)
    return {"items": rows, "total_count": len(rows) if total is None else total}


def patient(identifier=P1, *, full=True):
    row = {"patient_id": identifier, "caregiver_id": USER, "dementia_stage": "stored stage",
           "diagnosis_date": TODAY-timedelta(days=10), "symptoms": [f"signal {identifier.int}"],
           "interests": ["stored interest"], "updated_at": NOW,
           **{field: collection() for field in CHILDREN}}
    if not full:
        return row
    row["clinical_assessments"] = collection([
        {"patient_id": identifier, "assessment_type": "scale B", "score": Decimal("12.30"),
         "result_detail": "PRIVATE_ASSESSMENT", "assessed_at": TODAY-timedelta(days=2)},
        {"patient_id": identifier, "assessment_type": "scale A", "score": None,
         "result_detail": None, "assessed_at": TODAY-timedelta(days=1)},
    ], total=7)
    row["safety_events"] = collection([
        {"patient_id": identifier, "event_date": TODAY-timedelta(days=2), "has_fall": True,
         "has_wandering": False, "has_missing": False, "note": "PRIVATE_SAFETY"},
        {"patient_id": identifier, "event_date": TODAY-timedelta(days=1), "has_fall": False,
         "has_wandering": True, "has_missing": False, "note": None},
    ], total=9)
    row["medications"] = collection([
        {"patient_id": identifier, "drug_name": "stored stopped drug", "is_taking": False,
         "start_date": TODAY-timedelta(days=100), "end_date": TODAY-timedelta(days=2),
         "note": "PRIVATE_MEDICATION", "dosage": "PRIVATE_DOSAGE", "frequency": "PRIVATE_FREQUENCY"},
        {"patient_id": identifier, "drug_name": "stored current drug", "is_taking": True,
         "start_date": None, "end_date": None, "note": None, "dosage": None, "frequency": None},
    ], total=8)
    row["medical_visits"] = collection([
        {"patient_id": identifier, "visit_date": TODAY-timedelta(days=3), "is_visited": True,
         "department": "stored department", "visit_content": "PRIVATE_VISIT_MEMO",
         **{field: "PRIVATE_VISIT_DETAIL" for field in (
             "visit_reason", "diagnosis", "treatment_content", "test_summary",
             "medication_change", "doctor_note", "follow_up_plan")}},
        {"patient_id": identifier, "visit_date": TODAY+timedelta(days=2), "is_visited": False,
         "department": None, "visit_content": None},
    ], total=12)
    return row


def snapshot(rows=None, *, full=True):
    rows = [patient()] if rows is None else rows
    return FeedReadSnapshot.model_validate({
        "caregiver_profile": {"user_id": USER, "relationship": "stored relationship",
                              "burden_score": 20, "mood_score": 4, "lifestyle_tags": ["stored tag"],
                              "updated_at": NOW} if full else None,
        "managed_patients": collection(rows),
        "recent_care_logs": collection([
            {"user_id": USER, "patient_id": None, "log_type": "stored unlinked type",
             "content": "PRIVATE_NULL_LOG", "mood_tag": "stored mood", "logged_at": NOW-timedelta(days=2)},
            {"user_id": USER, "patient_id": rows[0]["patient_id"], "log_type": "patient_care",
             "content": "PRIVATE_PATIENT_LOG", "mood_tag": None, "logged_at": NOW-timedelta(days=1)},
        ], total=11) if full and rows else collection(),
        "long_term_summary": {"user_id": USER, "period_start": date(2026, 9, 1),
                              "period_end": date(2026, 9, 30), "summary": "PRIVATE_MONTHLY_SUMMARY",
                              "updated_tags": ["stored summary tag"], "generated_at": NOW-timedelta(days=1)}
        if full else None,
    })


def context_loader(source, *, clock=lambda: NOW):
    return DefaultFeedContextLoader(source=source, clock=clock, service_timezone=APP_TIMEZONE)


class ProductionSourceTests(unittest.TestCase):
    def source(self, value):
        read = Mock(return_value=value)
        return ProductionFeedContextSource(read_snapshot=read), read

    def rejected(self, value):
        source, read = self.source(value)
        with self.assertRaises(ProductionFeedContextSourceError) as caught:
            source.load_snapshot(**ARGS)
        read.assert_called_once_with(**ARGS)
        self.assertEqual(str(caught.exception), "Production Feed source validation failed")
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn("PRIVATE", "".join(traceback.format_exception(caught.exception)))

    def test_protocol_and_exactly_one_read_with_five_unchanged_keyword_values(self):
        self.assertEqual(inspect.signature(ProductionFeedContextSource.load_snapshot),
                         inspect.signature(FeedContextSource.load_snapshot))
        source, read = self.source(snapshot())
        source.load_snapshot(**ARGS)
        read.assert_called_once_with(**ARGS)
        self.assertEqual(read.call_args.args, ())
        for key, value in ARGS.items():
            self.assertIs(read.call_args.kwargs[key], value)

    def test_zero_patients_is_successful_cold_start_without_caregiver(self):
        source, read = self.source(snapshot([], full=False))
        self.assertEqual(json.loads(prepare_production_feed_input(CONTEXT, context_loader(source))),
                         {"feed_scope": "caregiver_all", "caregiver": None, "patients": []})
        read.assert_called_once_with(**ARGS)

    def test_one_patient_source_preserves_all_counts_content_and_decimal(self):
        raw = snapshot()
        source, _ = self.source(raw)
        result = source.load_snapshot(**ARGS)
        row = result.managed_patients.items[0]
        self.assertEqual((row.patient_id, row.caregiver_id), (P1, USER))
        self.assertEqual(result.caregiver_profile.burden_score, 20)
        self.assertEqual([getattr(row, field).total_count for field in CHILDREN], [7, 9, 8, 12])
        self.assertEqual(result.managed_patients.total_count, 1)
        self.assertEqual(result.recent_care_logs.total_count, 11)
        self.assertEqual(result.recent_care_logs.items[0].content, "PRIVATE_NULL_LOG")
        self.assertIsNone(result.recent_care_logs.items[0].patient_id)
        self.assertEqual(result.long_term_summary.summary, "PRIVATE_MONTHLY_SUMMARY")
        score = row.clinical_assessments.items[0].score
        self.assertIsInstance(score, Decimal)
        self.assertEqual(score.as_tuple(), Decimal("12.30").as_tuple())

    def test_backend_return_order_is_preserved_even_when_different_from_source_baseline(self):
        raw = snapshot([patient(P2), patient(P1)])
        source, _ = self.source(raw)
        result = source.load_snapshot(**ARGS)
        self.assertEqual([p.patient_id for p in result.managed_patients.items], [P2, P1])
        for backend, mapped in zip(raw.managed_patients.items, result.managed_patients.items):
            self.assertEqual([r.assessment_type for r in mapped.clinical_assessments.items],
                             [r.assessment_type for r in backend.clinical_assessments.items])
            self.assertEqual([r.event_date for r in mapped.safety_events.items],
                             [r.event_date for r in backend.safety_events.items])
            self.assertEqual([r.drug_name for r in mapped.medications.items],
                             [r.drug_name for r in backend.medications.items])
            self.assertEqual([r.visit_date for r in mapped.medical_visits.items],
                             [r.visit_date for r in backend.medical_visits.items])
        self.assertEqual([r.logged_at for r in result.recent_care_logs.items],
                         [r.logged_at for r in raw.recent_care_logs.items])

    def test_decimal_precision_never_passes_through_float(self):
        raw = snapshot()
        expected = Decimal("0.123456789012345678901234567890")
        raw.managed_patients.items[0].clinical_assessments.items[0].score = expected
        source, _ = self.source(raw)
        actual = source.load_snapshot(**ARGS).managed_patients.items[0].clinical_assessments.items[0].score
        self.assertEqual(actual.as_tuple(), expected.as_tuple())

    def test_medication_and_visit_backend_only_fields_are_excluded(self):
        source, _ = self.source(snapshot())
        row = source.load_snapshot(**ARGS).managed_patients.items[0]
        self.assertEqual(set(row.medications.items[0].model_dump()),
                         {"patient_id", "drug_name", "is_taking", "start_date", "end_date", "note"})
        self.assertEqual(set(row.medical_visits.items[0].model_dump()),
                         {"patient_id", "visit_date", "is_visited", "department", "visit_content"})
        self.assertEqual(row.medical_visits.items[0].visit_content, "PRIVATE_VISIT_MEMO")
        self.assertNotIn("PRIVATE_VISIT_DETAIL", row.model_dump_json())
        self.assertNotIn("PRIVATE_DOSAGE", row.model_dump_json())
        self.assertNotIn("PRIVATE_FREQUENCY", row.model_dump_json())

    def test_twenty_patients_preserved_regardless_of_agent_patient_scope(self):
        raw = snapshot([patient(UUID(int=index), full=False) for index in range(20, 0, -1)], full=False)
        for selected in (None, P1, UUID(int=20), OTHER):
            source, read = self.source(raw)
            value = context_loader(source)(CONTEXT.model_copy(update={"patient_id": selected}), recent_days=30)
            read.assert_called_once_with(**ARGS)
            self.assertEqual(value.managed_patient_profiles.coverage.total_count, 20)
            self.assertEqual([p.patient_ref for p in value.managed_patient_profiles.items],
                             [f"patient_{i}" for i in range(1, 21)])
            self.assertEqual([p.symptoms for p in value.managed_patient_profiles.items],
                             [[f"signal {i}"] for i in range(1, 21)])

    def test_partial_managed_patient_list_and_returned_twenty_one_patients_fail(self):
        raw = snapshot(); raw.managed_patients.total_count = 2
        self.rejected(raw)
        self.rejected(snapshot([patient(UUID(int=i), full=False) for i in range(1, 22)], full=False))

    def test_duplicate_managed_patient_identity_fails(self):
        raw = snapshot(); raw.managed_patients.items.append(deepcopy(raw.managed_patients.items[0]))
        raw.managed_patients.total_count = 2
        self.rejected(raw)

    def test_backend_exception_types_and_instances_are_preserved(self):
        for error in (feed_read.FeedUserNotFound(USER), feed_read.FeedReadError("PRIVATE_DB_ERROR"),
                      feed_read.FeedPatientLimitExceeded(USER, 21)):
            read = Mock(side_effect=error)
            source = ProductionFeedContextSource(read_snapshot=read)
            with self.assertRaises(type(error)) as caught:
                source.load_snapshot(**ARGS)
            self.assertIs(caught.exception, error)
            read.assert_called_once_with(**ARGS)
            with self.assertRaises(FeedContextLoadError) as caught:
                prepare_production_feed_input(CONTEXT, context_loader(source))
            self.assertEqual(caught.exception.code, "source_load_failed")
            self.assertNotIn("PRIVATE", str(caught.exception))

    def test_none_dict_and_wrong_backend_dto_fail(self):
        for value in (None, {}, snapshot().model_dump(), object()):
            self.rejected(value)

    def test_mutated_nested_types_and_counts_are_strictly_revalidated(self):
        mutations = (
            lambda r: setattr(r.managed_patients, "total_count", True),
            lambda r: setattr(r.managed_patients.items[0].clinical_assessments, "total_count", 0),
            lambda r: setattr(r.managed_patients.items[0].clinical_assessments.items[0], "score", 12.3),
            lambda r: setattr(r.managed_patients.items[0].medications.items[0], "is_taking", "false"),
            lambda r: setattr(r.managed_patients.items[0], "patient_id", str(P1)),
            lambda r: setattr(r.managed_patients.items[0].medical_visits.items[0], "visit_date", "2026-10-09"),
            lambda r: setattr(r.long_term_summary, "generated_at", NOW.replace(tzinfo=None)),
        )
        for mutate in mutations:
            raw = snapshot(); mutate(raw)
            self.rejected(raw)

    def test_constructed_missing_required_nested_fields_fail(self):
        raw = snapshot()
        collection_type = type(raw.managed_patients.items[0].clinical_assessments)
        raw.managed_patients.items[0].clinical_assessments = collection_type.model_construct(items=[])
        self.rejected(raw)
        raw = FeedReadSnapshot.model_construct(managed_patients=snapshot().managed_patients,
                                               recent_care_logs=snapshot().recent_care_logs)
        self.rejected(raw)

    def test_all_ownership_and_dangling_log_violations_fail(self):
        mutations = (
            lambda r: setattr(r.caregiver_profile, "user_id", OTHER),
            lambda r: setattr(r.managed_patients.items[0], "caregiver_id", OTHER),
            lambda r: setattr(r.recent_care_logs.items[0], "user_id", OTHER),
            lambda r: setattr(r.recent_care_logs.items[0], "patient_id", OTHER),
            lambda r: setattr(r.long_term_summary, "user_id", OTHER),
        )
        for mutate in mutations:
            raw = snapshot(); mutate(raw)
            self.rejected(raw)
        for field in CHILDREN:
            raw = snapshot(); getattr(raw.managed_patients.items[0], field).items[0].patient_id = OTHER
            self.rejected(raw)

    def test_future_completed_visit_and_past_unvisited_visit_fail_without_filtering(self):
        for visited, day in ((True, TODAY+timedelta(days=1)), (False, TODAY-timedelta(days=1))):
            raw = snapshot()
            visit = raw.managed_patients.items[0].medical_visits.items[0]
            visit.is_visited, visit.visit_date = visited, day
            before = raw.model_dump()
            self.rejected(raw)
            self.assertEqual(raw.model_dump(), before)

    def test_stopped_medication_with_future_end_date_fails_without_reclassification(self):
        raw = snapshot()
        raw.managed_patients.items[0].medications.items[0].end_date = TODAY+timedelta(days=1)
        before = raw.model_dump()
        self.rejected(raw)
        self.assertEqual(raw.model_dump(), before)

    def test_same_day_unvisited_and_completed_visits_are_not_arbitrarily_excluded(self):
        for visited in (False, True):
            raw = snapshot()
            visit = raw.managed_patients.items[0].medical_visits.items[0]
            visit.visit_date, visit.is_visited = TODAY, visited
            source, _ = self.source(raw)
            self.assertEqual(context_loader(source)(CONTEXT, recent_days=30)
                             .managed_patient_profiles.items[0].medical_visits.items[0].is_visited, visited)

    def test_current_medication_date_flag_conflict_is_not_clinically_repaired(self):
        raw = snapshot()
        med = raw.managed_patients.items[0].medications.items[0]
        med.is_taking = True  # Past end date remains stored data, not inferred state.
        source, _ = self.source(raw)
        value = context_loader(source)(CONTEXT, recent_days=30)
        self.assertTrue(value.managed_patient_profiles.items[0].medications.items[0].is_taking)
        self.assertEqual(value.managed_patient_profiles.items[0].medications.items[0].end_date, med.end_date)

    def test_successful_absence_and_empty_inclusion_with_nonzero_count_are_distinct(self):
        raw = snapshot([], full=False)
        raw.recent_care_logs.total_count = 5
        source, _ = self.source(raw)
        value = context_loader(source)(CONTEXT, recent_days=30)
        self.assertEqual(value.recent_care_context.care_logs.items, [])
        self.assertEqual(value.recent_care_context.care_logs.coverage.total_count, 5)
        self.assertTrue(value.recent_care_context.care_logs.coverage.is_truncated)
        self.assertIsNone(value.long_term_summary)

    def test_backend_snapshot_is_unchanged_and_source_mutations_are_isolated(self):
        raw = snapshot(); before = raw.model_dump()
        source, _ = self.source(raw)
        result = source.load_snapshot(**ARGS)
        result.managed_patients.items[0].symptoms.append("changed source")
        result.recent_care_logs.items[0].content = "changed source"
        result.long_term_summary.updated_tags.append("changed source")
        self.assertEqual(raw.model_dump(), before)

    def test_non_callable_backend_is_rejected(self):
        with self.assertRaises(ProductionFeedContextSourceError):
            ProductionFeedContextSource(read_snapshot=None)


class ProductionStageOneTests(unittest.TestCase):
    def source(self, raw):
        read = Mock(return_value=raw)
        return ProductionFeedContextSource(read_snapshot=read), read

    def test_full_context_real_logs_summary_and_all_child_coverage(self):
        source, read = self.source(snapshot([patient(P2), patient(P1)]))
        value = context_loader(source)(CONTEXT, recent_days=30)
        read.assert_called_once_with(**ARGS)
        self.assertIsInstance(value, FeedPersonalizationContextV1)
        self.assertEqual([p.patient_ref for p in value.managed_patient_profiles.items], ["patient_1", "patient_2"])
        for row in value.managed_patient_profiles.items:
            for field, total in zip(CHILDREN, (7, 9, 8, 12)):
                coverage = getattr(row, field).coverage
                self.assertEqual((coverage.total_count, coverage.included_count, coverage.is_truncated),
                                 (total, 2, True))
        self.assertIsNone(value.recent_care_context.care_logs.items[0].patient_ref)
        self.assertEqual(value.recent_care_context.care_logs.items[1].patient_ref, "patient_2")
        self.assertEqual(value.recent_care_context.care_logs.coverage.total_count, 11)
        self.assertEqual(value.long_term_summary.summary, "PRIVATE_MONTHLY_SUMMARY")

    def test_loader_date_and_window_arguments_match_injected_clock(self):
        offset = timezone(timedelta(hours=-5))
        source, read = self.source(snapshot())
        prepare_production_feed_input(CONTEXT, context_loader(source, clock=lambda: NOW.astimezone(offset)))
        read.assert_called_once_with(**ARGS)

    def test_provider_input_uses_existing_allowlist_without_raw_snapshot_contents(self):
        source, _ = self.source(snapshot([patient(P2), patient(P1)]))
        text = prepare_production_feed_input(CONTEXT, context_loader(source))
        payload = json.loads(text)
        self.assertEqual(payload["feed_scope"], "caregiver_all")
        self.assertEqual(set(payload), {"feed_scope", "caregiver", "patients"})
        self.assertEqual(set(payload["caregiver"]), {"relationship", "lifestyle_tags"})
        for row in payload["patients"]:
            self.assertEqual(set(row), {"patient_ref", "dementia_stage", "symptoms", "interests",
                                        "safety_events", "assessment_types"})
        for forbidden in ("PRIVATE", str(USER), str(P1), str(P2), CONTEXT.request_id,
                          "care_logs", "summary", "coverage", "score", "medications", "medical_visits"):
            self.assertNotIn(forbidden, text)

    def test_existing_loader_validates_log_window_and_monthly_summary_dates(self):
        changes = (
            lambda r: setattr(r.recent_care_logs.items[0], "logged_at", NOW),
            lambda r: setattr(r.recent_care_logs.items[0], "logged_at", NOW-timedelta(days=30, seconds=1)),
            lambda r: setattr(r.long_term_summary, "period_start", TODAY),
            lambda r: setattr(r.long_term_summary, "period_end", TODAY+timedelta(days=1)),
            lambda r: setattr(r.long_term_summary, "generated_at", NOW+timedelta(seconds=1)),
        )
        for change in changes:
            raw = snapshot(); change(raw)
            source, _ = self.source(raw)
            with self.assertRaises(FeedContextLoadError) as caught:
                prepare_production_feed_input(CONTEXT, context_loader(source))
            self.assertEqual(caught.exception.code, "context_validation_failed")

    def test_log_window_start_and_last_instant_remain_valid(self):
        raw = snapshot()
        raw.recent_care_logs.items[0].logged_at = ARGS["recent_period_start"]
        raw.recent_care_logs.items[1].logged_at = NOW-timedelta(microseconds=1)
        source, _ = self.source(raw)
        self.assertEqual(len(context_loader(source)(CONTEXT, recent_days=30).recent_care_context.care_logs.items), 2)

    def test_feed_workflow_runs_all_three_stages_after_valid_production_prepare(self):
        source, read = self.source(snapshot())
        load = context_loader(source)
        events = []
        def prepare(context):
            events.append("context_prepare")
            return prepare_production_feed_input(context, load)
        def execute(context, prepared):
            events.append("feed_execution")
            self.assertEqual(json.loads(prepared)["feed_scope"], "caregiver_all")
            return StructuredAgentResult(
                execution={"status": "completed", "request_id": context.request_id,
                           "final_answer": None, "tool_rounds": 0, "total_tool_calls": 0,
                           "called_tools": [], "errors": []},
                output={"schema_version": "2", "response_type": "feed", "items": []}, sources=[],
            )
        receipt = object()
        def handoff(context, result):
            events.append("backend_handoff")
            self.assertEqual(result.execution.request_id, context.request_id)
            return receipt
        workflow = FeedWorkflow(context_prepare=prepare, feed_execution=execute, backend_handoff=handoff)
        self.assertIs(workflow.run(CONTEXT), receipt)
        self.assertEqual(events, ["context_prepare", "feed_execution", "backend_handoff"])
        read.assert_called_once_with(**ARGS)

    def test_invalid_snapshot_policy_or_backend_failure_never_reaches_execution(self):
        raw = snapshot(); raw.managed_patients.items[0].medical_visits.items[0].is_visited = False
        for value in (raw, None, feed_read.FeedUserNotFound(USER), feed_read.FeedReadError("PRIVATE_DB"),
                      feed_read.FeedPatientLimitExceeded(USER, 21)):
            read = Mock(side_effect=value) if isinstance(value, Exception) else Mock(return_value=value)
            source = ProductionFeedContextSource(read_snapshot=read)
            load = context_loader(source)
            execute, handoff = Mock(), Mock()
            workflow = FeedWorkflow(context_prepare=lambda c: prepare_production_feed_input(c, load),
                                    feed_execution=execute, backend_handoff=handoff)
            with self.assertRaises(FeedWorkflowError) as caught:
                workflow.run(CONTEXT)
            self.assertEqual(caught.exception.stage, "context_prepare")
            execute.assert_not_called()
            handoff.assert_not_called()
            read.assert_called_once_with(**ARGS)

    def test_real_backend_function_is_called_through_production_stage_one_with_only_db_io_mocked(self):
        raw = snapshot([patient(P2), patient(P1)])
        patients = raw.managed_patients.items
        cursor = MagicMock()
        cursor.fetchone.side_effect = [{"ok": 1}, raw.caregiver_profile.model_dump(),
                                      {"total": raw.recent_care_logs.total_count}, raw.long_term_summary.model_dump()]
        patient_rows = [{key: getattr(row, key) for key in (
            "patient_id", "caregiver_id", "dementia_stage", "diagnosis_date", "symptoms", "interests", "updated_at")}
            for row in patients]
        all_rows = [patient_rows]
        for field in CHILDREN:
            all_rows.extend((
                [record.model_dump() for row in patients for record in getattr(row, field).items],
                [{"patient_id": row.patient_id, "total": getattr(row, field).total_count} for row in patients],
            ))
        all_rows.append([log.model_dump() for log in raw.recent_care_logs.items])
        all_rows.append([])  # 신규 recently_recommended 조회 결과
        cursor.fetchall.side_effect = all_rows
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        with patch.object(feed_read, "get_connection") as get_connection:
            get_connection.return_value.__enter__.return_value = connection
            read = Mock(wraps=feed_read.get_feed_context_snapshot)
            source = ProductionFeedContextSource(read_snapshot=read)
            # Use the real Loader/Prepare; capture its full Context before minimization.
            load = context_loader(source)
            execution = Mock(side_effect=lambda context, prepared: StructuredAgentResult(
                execution={"status": "completed", "request_id": context.request_id,
                           "final_answer": None, "tool_rounds": 0, "total_tool_calls": 0,
                           "called_tools": [], "errors": []},
                output={"schema_version": "2", "response_type": "feed", "items": []}, sources=[],
            ))
            receipt = object()
            handoff = Mock(return_value=receipt)
            workflow = FeedWorkflow(
                context_prepare=lambda context: prepare_production_feed_input(context, load),
                feed_execution=execution, backend_handoff=handoff,
            )
            with patch.object(prepare_module, "build_production_feed_provider_input",
                              wraps=prepare_module.build_production_feed_provider_input) as project:
                self.assertIs(workflow.run(CONTEXT), receipt)
                result = json.loads(execution.call_args.args[1])
                captured = project.call_args.args[0]
        read.assert_called_once_with(**ARGS)
        get_connection.assert_called_once_with()
        self.assertEqual(connection.isolation_level, IsolationLevel.REPEATABLE_READ)
        self.assertIs(connection.read_only, True)
        self.assertEqual(result["feed_scope"], "caregiver_all")
        self.assertEqual(len(result["patients"]), 2)
        execution.assert_called_once()
        handoff.assert_called_once()
        self.assertEqual(captured.recent_care_context.care_logs.coverage.total_count, 11)
        self.assertEqual(captured.long_term_summary.summary, "PRIVATE_MONTHLY_SUMMARY")
        self.assertEqual(cursor.execute.call_count, 15)
        self.assertEqual([call.args[0] for call in cursor.execute.call_args_list], [
            feed_read._USER_EXISTS, feed_read._CAREGIVER_PROFILE, feed_read._PATIENTS,
            feed_read._ASSESSMENTS, feed_read._ASSESSMENTS_COUNT,
            feed_read._SAFETY, feed_read._SAFETY_COUNT,
            feed_read._MEDICATIONS, feed_read._MEDICATIONS_COUNT,
            feed_read._VISITS, feed_read._VISITS_COUNT,
            feed_read._CARE_LOGS, feed_read._CARE_LOGS_COUNT,
            feed_read._LONG_TERM_SUMMARY, feed_read._RECENT_FEED_PAPERS,
        ])
