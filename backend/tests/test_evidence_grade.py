"""검색 결과의 근거 등급(evidence_grade) 테스트 (담당: 김현서).

챗봇이 "근거 등급 어때?"에 답하려면 Retriever가 등급을 넘겨야 한다.
등급은 피드(_derived_grade)·앱 화면과 같은 규칙(study_type → LEVEL → to_grade)이어야 한다.
"""
import unittest

from app.ai.retrieval.evidence import LEVEL, to_grade
from app.ai.retrieval.search import EvidenceItem, evidence_grade


class EvidenceGradeTest(unittest.TestCase):
    def test_same_rule_as_feed(self):
        for st in LEVEL:
            self.assertEqual(evidence_grade(st), to_grade(LEVEL.get(st)))

    def test_examples(self):
        self.assertEqual(evidence_grade("meta_analysis"), "A")
        self.assertEqual(evidence_grade("guideline"), "A")
        self.assertEqual(evidence_grade("rct"), "B")
        self.assertIsNone(evidence_grade("protocol"))

    def test_grade_is_a_to_d_or_none(self):
        for st in list(LEVEL) + ["unknown_type"]:
            self.assertIn(evidence_grade(st), {"A", "B", "C", "D", None})

    def test_empty_study_type(self):
        self.assertIsNone(evidence_grade(None))
        self.assertIsNone(evidence_grade(""))

    def test_field_default_none(self):
        item = EvidenceItem(evidence_type="new_research", title="t", relevance_score=0.5)
        self.assertIsNone(item.evidence_grade)


if __name__ == "__main__":
    unittest.main()
