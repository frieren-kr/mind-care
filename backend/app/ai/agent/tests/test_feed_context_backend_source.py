"""Development policies migrated to Production; existing 29 Adapter tests remain.

Do not copy Demo latest/current/ordering rules or unavailable placeholders.
Backend DTOs omit record IDs: their uniqueness is Backend-owned, not inferred.
"""
from datetime import timedelta
import json
import unittest
from unittest.mock import Mock
from uuid import UUID

from app.user_schemas import APP_TIMEZONE
from ..testing.feed_workflow_fakes import FakeFeedDependencies
from ..workflows.feed import FeedWorkflowError
from ..workflows.feed_context_backend_source import ProductionFeedContextSource, ProductionFeedContextSourceError
from ..workflows.feed_context_loader import FeedContextLoadError
from ..workflows.feed_context_prepare import prepare_production_feed_input
from .test_feed_context_production_source import (
    CONTEXT, NOW, TODAY, USER, P1, P2, ARGS, CHILDREN, patient, snapshot, context_loader,
)


class ProductionSourceMigratedPolicyTests(unittest.TestCase):
    def source(self, raw):
        read = Mock(return_value=raw)
        return ProductionFeedContextSource(read_snapshot=read), read

    def test_empty_child_collections_keep_managed_patient_and_declared_coverage(self):
        raw = snapshot([patient(full=False)], full=False)
        source, read = self.source(raw)
        value = context_loader(source)(CONTEXT, recent_days=30)
        read.assert_called_once_with(**ARGS)
        self.assertEqual(len(value.managed_patient_profiles.items), 1)
        for field in CHILDREN:
            rows = getattr(value.managed_patient_profiles.items[0], field)
            self.assertEqual(rows.items, [])
            self.assertEqual((rows.coverage.total_count, rows.coverage.included_count), (0, 0))

    def test_every_child_provenance_is_preserved_for_all_patients(self):
        raw = snapshot([patient(P2), patient(P1)])
        for index, row in enumerate(raw.managed_patients.items):
            row.clinical_assessments.items[0].assessment_type = f"patient scale {index}"
            row.medications.items[0].drug_name = f"patient drug {index}"
            row.safety_events.items[0].note = f"patient safety {index}"
            row.medical_visits.items[0].visit_content = f"patient visit {index}"
        source, _ = self.source(raw)
        value = context_loader(source)(CONTEXT, recent_days=30)
        for row, index in zip(value.managed_patient_profiles.items, (1, 0)):
            self.assertEqual(row.clinical_assessments.items[0].assessment_type, f"patient scale {index}")
            self.assertEqual(row.medications.items[0].drug_name, f"patient drug {index}")
            self.assertEqual(row.safety_events.items[0].note, f"patient safety {index}")
            self.assertEqual(row.medical_visits.items[0].visit_content, f"patient visit {index}")

    def test_no_incident_safety_row_fails_whole_snapshot_without_filter_or_count_change(self):
        raw = snapshot()
        row = raw.managed_patients.items[0].safety_events.items[0]
        row.has_fall = row.has_wandering = row.has_missing = False
        original = raw.model_dump()
        source, read = self.source(raw)
        with self.assertRaises(ProductionFeedContextSourceError) as caught:
            source.load_snapshot(**ARGS)
        self.assertEqual(str(caught.exception), "Production Feed source validation failed")
        read.assert_called_once_with(**ARGS)
        self.assertEqual(raw.model_dump(), original)
        with self.assertRaises(FeedContextLoadError) as caught:
            prepare_production_feed_input(CONTEXT, context_loader(source))
        self.assertEqual(caught.exception.code, "source_load_failed")

    def test_each_incident_flag_is_valid_without_clinical_reinterpretation(self):
        for flag in ("has_fall", "has_wandering", "has_missing"):
            raw = snapshot()
            row = raw.managed_patients.items[0].safety_events.items[0]
            for field in ("has_fall", "has_wandering", "has_missing"):
                setattr(row, field, field == flag)
            source, _ = self.source(raw)
            value = context_loader(source)(CONTEXT, recent_days=30)
            self.assertTrue(getattr(value.managed_patient_profiles.items[0].safety_events.items[0], flag))
            self.assertEqual(value.managed_patient_profiles.items[0].safety_events.coverage.total_count, 9)

    def test_loader_still_owns_diagnosis_assessment_safety_and_medication_interval_validation(self):
        mutations = (
            lambda p: setattr(p, "diagnosis_date", TODAY+timedelta(days=1)),
            lambda p: setattr(p.clinical_assessments.items[0], "assessed_at", TODAY+timedelta(days=1)),
            lambda p: setattr(p.safety_events.items[0], "event_date", TODAY+timedelta(days=1)),
            lambda p: setattr(p.medications.items[0], "start_date", TODAY),
        )
        for mutate in mutations:
            raw = snapshot(); mutate(raw.managed_patients.items[0])
            source, _ = self.source(raw)
            source.load_snapshot(**ARGS)
            with self.assertRaises(FeedContextLoadError) as caught:
                prepare_production_feed_input(CONTEXT, context_loader(source))
            self.assertEqual(caught.exception.code, "context_validation_failed")

    def test_null_score_and_empty_arrays_are_real_values_not_inferred_defaults(self):
        raw = snapshot()
        row = raw.managed_patients.items[0]
        row.clinical_assessments.items[0].score = None
        row.symptoms = row.interests = []
        raw.caregiver_profile.lifestyle_tags = []
        source, _ = self.source(raw)
        result = source.load_snapshot(**ARGS)
        self.assertIsNone(result.managed_patients.items[0].clinical_assessments.items[0].score)
        self.assertEqual(result.managed_patients.items[0].symptoms, [])
        self.assertEqual(result.managed_patients.items[0].interests, [])
        self.assertEqual(result.caregiver_profile.lifestyle_tags, [])

    def test_assessment_history_is_not_demo_deduplicated(self):
        raw = snapshot()
        row = raw.managed_patients.items[0]
        row.clinical_assessments.items[1].assessment_type = row.clinical_assessments.items[0].assessment_type
        original = raw.model_dump()
        source, _ = self.source(raw)
        value = context_loader(source)(CONTEXT, recent_days=30)
        self.assertEqual(len(value.managed_patient_profiles.items[0].clinical_assessments.items), 2)
        self.assertEqual(value.managed_patient_profiles.items[0].clinical_assessments.coverage.total_count, 7)
        self.assertEqual(raw.model_dump(), original)

    def test_stored_backend_order_is_preserved_for_null_start_dates_and_equal_dates(self):
        raw = snapshot()
        row = raw.managed_patients.items[0]
        row.medications.items.reverse()  # null start date first, intentionally.
        row.medical_visits.items[0].visit_date = TODAY
        row.medical_visits.items[1].visit_date = TODAY
        before = raw.model_dump()
        source, _ = self.source(raw)
        result = source.load_snapshot(**ARGS).managed_patients.items[0]
        self.assertIsNone(result.medications.items[0].start_date)
        self.assertEqual([v.is_visited for v in result.medical_visits.items], [True, False])
        self.assertEqual(raw.model_dump(), before)

    def test_adapter_does_not_apply_own_caps_or_replace_backend_total_with_included_count(self):
        raw = snapshot()
        row = raw.managed_patients.items[0]
        record = row.clinical_assessments.items[0]
        row.clinical_assessments.items = [record.model_copy(update={"assessed_at": TODAY-timedelta(days=i)})
                                          for i in range(21)]
        row.clinical_assessments.total_count = 50
        source, read = self.source(raw)
        value = context_loader(source)(CONTEXT, recent_days=30)
        read.assert_called_once_with(**ARGS)
        rows = value.managed_patient_profiles.items[0].clinical_assessments
        self.assertEqual((rows.coverage.total_count, rows.coverage.included_count, rows.coverage.is_truncated),
                         (50, 21, True))

    def test_one_invalid_patient_never_becomes_partial_success(self):
        raw = snapshot([patient(P2), patient(P1)])
        raw.managed_patients.items[1].medications.items[0].patient_id = P2
        original = raw.model_dump()
        source, read = self.source(raw)
        with self.assertRaises(ProductionFeedContextSourceError):
            source.load_snapshot(**ARGS)
        read.assert_called_once_with(**ARGS)
        self.assertEqual(raw.model_dump(), original)

    def test_over_patient_cap_stops_workflow_before_provider_and_handoff(self):
        raw = snapshot([patient(UUID(int=i), full=False) for i in range(1, 22)], full=False)
        deps = FakeFeedDependencies(CONTEXT, snapshot=raw, reference_time=NOW)
        with self.assertRaises(FeedWorkflowError) as caught:
            deps.workflow().run(CONTEXT)
        self.assertEqual(caught.exception.stage, "context_prepare")
        self.assertEqual(len(deps.read_calls), 1)
        self.assertEqual(deps.client.requests, [])
        self.assertEqual(deps.saved, [])

    def test_snapshot_reference_time_is_loader_owned_and_has_no_aggregate_generated_at(self):
        raw = snapshot()
        source, read = self.source(raw)
        value = context_loader(source)(CONTEXT, recent_days=30)
        read.assert_called_once_with(**ARGS)
        self.assertEqual(value.reference_time, NOW)
        self.assertEqual(value.recent_care_context.period_start, NOW-timedelta(days=30))
        self.assertEqual(value.recent_care_context.period_end, NOW)
        self.assertNotIn("generated_at", json.loads(prepare_production_feed_input(CONTEXT, context_loader(source))))
