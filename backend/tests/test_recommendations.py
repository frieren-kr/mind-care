"""/recommendations 테스트. 실 DB가 떠 있어야 한다.

실행 (backend/ 에서):  python -m unittest tests.test_recommendations

★ PC마다 다른 환경에서도 돌아간다 ★
사용자 id를 하드코딩하지 않는다. 시드 스크립트가 만드는 **이메일**로 users에서 id를 찾는다
(`uuid_generate_v4()`라서 시드를 돌릴 때마다 id가 달라지고, 팀원끼리도 다르다).
필요한 데이터가 없으면 **실패가 아니라 skip** 하고, 무엇을 실행해야 하는지 이유에 적는다.

테스트별로 필요한 것이 다르다:
- 샘플 간병인 (`python -m scripts.seed_dev_profiles`) — 엔드포인트 테스트 전부
- 팀 기준 논문 (`team_pmids_1000.txt`의 논문이 `papers`에) — AI 방식 경로
- 논문 임베딩 (`paper_embeddings`) + `sentence-transformers` 설치 — 검색 방식 경로
뒤 두 가지는 팀 덤프를 복원해야 생긴다 (루트 README의 "팀원 데이터 맞추기").
"""
import unittest
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from app.ai.agent.outputs import StructuredAgentResult
from app.api.recommendations import backend_handoff, get_feed_execution
from app.db.connection import get_connection
from main import app

# 시드 스크립트(scripts/seed_dev_profiles.py)가 만드는 샘플 간병인 이메일.
SEED_EMAIL_ONE_PATIENT = "dev-seed-1@example.com"   # 환자 1명
SEED_EMAIL_TWO_PATIENTS = "dev-seed-4@example.com"  # 환자 2명
SEED_EMAIL_LIKE = "dev-seed-%@example.com"

NO_USER = "00000000-0000-0000-0000-000000000000"  # 어느 DB에도 없는 sentinel

_SEED = Path(__file__).parents[1] / "app" / "db" / "seed" / "team_pmids_1000.txt"
DB_PMIDS = _SEED.read_text().split()[:3]
MISSING_PMID = "999999999999"

# assert_search()가 기대하는 항목 수 = 검색이 돌려줘야 하는 최소 임베딩 수.
SEARCH_ITEMS = 5

SEED_HINT = "샘플 간병인이 없습니다 — python -m scripts.seed_dev_profiles 먼저 실행"
DUMP_HINT = "팀 덤프 복원 필요 (루트 README의 '팀원 데이터 맞추기')"


@dataclass(frozen=True)
class DbState:
    """테스트를 돌릴 수 있는지 판단하는 데 필요한 DB 현황."""

    # 샘플 간병인: 이메일 → (user_id, 담당 환자 수)
    seed_users: dict[str, tuple[UUID, int]] = field(default_factory=dict)
    team_papers: int = 0   # DB_PMIDS 중 papers에 있는 수
    embeddings: int = 0    # paper_embeddings 행 수
    error: str = ""        # DB에 접속조차 못 한 경우의 메시지


@cache
def db_state() -> DbState:
    """DB 현황을 한 번만 조회해 재사용한다 (import 시점이 아니라 첫 사용 시점에).

    DB가 꺼져 있어도 import가 깨지지 않게, 접속 실패는 error에 담아 돌려준다.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT u.email, u.id, count(p.id) AS patients
                      FROM users AS u
                      LEFT JOIN patient_profiles AS p ON p.caregiver_id = u.id
                     WHERE u.email LIKE %s
                     GROUP BY u.email, u.id
                    """,
                    (SEED_EMAIL_LIKE,),
                )
                seed_users = {email: (user_id, patients)
                              for email, user_id, patients in cur.fetchall()}
                cur.execute("SELECT count(*) FROM papers WHERE external_id = ANY(%s)", (DB_PMIDS,))
                team_papers = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM paper_embeddings")
                embeddings = cur.fetchone()[0]
        return DbState(seed_users=seed_users, team_papers=team_papers, embeddings=embeddings)
    except Exception as err:  # 접속 실패 / 테이블 없음 등
        return DbState(error=f"DB를 읽을 수 없습니다 ({type(err).__name__}) — "
                             "docker compose up -d 후 마이그레이션 적용 여부를 확인하세요")


