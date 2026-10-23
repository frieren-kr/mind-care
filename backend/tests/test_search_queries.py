"""검색 방식(mode="search") 영어 검색어 테스트. DB·임베딩 없이 돈다.

실행 (backend/ 에서):  python -m unittest tests.test_search_queries
"""
import unittest
from datetime import datetime
from unittest.mock import patch
from uuid import UUID, uuid4

from app.ai.retrieval.search import EvidencePackage
from app.api.recommendations import GENERAL_QUERY, build_queries, search_recommendations
from app.user_schemas import PatientOut

NOW = datetime(2026, 10, 21)


def patient(stage=None, symptoms=(), interests=()) -> PatientOut:
    return PatientOut(id=uuid4(), caregiver_id=UUID(int=1), dementia_stage=stage,
                      symptoms=list(symptoms), interests=list(interests), created_at=NOW, updated_at=NOW)


class BuildQueriesTests(unittest.TestCase):
    def test_caregiver_two_example(self):
        """간병인2 예: 중등도 + 배회·낙상."""
        self.assertEqual(build_queries(patient("중등도", ["배회", "낙상 위험"], ["배회 대응", "낙상 예방"])), [
            "wandering in moderate dementia: interventions and management",
            "wandering management for people with moderate dementia",
            "fall risk in moderate dementia: interventions and management",
            "fall prevention for people with moderate dementia",
        ])

    def test_unknown_word_stays_korean_and_spacing_is_ignored(self):
        self.assertEqual(build_queries(patient("경도", [" 수면 장애 ", "손떨림"], ["보호자지원 프로그램"])), [
            "sleep disturbance in mild dementia: interventions and management",
            "caregiver support programs for people with mild dementia",
            "손떨림 in mild dementia: interventions and management",
        ])

    def test_no_stage_or_unknown_stage_is_dementia(self):
        self.assertEqual(build_queries(patient(None, ["우울"])),
                         ["depression in dementia: interventions and management"])
        self.assertEqual(build_queries(patient("모름", ["우울"])),
                         ["depression in dementia: interventions and management"])

    def test_no_symptoms_or_interests(self):
        self.assertEqual(build_queries(patient("중증")), ["severe dementia care: interventions and management"])
        self.assertEqual(build_queries(patient()), [GENERAL_QUERY])

    def test_drug_and_non_drug_treatment_expand(self):
        self.assertEqual(build_queries(patient("경도", interests=["약물치료", "비약물 치료"])), [
            "cholinesterase inhibitors and memantine for mild dementia",
            "anti-amyloid antibody treatment (lecanemab, donanemab) for early Alzheimer's disease",
            "cognitive stimulation and cognitive training for mild dementia",
            "physical exercise intervention for mild dementia and mild cognitive impairment",
        ])

    def test_anti_amyloid_line_only_for_early_stages(self):
        anti_amyloid = "anti-amyloid antibody treatment (lecanemab, donanemab) for early Alzheimer's disease"
        for stage in ("경도인지장애", "경도"):
            self.assertIn(anti_amyloid, build_queries(patient(stage, interests=["약물 치료"])))
        for stage in ("중등도", "중증", None):
            queries = build_queries(patient(stage, interests=["약물 치료"]))
            self.assertEqual(len(queries), 1)
            self.assertTrue(queries[0].startswith("cholinesterase inhibitors and memantine for "))
        self.assertEqual(build_queries(patient("중증", interests=["약물 치료"])),
                         ["cholinesterase inhibitors and memantine for severe dementia"])

    def test_alternates_and_caps_at_four(self):
        queries = build_queries(patient("경도", ["우울", "불안", "환각"], ["운동", "음악 치료"]))
        self.assertEqual(queries, [
            "depression in mild dementia: interventions and management",
            "physical exercise for people with mild dementia",
            "anxiety in mild dementia: interventions and management",
            "music therapy for people with mild dementia",
        ])

    def test_same_line_once(self):
        """수면장애·수면 문제는 같은 영어라 한 줄만."""
        self.assertEqual(build_queries(patient("경도", ["수면장애", "수면 문제"])),
                         ["sleep disturbance in mild dementia: interventions and management"])

    def test_no_caregiver_or_family_words(self):
        queries = build_queries(patient("중등도", ["배회", "우울", "공격성"], ["운동", "약물 치료"]))
        self.assertFalse(any("caregiver" in q or "family" in q for q in queries))


class SearchRecommendationsQueriesTests(unittest.TestCase):
    def queries(self, patients, interests=None, k=5):
        with patch("app.api.recommendations.search_by_queries",
                   return_value=EvidencePackage(query="", evidence=[])) as search, \
             patch("app.api.recommendations.functions.get_paper_details_by_pmids", return_value={}):
            body = search_recommendations(UUID(int=1), None, patients, k, caregiver_interests=interests)
        self.assertEqual(search.call_args.args[0], body.queries)
        return body.queries

    def test_patients_mix_by_line_order_and_dedupe(self):
        first = patient("경도", ["우울", "배회"])
        second = patient("중등도", ["배회"], ["운동"])
        third = patient("경도", ["우울"])  # 첫 환자 1번째 줄과 같다
        self.assertEqual(self.queries([first, second, third]), [
            "depression in mild dementia: interventions and management",
            "wandering in moderate dementia: interventions and management",
            "wandering in mild dementia: interventions and management",
            "physical exercise for people with moderate dementia",
        ])

    def test_zero_patients_use_general_query(self):
        self.assertEqual(self.queries([]), [GENERAL_QUERY])

    def test_caregiver_interests_mix_like_one_more_patient(self):
        queries = self.queries([patient("경도", ["우울"])], ["운동", "돌봄스트레스", "운동 "])
        self.assertEqual(queries, [
            "depression in mild dementia: interventions and management",
            "physical exercise for people with dementia",
            "돌봄스트레스 for people with dementia",
        ])

    def test_caregiver_interest_is_in_first_round_with_many_patients(self):
        """환자 2명(각 4줄) + 간병인 관심사: 관심사 1번째 줄이 첫 바퀴(3번째)에 들어가고 전체는 k줄."""
        first = patient("경도", ["우울", "불안"], ["운동", "음악 치료"])
        second = patient("중증", ["삼킴 곤란", "공격성"], ["식사 보조", "욕창 예방"])
        self.assertEqual(self.queries([first, second], ["회상 치료"]), [
            "depression in mild dementia: interventions and management",
            "dysphagia in severe dementia: interventions and management",
            "reminiscence therapy for people with dementia",
            "physical exercise for people with mild dementia",
            "feeding assistance for people with severe dementia",
        ])

    def test_queries_capped_at_k(self):
        first = patient("경도", ["우울", "불안"], ["운동", "음악 치료"])
        second = patient("중증", ["삼킴 곤란", "공격성"], ["식사 보조", "욕창 예방"])
        self.assertEqual(len(self.queries([first, second])), 5)
        self.assertEqual(self.queries([first, second], k=2), [
            "depression in mild dementia: interventions and management",
            "dysphagia in severe dementia: interventions and management",
        ])

    def test_caregiver_interest_duplicate_line_once(self):
        self.assertEqual(self.queries([], ["돌봄"]), [GENERAL_QUERY, "돌봄 for people with dementia"])
        self.assertEqual(self.queries([patient(None, interests=["운동"])], ["운동"]),
                         ["physical exercise for people with dementia"])


if __name__ == "__main__":
    unittest.main()
