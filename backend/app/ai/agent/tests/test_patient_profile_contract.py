"""Patient Profile V1 계약 검증. 실제 DB/권한/의료 판단은 구현하지 않는다."""
from datetime import date
from unittest import TestCase

from pydantic import ValidationError

from ..executor import ToolExecutor
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..testing.fake_tools import fake_get_patient_profile
from ..tools import contracts as c
from .test_workflows import CONTEXT, call


PROFILE_FIELDS = {"name", "dementia_stage", "diagnosis_date", "symptoms", "interests"}
SPARSE_PROFILE = {"name": None, "dementia_stage": None, "diagnosis_date": None,
                  "symptoms": [], "interests": []}


class PatientProfileContractTests(TestCase):
    def execute(self, handler, args=None):
        registry = ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.PATIENT_PROFILE], handler),))
        return ToolExecutor(registry).execute(call(args=args), CONTEXT)

    def test_empty_input_and_canonical_schema(self):
        self.assertEqual(c.PatientProfileInput().model_dump(), {})
        schema = c.PatientProfileInput.model_json_schema()
        self.assertEqual(schema["properties"], {})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(TOOL_CONTRACTS[ToolName.PATIENT_PROFILE].definition()["function"]["parameters"], schema)

    def test_additional_arguments_rejected_before_handler(self):
        for key, value in {"patient_id": str(CONTEXT.patient_id), "user_id": str(CONTEXT.user_id),
                           "caregiver_id": str(CONTEXT.user_id), "include_conditions": True,
                           "include_care_environment": False, "unknown": "value"}.items():
            with self.subTest(key=key):
                with self.assertRaises(ValidationError):
                    c.PatientProfileInput.model_validate({key: value})
                seen = []
                outcome, _ = self.execute(lambda context, args: seen.append(context), {key: value})
                self.assertFalse(outcome.success)
                self.assertEqual(outcome.error.code, "invalid_arguments")
                self.assertIsNone(outcome.data)
                self.assertEqual(seen, [])

    def test_exact_output_fields(self):
        self.assertEqual(set(c.PatientProfileOutput.model_fields), PROFILE_FIELDS)
        schema = c.PatientProfileOutput.model_json_schema()
        self.assertEqual(set(schema["properties"]), PROFILE_FIELDS)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(c.PatientProfileOutput().model_dump()), PROFILE_FIELDS)

    def test_extra_output_fields_rejected(self):
        for key, value in {"patient_id": str(CONTEXT.patient_id), "user_id": str(CONTEXT.user_id),
                           "caregiver_id": str(CONTEXT.user_id), "conditions": [],
                           "care_environment": {"type": "home"}, "unknown": None}.items():
            with self.subTest(key=key), self.assertRaises(ValidationError):
                c.PatientProfileOutput.model_validate({key: value})

    def test_sparse_profile_defaults_and_json_roundtrip(self):
        output = c.PatientProfileOutput()
        self.assertEqual(output.model_dump(), SPARSE_PROFILE)
        self.assertEqual(c.PatientProfileOutput.model_validate_json(output.model_dump_json()), output)

    def test_list_defaults_are_independent(self):
        first, second = c.PatientProfileOutput(), c.PatientProfileOutput()
        first.symptoms.append("fixture")
        first.interests.append("fixture")
        self.assertEqual(second.symptoms, [])
        self.assertEqual(second.interests, [])

    def test_stage_accepts_arbitrary_stored_string_and_null(self):
        for stage in (None, "경도", " MCI/임의 저장 문자열 "):
            with self.subTest(stage=stage):
                self.assertEqual(c.PatientProfileOutput(dementia_stage=stage).dementia_stage, stage)

    def test_diagnosis_date_iso_serialization(self):
        output = c.PatientProfileOutput(diagnosis_date=date(2025, 1, 1))
        self.assertEqual(output.model_dump(mode="json")["diagnosis_date"], "2025-01-01")
        self.assertEqual(c.PatientProfileOutput.model_validate_json(output.model_dump_json()), output)

    def test_fake_uses_deterministic_v1_fixture(self):
        args = c.PatientProfileInput()
        first = fake_get_patient_profile(CONTEXT, args)
        self.assertEqual(first, fake_get_patient_profile(CONTEXT, args))
        self.assertEqual(first.model_dump(mode="json"), {
            "name": "합성 테스트 환자", "dementia_stage": "경도", "diagnosis_date": "2025-01-01",
            "symptoms": ["수면 변화"], "interests": ["수면"],
        })

    def test_sparse_model_and_dict_are_successful(self):
        for returned in (c.PatientProfileOutput(), SPARSE_PROFILE):
            with self.subTest(returned=type(returned).__name__):
                outcome, trace = self.execute(lambda context, args: returned)
                self.assertTrue(outcome.success)
                self.assertTrue(trace.success)
                self.assertIsNone(outcome.error)
                self.assertEqual(outcome.data, SPARSE_PROFILE)

    def test_handler_failures_are_not_sparse_success(self):
        def failing(context, args):
            raise RuntimeError("PRIVATE database detail")

        def timed_out(context, args):
            raise TimeoutError("PRIVATE timeout detail")

        cases = [(lambda context, args: None, "empty_result"),
                 (lambda context, args: {"name": 123}, "invalid_output"),
                 (failing, "tool_exception"), (timed_out, "tool_timeout")]
        for handler, expected in cases:
            with self.subTest(expected=expected):
                outcome, trace = self.execute(handler)
                self.assertFalse(outcome.success)
                self.assertFalse(trace.success)
                self.assertIsNone(outcome.data)
                self.assertEqual(outcome.error.code, expected)
                self.assertEqual(trace.error_type, expected)
                self.assertNotIn("PRIVATE", outcome.model_dump_json())
