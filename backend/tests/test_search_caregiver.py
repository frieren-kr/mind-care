"""search()의 환자 중심 규칙 테스트 (담당: 김현서). 임베딩 모델·DB 없이 가짜 논문으로 돈다.

  - 환자 증상 검색: 보호자 본인 대상 논문은 빠진다 (환자+보호자 함께 하는 연구는 남음)
  - 보호자 검색어(caregiver, burden, 보호자 …): 보호자 논문이 그대로 남는다
  - relevance_score는 원래 유사도 그대로

실행 (backend/ 에서):  python -m unittest tests.test_search_caregiver
"""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

from app.ai.retrieval import search as rag

PAPERS = [  # (pmid, 제목, 유사도)
    ("1", "Nonpharmacological interventions to improve the sleep of informal caregivers of people with dementia", 0.701),
    ("2", "The DREAMS START intervention for sleep in dementia: long-term follow-up of a randomized controlled trial", 0.698),
    ("3", "Sleep and daily pain intensity among dementia caregivers", 0.690),
    ("4", "Reminiscence therapy for people living with dementia and their caregivers", 0.670),
    ("5", "Sleep disorders in Alzheimer's disease", 0.658),
]


def _paper(pmid, title):
    return NS(paper_id=pmid, external_id=pmid, title=title, abstract="dementia", publication_types=[],
              mesh_terms=["Dementia", "Humans"], published_date=None, journal=None, doi=None)


class PatientFocusSearchTest(unittest.TestCase):
    def setUp(self):
        papers = {pm: _paper(pm, t) for pm, t, _ in PAPERS}
        patches = [
            patch.object(rag, "embed_query", return_value=[0.0]),
            patch.object(rag, "search_similar",
                         side_effect=lambda vec, top_k, model_name: [NS(paper_id=pm, score=s) for pm, _, s in PAPERS][:top_k]),
            patch.object(rag, "get_papers_by_ids", side_effect=lambda ids: [papers[i] for i in ids]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def pmids(self, pkg):
        return [e.pmid for e in pkg.evidence]

    def test_patient_query_drops_caregiver_papers(self):
        pkg = rag.search("sleep disturbance in mild dementia: interventions and management", k=5)
        self.assertEqual(self.pmids(pkg), ["2", "4", "5"])  # 1·3(보호자 본인) 제외, 4(환자+보호자)는 남음

    def test_caregiver_query_keeps_caregiver_papers(self):
        pkg = rag.search("caregiver burden and sleep", k=5)
        self.assertEqual(self.pmids(pkg), ["1", "2", "3", "4", "5"])

    def test_korean_caregiver_query_keeps(self):
        self.assertIn("1", self.pmids(rag.search("보호자 수면 문제", k=5)))

    def test_exclusion_can_be_turned_off(self):
        pkg = rag.search("sleep disturbance in mild dementia", k=5, exclude_caregiver=False)
        self.assertEqual(self.pmids(pkg), ["1", "2", "3", "4", "5"])

    def test_relevance_score_is_original_similarity(self):
        pkg = rag.search("sleep disturbance in mild dementia", k=5)
        self.assertEqual(pkg.evidence[0].relevance_score, 0.698)

    def test_search_by_queries_decides_per_query(self):
        pkg = rag.search_by_queries(["sleep disturbance in mild dementia", "caregiver support programs"], k=5)
        # 환자 줄: 2,4,5 / 보호자 줄: 1,2,3,4,5 → 번갈아: 2,1,4,(2 중복),5,3
        self.assertEqual(self.pmids(pkg), ["2", "1", "4", "5", "3"])


if __name__ == "__main__":
    unittest.main()
