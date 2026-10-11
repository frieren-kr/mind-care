"""/recommendations 테스트. 실 DB(팀 기준 데이터 1,000편 + 샘플 간병인)가 떠 있어야 한다.

실행 (backend/ 에서):  python -m unittest tests.test_recommendations
"""
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.ai.agent.outputs import StructuredAgentResult
from app.api.recommendations import backend_handoff, get_feed_execution
from main import app

CAREGIVER_1 = "593bb66f-148d-481e-9537-00473b147d26"  # 환자 1명
CAREGIVER_4 = "1d95ab32-8c50-4f62-ade8-a236121a2175"  # 환자 2명
NO_USER = "00000000-0000-0000-0000-000000000000"

_SEED = Path(__file__).parents[1] / "app" / "db" / "seed" / "team_pmids_1000.txt"
DB_PMIDS = _SEED.read_text().split()[:3]
MISSING_PMID = "999999999999"


def make_result(request_id: str, pmids: list) -> StructuredAgentResult:
    """웅 님 feed_execution이 돌려줄 결과를 직접 만든다 (LLM·검색 없이)."""
    refs = [{"tool_call_id": "call_1", "evidence_index": i} for i in range(len(pmids))]
    return StructuredAgentResult(
        execution={"status": "completed", "request_id": request_id, "final_answer": None,
                   "tool_rounds": 1, "total_tool_calls": 1, "called_tools": [], "errors": []},
        output={"schema_version": "1", "response_type": "feed", "items": [
            {"source_ref": ref, "headline": f"제목 {i}", "summary_bullets": [f"요약 {i}"],
             "personal_reason": f"이유 {i}", "category": "care"}
            for i, ref in enumerate(refs)]},
        sources=[
            {"source_ref": ref, "evidence_type": "new_research", "title": "AI가 본 제목", "pmid": pmid,
             "doi": None, "journal": None, "publication_year": None, "study_type": None,
             "organization": None, "source_url": None, "full_text_available": False}
            for ref, pmid in zip(refs, pmids)],
    )


class BackendHandoffTests(unittest.TestCase):
    def test_keeps_only_papers_in_db(self):
        pmids = [DB_PMIDS[0], None, DB_PMIDS[1], MISSING_PMID, DB_PMIDS[2]]
        items = backend_handoff(make_result("r1", pmids))

        self.assertEqual(len(items), 3)
        # AI 결과에서 온 칸 — 빠진 항목(1, 3번) 때문에 번호가 밀리지 않아야 한다.
        self.assertEqual([i.headline for i in items], ["제목 0", "제목 2", "제목 4"])
        self.assertEqual([i.summary_bullets for i in items], [["요약 0"], ["요약 2"], ["요약 4"]])
        self.assertEqual([i.personal_reason for i in items], ["이유 0", "이유 2", "이유 4"])
        for item, pmid in zip(items, DB_PMIDS):
            self.assertEqual(item.category, "care")
            self.assertIsNone(item.relevance_score)
            # DB에서 온 칸
            self.assertIn(pmid, item.pubmed_url)
            self.assertNotEqual(item.title, "AI가 본 제목")
            self.assertTrue(item.id)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)

    def use(self, feed_execution):
        app.dependency_overrides[get_feed_execution] = lambda: feed_execution

    def get(self, user_id=CAREGIVER_1, **params):
        return self.client.get(f"/recommendations/{user_id}", params=params)

    def assert_search(self, response):
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "search")
        self.assertTrue(body["query"])
        self.assertEqual(len(body["items"]), 5)
        self.assertIsNotNone(body["items"][0]["relevance_score"])
        self.assertIsNone(body["items"][0]["headline"])

    def test_no_feed_execution_uses_search(self):
        self.assertIsNone(get_feed_execution())  # 교체 전 기본값
        self.assert_search(self.get())

    def test_fake_feed_execution_uses_ai(self):
        received = []

        def fake(context, provider_input):
            received.append(provider_input)
            return make_result(context.request_id, DB_PMIDS)

        self.use(fake)
        response = self.get()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "ai")
        self.assertIsNone(body["query"])
        self.assertEqual(len(body["items"]), 3)
        self.assertTrue(received[0])  # 실제 프로필이 context_prepare를 통과했다
        for item, pmid in zip(body["items"], DB_PMIDS):
            self.assertTrue(item["headline"])
            self.assertTrue(item["personal_reason"])
            self.assertEqual(item["category"], "care")
            self.assertIsNone(item["relevance_score"])
            self.assertIn(pmid, item["pubmed_url"])

    def test_k_limits_ai_items(self):
        self.use(lambda context, _: make_result(context.request_id, DB_PMIDS))
        self.assertEqual(len(self.get(k=2).json()["items"]), 2)

    def test_feed_execution_error_falls_back_to_search(self):
        def broken(context, provider_input):
            raise RuntimeError("boom")

        self.use(broken)
        with self.assertLogs("app.api.recommendations", "WARNING") as logs:
            self.assert_search(self.get())
        self.assertIn("stage=feed_execution", logs.output[0])
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("boom", logs.output[0])

    def test_no_usable_ai_items_falls_back_to_search(self):
        self.use(lambda context, _: make_result(context.request_id, [None, MISSING_PMID]))
        self.assert_search(self.get())

    def test_error_statuses(self):
        self.assertEqual(self.get(CAREGIVER_4).status_code, 400)
        self.assertEqual(self.get(NO_USER).status_code, 404)
        self.assertEqual(self.get(patient_id=NO_USER).status_code, 404)
        self.assertEqual(self.get(k=6).status_code, 422)


if __name__ == "__main__":
    unittest.main()
