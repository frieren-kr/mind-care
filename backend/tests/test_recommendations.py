"""/recommendations 테스트. 기존 DB 통합 테스트는 실 DB/샘플 데이터가 필요하다.

OfflineFeedHandoffTests는 DB/RAG read를 Mock으로 대체하여 Handoff와 HTTP를 검증한다.

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
import json
import unittest
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from uuid import UUID, uuid4
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.ai.agent.outputs import StructuredAgentResult
from app.ai.agent.schemas import AgentContext
from app.api.recommendations import (
    GENERAL_QUERY,
    _feed_context_loader,
    backend_handoff,
    context_prepare,
    get_feed_execution,
)
from app.db.connection import get_connection
from app.schemas import PaperDetailResponse
from app.user_schemas import PatientContext, UserProfileContext
from main import app

# 시드 스크립트(scripts/seed_dev_profiles.py)가 만드는 샘플 간병인 이메일.
SEED_EMAIL_ONE_PATIENT = "dev-seed-1@example.com"   # 환자 1명
SEED_EMAIL_TWO_PATIENTS = "dev-seed-4@example.com"  # 환자 2명
SEED_EMAIL_LIKE = "dev-seed-%@example.com"
TEST_EMAIL_PREFIX = "test-reco-"  # 이 파일이 만들고 지우는 임시 사용자 (환자 0명)

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
        output={"schema_version": "2", "response_type": "feed", "items": [
            {"source_ref": ref, "personal_reason": f"이유 {i}", "category": "care"}
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
        self.assertEqual([i.personal_reason for i in items], ["이유 0", "이유 2", "이유 4"])
        for item, pmid in zip(items, DB_PMIDS):
            self.assertEqual(item.category, "care")
            self.assertIsNone(item.relevance_score)
            # DB에서 온 칸
            self.assertIn(pmid, item.pubmed_url)
            self.assertNotEqual(item.title, "AI가 본 제목")
            self.assertTrue(item.id)


class OfflineFeedHandoffTests(unittest.TestCase):
    """Actual Handoff/HTTP/Production Stage 1 with mocked reads, no DB or RAG I/O."""

    def setUp(self):
        from app.ai.agent.tests.test_feed_context_production_source import NOW, USER, P1, P2, patient, snapshot
        self.user, self.patient_ids = USER, (P1, P2)
        self.snapshot = snapshot
        rows = [patient(identifier) for identifier in self.patient_ids]
        self.profile = UserProfileContext(user_id=USER, name="Synthetic caregiver", generated_at=NOW,
            patients=[PatientContext(patient={"id": identifier, "caregiver_id": USER,
                "name": "Synthetic patient", "created_at": NOW, "updated_at": NOW,
                "symptoms": [f"signal {identifier.int}"], "interests": []}) for identifier in self.patient_ids])
        self.papers = {pmid: PaperDetailResponse(id=UUID(int=100+i), title=f"DB title {i}",
            pubmed_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", study_type="rct", evidence_level="2",
            summary_background=f"DB background {i}", summary_finding=f"DB finding {i}",
            summary_limitation=f"DB limitation {i}") for i, pmid in enumerate(DB_PMIDS)}
        guards = {
            "psycopg.connect": {"side_effect": AssertionError("Offline tests must not connect to DB")},
            "app.api.recommendations.user_functions.get_user_profile_context": {"return_value": self.profile},
            "app.api.recommendations.functions.get_paper_details_by_pmids": {"return_value": self.papers},
            "app.api.recommendations.search_by_queries": {},
        }
        mocks = {}
        for target, options in guards.items():
            patcher = patch(target, **options)
            mocks[target] = patcher.start()
            self.addCleanup(patcher.stop)
        self.details = mocks["app.api.recommendations.functions.get_paper_details_by_pmids"]
        self.search = mocks["app.api.recommendations.search_by_queries"]
        from app.ai.retrieval.search import EvidenceItem, EvidencePackage
        self.search.return_value = EvidencePackage(query="Synthetic search", evidence=[
            EvidenceItem(evidence_type="new_research", pmid=DB_PMIDS[0], title="Synthetic evidence",
                         relevance_score=0.7)])
        read = patch.object(_feed_context_loader.source, "read_snapshot", return_value=snapshot(rows))
        self.read_snapshot = read.start()
        self.addCleanup(read.stop)
        clock = patch.object(_feed_context_loader, "clock", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        previous_overrides = app.dependency_overrides.copy()
        self.addCleanup(lambda: (app.dependency_overrides.clear(),
                                 app.dependency_overrides.update(previous_overrides)))
        app.dependency_overrides[get_feed_execution] = lambda: None
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def get(self, **params):
        return self.client.get(f"/recommendations/{self.user}", params=params)

    def use(self, execution):
        app.dependency_overrides[get_feed_execution] = lambda: execution

    def test_handoff_keeps_db_title_summaries_grades_order_and_agent_reasons(self):
        pmids = [DB_PMIDS[0], None, DB_PMIDS[1], MISSING_PMID, DB_PMIDS[2]]
        result = make_result("offline", pmids)
        self.assertEqual(set(result.output.items[0].model_dump()), {"source_ref", "personal_reason", "category"})
        before = result.model_dump()
        items = backend_handoff(result)
        self.details.assert_called_once_with([DB_PMIDS[0], DB_PMIDS[1], MISSING_PMID, DB_PMIDS[2]])
        self.assertEqual([item.personal_reason for item in items], ["이유 0", "이유 2", "이유 4"])
        for i, item in enumerate(items):
            self.assertEqual(item.id, self.papers[DB_PMIDS[i]].id)
            self.assertEqual(item.title, f"DB title {i}")
            self.assertEqual((item.summary_background, item.summary_finding, item.summary_limitation),
                             (f"DB background {i}", f"DB finding {i}", f"DB limitation {i}"))
            self.assertEqual((item.category, item.evidence_grade), ("care", "B"))
            self.assertFalse(hasattr(item, "headline") or hasattr(item, "summary_bullets"))
            self.assertIsNone(item.relevance_score)
        self.assertEqual(result.model_dump(), before)

    def test_empty_feed_handoff_is_empty(self):
        self.assertEqual(backend_handoff(make_result("offline", [])), [])
        self.details.assert_called_once_with([])

    def test_http_ai_v2_retains_all_patient_scope_reason_and_category(self):
        received = []
        def execution(context, provider_input):
            received.append((context, json.loads(provider_input)))
            return make_result(context.request_id, DB_PMIDS)
        self.use(execution)
        response = self.get(patient_id=str(self.patient_ids[0]))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "ai")
        self.assertIsNone(body["queries"])
        self.assertEqual(body["patient_id"], str(self.patient_ids[0]))
        self.assertIsNone(received[0][0].patient_id)
        self.assertEqual(received[0][1]["feed_scope"], "caregiver_all")
        self.assertEqual(len(received[0][1]["patients"]), 2)
        self.read_snapshot.assert_called_once()
        self.assertEqual([item["personal_reason"] for item in body["items"]], ["이유 0", "이유 1", "이유 2"])
        for i, item in enumerate(body["items"]):
            self.assertEqual(item["title"], f"DB title {i}")
            self.assertEqual(item["summary_finding"], f"DB finding {i}")
            self.assertEqual(item["category"], "care")
            self.assertEqual(item["evidence_grade"], "B")
            self.assertNotIn("headline", item)
            self.assertNotIn("summary_bullets", item)
        self.search.assert_not_called()

    def test_http_k_preserves_recommendation_order(self):
        self.use(lambda context, _: make_result(context.request_id, DB_PMIDS))
        body = self.get(k=2).json()
        self.assertEqual(body["mode"], "ai")
        self.assertEqual([item["title"] for item in body["items"]], ["DB title 0", "DB title 1"])

    def test_http_failed_execution_keeps_search_fallback(self):
        self.use(Mock(side_effect=RuntimeError("PRIVATE execution failure")))
        with self.assertLogs("app.api.recommendations", "WARNING") as logs:
            body = self.get().json()
        self.assertEqual(body["mode"], "search")
        self.assertNotIn("PRIVATE", "\n".join(logs.output))
        self.assertIsNone(body["items"][0]["personal_reason"])
        self.assertIsNone(body["items"][0]["category"])
        self.search.assert_called_once()

    def test_http_unusable_ai_papers_keep_search_fallback(self):
        self.use(lambda context, _: make_result(context.request_id, [None, MISSING_PMID]))
        with self.assertLogs("app.api.recommendations", "WARNING"):
            body = self.get().json()
        self.assertEqual(body["mode"], "search")
        self.search.assert_called_once()

    def test_http_retired_feed_fields_are_rejected_then_use_search_fallback(self):
        def execution(context, _):
            payload = make_result(context.request_id, DB_PMIDS).model_dump()
            payload["output"]["items"][0].update(headline="Old title", summary_bullets=["Old summary"])
            return StructuredAgentResult.model_validate(payload)
        self.use(execution)
        with self.assertLogs("app.api.recommendations", "WARNING"):
            body = self.get().json()
        self.assertEqual(body["mode"], "search")
        self.assertNotIn("headline", body["items"][0])
        self.assertNotIn("summary_bullets", body["items"][0])
        self.search.assert_called_once()

    def test_http_default_search_is_unchanged(self):
        body = self.get().json()
        self.assertEqual(body["mode"], "search")
        self.assertEqual(len(body["queries"]), 2)
        self.assertNotIn("headline", body["items"][0])
        self.assertNotIn("summary_bullets", body["items"][0])
        self.read_snapshot.assert_not_called()

    def test_http_zero_patients_keeps_ai_cold_start_and_general_search(self):
        self.profile.patients = []
        self.read_snapshot.return_value = self.snapshot([], full=False)
        received = []
        def execution(context, provider_input):
            received.append(json.loads(provider_input))
            return make_result(context.request_id, DB_PMIDS)
        self.use(execution)
        self.assertEqual(self.get().json()["mode"], "ai")
        self.assertEqual(received[0]["patients"], [])
        self.use(None)
        body = self.get().json()
        self.assertEqual(body["mode"], "search")
        self.assertEqual(body["queries"], [GENERAL_QUERY])


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

    def assert_search(self, response, queries=1):
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "search")
        self.assertEqual(len(body["queries"]), queries)
        self.assertTrue(all(body["queries"]))
        self.assertEqual(len(body["items"]), SEARCH_ITEMS)
        self.assertIsNotNone(body["items"][0]["relevance_score"])
        self.assertNotIn("headline", body["items"][0])
        return body

    def patient_ids(self, user_id) -> list[str]:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM patient_profiles WHERE caregiver_id = %s"
                            " ORDER BY created_at, id", (user_id,))
                return [str(row[0]) for row in cur.fetchall()]

    def create_user_without_patients(self) -> UUID:
        def cleanup():
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM users WHERE email LIKE %s", (TEST_EMAIL_PREFIX + "%",))

        self.addCleanup(cleanup)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id",
                            ("테스트 간병인", f"{TEST_EMAIL_PREFIX}{uuid4().hex[:12]}@example.com"))
                return cur.fetchone()[0]

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
        self.assertIsNone(body["queries"])
        self.assertIsNone(body["patient_id"])
        self.assertEqual(len(body["items"]), 3)
        # 실 DB 스냅샷이 정식 Stage 1(context_prepare)을 통과했다.
        provider_input = json.loads(received[0])
        self.assertEqual(provider_input["feed_scope"], "caregiver_all")
        self.assertEqual(len(provider_input["patients"]), 1)
        for item, pmid in zip(body["items"], DB_PMIDS):
            self.assertNotIn("headline", item)
            self.assertNotIn("summary_bullets", item)
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

    def test_ai_uses_all_patients_even_with_patient_id(self):
        """AI 방식은 patient_id를 줘도 관리 환자 전체가 기준이다 (patient_id는 응답에만 적힌다)."""
        require_team_papers(self)
        caregiver_two = seed_user_id(self, SEED_EMAIL_TWO_PATIENTS, expected_patients=2)
        received = []

        def fake(context, provider_input):
            received.append((context, json.loads(provider_input)))
            return make_result(context.request_id, DB_PMIDS)

        self.use(fake)
        first = self.patient_ids(caregiver_two)[0]
        for params, expected in (({}, None), ({"patient_id": first}, first)):
            with self.subTest(params):
                body = self.get(caregiver_two, **params).json()
                self.assertEqual(body["mode"], "ai")
                self.assertEqual(body["patient_id"], expected)
                context, provider_input = received[-1]
                self.assertIsNone(context.patient_id)
                self.assertEqual(len(provider_input["patients"]), 2)

    def test_search_covers_all_patients_or_one(self):
        """검색 방식: 환자 2명 + patient_id 생략은 200(검색어 2개), 주면 그 환자 검색어 하나."""
        require_search(self)
        caregiver_two = seed_user_id(self, SEED_EMAIL_TWO_PATIENTS, expected_patients=2)
        both = self.assert_search(self.get(caregiver_two), queries=2)
        self.assertIsNone(both["patient_id"])

        second = self.patient_ids(caregiver_two)[1]
        one = self.assert_search(self.get(caregiver_two, patient_id=second), queries=1)
        self.assertEqual(one["patient_id"], second)
        self.assertEqual(one["queries"], [both["queries"][1]])

    def test_no_patients_search_uses_general_query(self):
        """환자 0명도 404가 아니라 응답한다 (검색 방식은 일반 검색어 하나)."""
        require_search(self)
        body = self.assert_search(self.get(self.create_user_without_patients()))
        self.assertEqual(body["queries"], [GENERAL_QUERY])
        self.assertIsNone(body["patient_id"])

    def test_no_patients_ai_is_cold_start(self):
        """환자 0명: AI 방식은 빈 환자 목록(cold start)으로 끝까지 돈다."""
        require_team_papers(self)
        received = []

        def fake(context, provider_input):
            received.append(json.loads(provider_input))
            return make_result(context.request_id, DB_PMIDS)

        self.use(fake)
        body = self.get(self.create_user_without_patients()).json()
        self.assertEqual(body["mode"], "ai")
        self.assertEqual(received[0]["patients"], [])
        self.assertIsNone(received[0]["caregiver"])

    def test_error_statuses(self):
        caregiver_two = seed_user_id(self, SEED_EMAIL_TWO_PATIENTS, expected_patients=2)
        self.assertEqual(self.get(NO_USER).status_code, 404)
        self.assertEqual(self.get(patient_id=NO_USER).status_code, 404)
        # 다른 간병인의 환자도 404.
        self.assertEqual(
            self.get(patient_id=self.patient_ids(caregiver_two)[0]).status_code, 404)
        self.assertEqual(self.get(k=6).status_code, 422)


class ProductionStage1Tests(unittest.TestCase):
    """정식 Stage 1(get_feed_context_snapshot → Source → Loader → Prepare)을 실 DB로 돌린다."""

    def test_sample_caregivers_pass_without_identifiers(self):
        state = db_state()
        if state.error:
            self.skipTest(state.error)
        if not state.seed_users:
            self.skipTest(SEED_HINT)

        for email, (user_id, patient_count) in sorted(state.seed_users.items()):
            with self.subTest(email), get_connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT name, phone_number FROM users WHERE id = %s", (user_id,))
                identifiers = [email, str(user_id), *cur.fetchone()]
                cur.execute("SELECT id, name FROM patient_profiles WHERE caregiver_id = %s",
                            (user_id,))
                patients = cur.fetchall()
                patient_ids = [row[0] for row in patients]
                identifiers += [str(v) for row in patients for v in row]
                cur.execute("SELECT DISTINCT hospital_name FROM medical_visits"
                            " WHERE patient_id = ANY(%s)", (patient_ids,))
                identifiers += [row[0] for row in cur.fetchall()]
                cur.execute("SELECT count(*) FROM care_logs WHERE user_id = %s"
                            " AND logged_at >= now() - interval '29 days'", (user_id,))
                has_logs = cur.fetchone()[0] > 0
                cur.execute("SELECT count(*) FROM profile_summaries WHERE user_id = %s"
                            " AND period_end < current_date - 1", (user_id,))
                has_summary = cur.fetchone()[0] > 0

                context = AgentContext(request_id="stage1", user_id=user_id, patient_id=None)
                full = _feed_context_loader(context, recent_days=30)
                provider_json = context_prepare(context)

                # 관리 환자 전원이 빠짐없이 들어간다.
                coverage = full.managed_patient_profiles.coverage
                self.assertEqual((coverage.total_count, coverage.included_count),
                                 (patient_count, patient_count))
                self.assertEqual(len(json.loads(provider_json)["patients"]), patient_count)
                # 돌봄 기록·월간 요약 (python -m scripts.seed_dev_care_logs 를 돌린 DB라면 들어 있다).
                if has_logs:
                    self.assertGreater(full.recent_care_context.care_logs.coverage.total_count, 0)
                if has_summary:
                    self.assertIsNotNone(full.long_term_summary)
                # 이름·이메일·전화번호·병원명·raw id가 전체 Context에도, LLM 입력에도 없다.
                for text in (full.model_dump_json(), provider_json):
                    for value in filter(None, identifiers):
                        self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main()
