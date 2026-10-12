"""Temporary recommendations API compatibility tests, never Production Stage 1."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import traceback
import json
import unittest
from typing import get_type_hints
from uuid import UUID


from app.user_schemas import (
    APP_TIMEZONE, CaregiverProfileOut, PatientContext, UserProfileContext,
)
from app.ai.agent.schemas import AgentContext
from app.ai.agent.workflows.feed_context_prepare import prepare_demo_feed_input
from app.ai.agent.workflows.feed_context_prepare import FeedProfileMappingError


NOW = datetime(2026, 10, 9, 16, tzinfo=timezone.utc)
TODAY = date(2026, 10, 10)
USER, P1, P2, OTHER = (UUID(int=value) for value in (10, 1, 2, 99))
CONTEXT = AgentContext(request_id="private-request-marker", user_id=USER, patient_id=OTHER)


def backend_patient(identifier=P1):
    # Build against real Backend DTO; these are synthetic persisted values.
    row = {"patient_id": identifier, "created_at": NOW}
    return PatientContext.model_validate({
        "patient": {"id": identifier, "caregiver_id": USER, "name": "PRIVATE_PATIENT_NAME",
                    "dementia_stage": "legacy-stage", "diagnosis_date": TODAY,
                    "symptoms": ["stored symptom"], "interests": ["stored interest"],
                    "created_at": NOW, "updated_at": NOW},
        "latest_assessments": [{**row, "id": UUID(int=101), "assessment_type": "legacy-test",
                                "score": 12.3, "result_detail": "stored assessment",
                                "assessed_at": date(2020, 1, 1), "assessed_by": "PRIVATE_ASSESSOR"}],
        "current_medications": [{**row, "id": UUID(int=102), "drug_name": "stored drug",
                                 "is_taking": True, "start_date": date(2020, 1, 1),
                                 "end_date": date(2020, 2, 1), "note": "stored medication note",
                                 "dosage": "PRIVATE_DOSAGE", "frequency": "PRIVATE_FREQUENCY",
                                 "updated_at": NOW}],
        "recent_safety_events": [{**row, "id": UUID(int=103), "event_date": TODAY,
                                  "has_fall": True, "has_wandering": False, "has_missing": False,
                                  "note": "stored event note"}],
        "recent_visits": [{**row, "id": UUID(int=104), "visit_date": TODAY, "is_visited": True,
                           "department": "stored department", "hospital_name": "PRIVATE_HOSPITAL",
                           "visit_content": "legacy whole visit or free memo", "updated_at": NOW,
                           **{key: "PRIVATE_005_DETAIL" for key in (
                               "visit_reason", "diagnosis", "treatment_content", "test_summary",
                               "medication_change", "doctor_note", "follow_up_plan")}}],
        "upcoming_visits": [{**row, "id": UUID(int=105), "visit_date": TODAY + timedelta(days=1),
                             "is_visited": False, "department": None, "visit_content": None,
                             "updated_at": NOW}],
    })


def backend_caregiver():
    return CaregiverProfileOut(
        user_id=USER, relationship="stored relationship", burden_score=24, mood_score=6,
        lifestyle_tags=["stored caregiver tag"], updated_at=NOW,
    )


def backend_profile(rows=None, *, caregiver=None):
    return UserProfileContext(
        user_id=USER, name="PRIVATE_CAREGIVER_NAME", caregiver_profile=caregiver,
        patients=[backend_patient()] if rows is None else rows, generated_at=NOW,
    )


class DemoContextPrepareTests(unittest.TestCase):
    def test_public_type_hints_resolve_user_profile_context_at_runtime(self):
        self.assertIs(get_type_hints(prepare_demo_feed_input)["profile"], UserProfileContext)

    def prepare(self, context, profile):
        return json.loads(prepare_demo_feed_input(context, profile,
                         reference_time=NOW, service_timezone=APP_TIMEZONE))

    def test_zero_and_one_patient_without_selection_are_valid(self):
        context = CONTEXT.model_copy(update={"patient_id": None})
        empty = self.prepare(context, backend_profile([]))
        self.assertEqual(empty, {"feed_scope": "single_patient_demo", "caregiver": None, "patients": []})
        patient = backend_patient()
        patient.patient.symptoms = []
        patient.patient.interests = []
        value = self.prepare(context, backend_profile([patient]))
        self.assertEqual(len(value["patients"]), 1)
        self.assertEqual(value["patients"][0]["symptoms"], [])
        self.assertEqual(value["patients"][0]["interests"], [])

    def test_explicit_selection_preserves_one_patient_and_source(self):
        second = backend_patient(P2)
        second.patient.symptoms = ["second patient only"]
        for rows, selected in (([backend_patient()], P1),
                               ([second, backend_patient()], P2),
                               ([backend_patient(), second], P2)):
            profile = backend_profile(rows, caregiver=backend_caregiver())
            before = profile.model_dump()
            value = self.prepare(CONTEXT.model_copy(update={"patient_id": selected}), profile)
            self.assertEqual(value["feed_scope"], "single_patient_demo")
            self.assertEqual(len(value["patients"]), 1)
            self.assertEqual(value["patients"][0]["symptoms"],
                             ["stored symptom"] if selected == P1 else ["second patient only"])
            self.assertEqual(profile.model_dump(), before)

    def test_ambiguous_missing_and_foreign_selection_fail_closed(self):
        foreign = backend_patient(P2)
        foreign.patient.caregiver_id = OTHER
        cases = ((None, backend_profile([backend_patient(), backend_patient(P2)])),
                 (OTHER, backend_profile()), (P1, backend_profile([])),
                 (P2, backend_profile([backend_patient(), foreign])),
                 (P1, backend_profile([backend_patient(), foreign])),
                 (P1, backend_profile([backend_patient(), backend_patient()])))
        for selected, profile in cases:
            with self.subTest(selected=selected), self.assertRaises(FeedProfileMappingError):
                self.prepare(CONTEXT.model_copy(update={"patient_id": selected}), profile)

    def test_raw_missing_and_mutated_selected_inputs_are_not_cold_start(self):
        profile = backend_profile()
        missing = UserProfileContext(user_id=USER, name="Synthetic", generated_at=NOW)
        bad = backend_profile()
        bad.patients[0].recent_visits[0].patient_id = P2
        future = backend_profile()
        future.patients[0].latest_assessments[0].assessed_at = TODAY + timedelta(days=1)
        for value in (None, profile.model_dump(), missing, bad, future):
            with self.subTest(kind=type(value).__name__), self.assertRaises(FeedProfileMappingError):
                self.prepare(CONTEXT.model_copy(update={"patient_id": P1}), value)
