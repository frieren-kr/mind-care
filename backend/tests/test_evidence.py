"""evidence.py 규칙 테스트 (담당: 김현서). DB·모델 없이 돈다.

실행 (backend/ 에서):  python -m unittest tests.test_evidence
규칙을 고칠 때 예전 판단이 깨지지 않았는지 확인하는 용도다.
"""
import unittest

from app.ai.retrieval.evidence import (
    classify,
    is_caregiver_focused,
    is_dementia_related,
    query_targets_caregiver,
    to_grade,
)

HUMAN = ["Humans"]


def study(pubtypes, title, abstract=""):
    return classify(pubtypes, HUMAN, title, abstract)


class ClassifyTest(unittest.TestCase):
    def test_pubtype_order_strongest_first(self):
        e = study(["Meta-Analysis", "Systematic Review"], "Exercise for dementia")
        self.assertEqual((e.study_type, e.evidence_level), ("meta_analysis", 1))
        self.assertEqual(study(["Randomized Controlled Trial"], "Music therapy trial").study_type, "rct")

    def test_secondary_analysis_of_rct_is_observational(self):
        e = study(["Randomized Controlled Trial"], "Sleep is associated with agitation: a secondary analysis")
        self.assertEqual((e.study_type, e.evidence_level), ("observational", 4))

    def test_title_keyword_when_pubtype_is_only_journal_article(self):
        self.assertEqual(study(["Journal Article"], "A systematic review of music therapy").study_type,
                         "systematic_review")

    def test_animal_study(self):
        e = classify([], [], "Amyloid in transgenic mice", "")
        self.assertTrue(e.is_animal)
        self.assertIsNone(e.evidence_level)

    def test_unknown_is_other(self):
        self.assertEqual(study(["Journal Article"], "Living with dementia at home").study_type, "other")


class ProtocolTest(unittest.TestCase):
    """연구 계획서(결과 없음)는 꼬리표가 RCT여도 protocol, 등급 없음."""

    def test_protocol_titles(self):
        for pubtypes, title in [
            (["Journal Article"], "A Research Protocol to Develop a Music-Based Mobile Application to Improve Sleep"),
            (["Randomized Controlled Trial"], "Sleep in Cognitive Impairment Trial (REST)-Protocol for a randomised controlled trial"),
            (["Journal Article"], "Exercise for agitation: a cluster randomized trial design and rationale"),
            (["Systematic Review Protocol"], "Music therapy for dementia: a protocol for systematic review"),
            (["Journal Article"], "Protocol for a stepped-wedge cluster trial of a dementia care programme"),
            (["Journal Article"], "Caregiver training in dementia: study protocol"),
        ]:
            with self.subTest(title=title):
                e = study(pubtypes, title)
                self.assertEqual((e.study_type, e.evidence_level), ("protocol", None))

    def test_protocol_abstract_future_tense(self):
        e = study(["Journal Article"], "Music and sleep in dementia",
                  "Participants will be randomised to music or usual care.")
        self.assertEqual(e.study_type, "protocol")

    def test_care_protocols_are_not_study_protocols(self):
        self.assertNotEqual(study(["Journal Article"], "Development of a care protocol for nurses in nursing homes").study_type,
                            "protocol")
        self.assertEqual(study(["Journal Article"], "Adherence to dementia care protocols in hospitals: a qualitative study").study_type,
                         "qualitative")
        self.assertEqual(study(["Randomized Controlled Trial"], "The DREAMS START intervention for sleep in dementia: long-term follow-up of a randomized controlled trial").study_type,
                         "rct")


class GradeTest(unittest.TestCase):
    def test_to_grade(self):
        self.assertEqual([to_grade(i) for i in range(1, 7)], ["A", "B", "C", "C", "D", "D"])
        self.assertIsNone(to_grade(None))


class DementiaFilterTest(unittest.TestCase):
    def test_mesh_dementia(self):
        self.assertTrue(is_dementia_related(["Dementia"], "Anything"))

    def test_other_disease_title_without_dementia_word(self):
        self.assertFalse(is_dementia_related([], "Cognitive impairment after stroke", "cognitive impairment"))

    def test_dementia_word_in_abstract(self):
        self.assertTrue(is_dementia_related([], "Sleep in older adults", "people with Alzheimer disease"))


class CaregiverFocusTest(unittest.TestCase):
    def test_caregiver_focused(self):
        for title in [
            "Nonpharmacological interventions to improve the sleep of informal caregivers of people with dementia",
            "Sleep and daily pain intensity among dementia caregivers",
            "Sleep quality in shift workers",
        ]:
            with self.subTest(title=title):
                self.assertTrue(is_caregiver_focused(title))

    def test_patient_or_dyad_not_caregiver_focused(self):
        for title in [
            "Reminiscence therapy for people living with cognitive impairment and their caregivers",
            "Improving sleep via carer delivered strategies in people with dementia",
            "Supporting safe walking in dementia",
        ]:
            with self.subTest(title=title):
                self.assertFalse(is_caregiver_focused(title))

    def test_query_intent(self):
        self.assertTrue(query_targets_caregiver("caregiver support programs for people with mild dementia"))
        self.assertTrue(query_targets_caregiver("보호자 부담 줄이는 법"))
        self.assertFalse(query_targets_caregiver("sleep disturbance in mild dementia: interventions and management"))


if __name__ == "__main__":
    unittest.main()