def seed_user_id(test: unittest.TestCase, email: str, expected_patients: int) -> UUID:
    """시드 이메일로 user_id를 찾는다. 없으면 skip.

    담당 환자 수까지 확인한다 (테스트가 '환자 1명'·'환자 2명' 상황을 구분해서 쓰기 때문에,
    시드 내용이 바뀌면 틀린 단정 대신 skip 하는 게 낫다).
    """
    state = db_state()
    if state.error:
        test.skipTest(state.error)
    if email not in state.seed_users:
        test.skipTest(f"{SEED_HINT} ({email} 없음)")
    user_id, patients = state.seed_users[email]
    if patients != expected_patients:
        test.skipTest(
            f"{email}의 담당 환자가 {patients}명입니다 (이 테스트는 {expected_patients}명 기준) — "
            "python -m scripts.seed_dev_profiles --reset 로 샘플을 다시 만드세요"
        )
    return user_id


def require_team_papers(test: unittest.TestCase) -> None:
    """team_pmids_1000.txt의 논문이 DB에 있어야 하는 테스트."""
    state = db_state()
    if state.error:
        test.skipTest(state.error)
    if state.team_papers < len(DB_PMIDS):
        test.skipTest(
            f"{DUMP_HINT} — 기준 논문 {len(DB_PMIDS)}건 중 {state.team_papers}건만 DB에 있습니다"
        )


@cache
def embedding_model_available() -> bool:
    """bge-m3를 불러올 수 있는지 (sentence-transformers 설치 여부).

    requirements.txt에는 있지만 설치가 무거워서(torch 포함 약 2GB) 안 깔아 둔 PC가 있다.
    검색 방식은 질의를 임베딩해야 하므로 이 패키지가 없으면 돌 수 없다.
    """
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def require_search(test: unittest.TestCase) -> None:
    """검색 방식(mode='search')을 쓰는 테스트.

    질의 임베딩용 모델 + 비교할 논문 임베딩이 모두 있어야 결과가 나온다.
    """
    if not embedding_model_available():
        test.skipTest(
            "sentence-transformers가 설치되지 않았습니다 (검색 방식은 질의 임베딩이 필요) — "
            "pip install -r requirements.txt"
        )
    state = db_state()
    if state.error:
        test.skipTest(state.error)
    if state.embeddings < SEARCH_ITEMS:
        test.skipTest(
            f"{DUMP_HINT} — paper_embeddings가 {state.embeddings}건입니다 "
            f"(검색 결과 {SEARCH_ITEMS}건이 필요)"
        )


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
        require_team_papers(self)
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
        # 모든 엔드포인트 테스트가 '환자 1명인 간병인'을 기본으로 쓴다.
        self.caregiver_one = seed_user_id(self, SEED_EMAIL_ONE_PATIENT, expected_patients=1)
        self.client = TestClient(app)
        self.addCleanup(app.dependency_overrides.clear)

    def use(self, feed_execution):
        app.dependency_overrides[get_feed_execution] = lambda: feed_execution

    def get(self, user_id=None, **params):
        return self.client.get(
            f"/recommendations/{user_id or self.caregiver_one}", params=params
        )

    def assert_search(self, response):
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "search")
        self.assertTrue(body["query"])
        self.assertEqual(len(body["items"]), SEARCH_ITEMS)
        self.assertIsNotNone(body["items"][0]["relevance_score"])
        self.assertIsNone(body["items"][0]["headline"])

    def test_no_feed_execution_uses_search(self):
        require_search(self)
        self.assertIsNone(get_feed_execution())  # 교체 전 기본값
        self.assert_search(self.get())

    def test_fake_feed_execution_uses_ai(self):
        require_team_papers(self)
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
        require_team_papers(self)
        self.use(lambda context, _: make_result(context.request_id, DB_PMIDS))
        self.assertEqual(len(self.get(k=2).json()["items"]), 2)

    def test_feed_execution_error_falls_back_to_search(self):
        require_search(self)

        def broken(context, provider_input):
            raise RuntimeError("boom")

        self.use(broken)
        with self.assertLogs("app.api.recommendations", "WARNING") as logs:
            self.assert_search(self.get())
        self.assertIn("stage=feed_execution", logs.output[0])
        self.assertIn("RuntimeError", logs.output[0])
        self.assertNotIn("boom", logs.output[0])

    def test_no_usable_ai_items_falls_back_to_search(self):
        require_search(self)
        self.use(lambda context, _: make_result(context.request_id, [None, MISSING_PMID]))
        self.assert_search(self.get())

    def test_error_statuses(self):
        # 환자가 2명이면 patient_id를 지정하지 않는 요청은 400이다.
        caregiver_two = seed_user_id(self, SEED_EMAIL_TWO_PATIENTS, expected_patients=2)
        self.assertEqual(self.get(caregiver_two).status_code, 400)
        self.assertEqual(self.get(NO_USER).status_code, 404)
        self.assertEqual(self.get(patient_id=NO_USER).status_code, 404)
        self.assertEqual(self.get(k=6).status_code, 422)


if __name__ == "__main__":
    unittest.main()
