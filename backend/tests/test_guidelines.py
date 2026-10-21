"""가이드라인 저장·검색 함수 테스트. 실 DB가 떠 있고 008 마이그레이션이 적용돼 있어야 한다.

실행 (backend/ 에서):  python -m unittest tests.test_guidelines

임베딩 모델을 부르지 않는다. 가짜 1024차원 벡터를 쓰고, 이 파일이 넣은 조각
(source_url이 TEST_URL로 시작)은 끝나면 지운다.
"""
import math
import unittest
from uuid import uuid4

from app.db import functions
from app.db.connection import get_connection
from app.schemas import EMBEDDING_DIM, GuidelineChunkIn

TEST_URL = "https://test.invalid/guideline-test/"


def vector(*hot: int) -> list[float]:
    """hot에 준 자리만 값이 있는 단위 벡터. vector(0)과의 코사인 유사도가 1/sqrt(len(hot))이 된다."""
    return [1 / math.sqrt(len(hot)) if i in hot else 0.0 for i in range(EMBEDDING_DIM)]


def chunk(index: int, embedding: list[float], **changed) -> GuidelineChunkIn:
    fields = {
        "doc_title": "테스트 가이드라인", "section": f"{index}장", "chunk_index": index,
        "content": f"조각 {index} 본문", "topic": "수면장애 관리", "source": "테스트 학회",
        "source_url": TEST_URL + "doc-a", "published_year": 2024, "language": "ko",
        "tags": ["수면장애"], "embedding": embedding, "embedding_model": "fake-model",
    }
    return GuidelineChunkIn(**{**fields, **changed})


def _rows(where: str = "") -> list[tuple]:
    with get_connection() as conn:
        return conn.execute(
            f"SELECT id, chunk_index, content, embedding IS NULL FROM guideline_knowledge "
            f"WHERE source_url LIKE %s {where} ORDER BY chunk_index", (TEST_URL + "%",)
        ).fetchall()


def _delete_test_rows() -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM guideline_knowledge WHERE source_url LIKE %s", (TEST_URL + "%",))


class GuidelineFunctionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            _delete_test_rows()
        except Exception as exc:
            raise unittest.SkipTest(
                f"DB에 접속할 수 없거나 008 마이그레이션 전입니다 — README의 008 안내 참고 ({type(exc).__name__})"
            )

    def setUp(self):
        self.addCleanup(_delete_test_rows)
        # vector(0)으로 검색하면 유사도가 1.0 → 0.707 → 0.577 순서가 된다.
        self.saved = functions.save_guidelines([
            chunk(2, vector(0, 1, 2)), chunk(0, vector(0)), chunk(1, vector(0, 1)),
        ])
        self.ids = {index: row_id for row_id, index, *_ in _rows()}

    def search(self, k: int = 50) -> list:
        """이 테스트가 넣은 조각만 걸러서 본다 (DB에 실제 가이드라인이 있어도 흔들리지 않게)."""
        mine = set(self.ids.values())
        return [r for r in functions.search_similar_guidelines(vector(0), k=k) if r.id in mine]

    def test_save_returns_count_and_stores_chunks(self):
        self.assertEqual(self.saved, 3)
        self.assertEqual([(index, content) for _, index, content, _ in _rows()],
                         [(0, "조각 0 본문"), (1, "조각 1 본문"), (2, "조각 2 본문")])

    def test_saving_same_key_updates_instead_of_inserting(self):
        saved = functions.save_guidelines([chunk(1, vector(5), content="고친 본문", section="새 절", tags=[])])

        self.assertEqual(saved, 1)
        self.assertEqual(len(_rows()), 3)  # 줄이 늘지 않는다
        updated = functions.get_guidelines_by_ids([self.ids[1]])[0]  # id도 그대로다
        self.assertEqual((updated.content, updated.section, updated.tags), ("고친 본문", "새 절", []))
        # 임베딩도 같이 바뀌어서 vector(0)과의 유사도가 0이 된다 → 맨 뒤로 간다.
        # 실제 가이드라인이 DB에 많으면 유사도 0인 조각은 검색 결과(k개)에서 아예 빠질 수도 있다.
        found = [r.id for r in self.search()]
        self.assertIn(found, ([self.ids[0], self.ids[2], self.ids[1]], [self.ids[0], self.ids[2]]))

    def test_same_chunk_index_in_another_document_is_a_separate_row(self):
        functions.save_guidelines([chunk(0, vector(7), source_url=TEST_URL + "doc-b")])
        self.assertEqual(len(_rows()), 4)

    def test_duplicate_key_in_one_batch_keeps_first(self):
        with self.assertLogs("app.db.functions", "WARNING"):
            saved = functions.save_guidelines([
                chunk(9, vector(9), content="첫 번째"), chunk(9, vector(9), content="두 번째"),
            ])
        self.assertEqual(saved, 1)
        self.assertEqual([content for _, index, content, _ in _rows() if index == 9], ["첫 번째"])

    def test_search_orders_by_cosine_similarity(self):
        results = self.search()

        self.assertEqual([r.id for r in results], [self.ids[0], self.ids[1], self.ids[2]])
        for result, expected in zip(results, [1.0, 1 / math.sqrt(2), 1 / math.sqrt(3)]):
            self.assertAlmostEqual(result.score, expected, places=5)
        self.assertEqual(functions.search_similar_guidelines(vector(0), k=0), [])
        self.assertLessEqual(len(functions.search_similar_guidelines(vector(0), k=2)), 2)

    def test_search_skips_rows_without_embedding(self):
        with get_connection() as conn:  # 임베딩 없이 들어간 줄 (save_guidelines로는 만들 수 없어 직접 만든다)
            conn.execute("UPDATE guideline_knowledge SET embedding = NULL WHERE id = %s", (self.ids[0],))

        self.assertEqual([r.id for r in self.search()], [self.ids[1], self.ids[2]])

    def test_get_by_ids_keeps_input_order_and_drops_missing(self):
        wanted = [self.ids[2], uuid4(), self.ids[0], self.ids[1]]

        chunks = functions.get_guidelines_by_ids(wanted)

        self.assertEqual([c.id for c in chunks], [self.ids[2], self.ids[0], self.ids[1]])
        first = chunks[1]
        self.assertEqual(
            (first.doc_title, first.section, first.chunk_index, first.content, first.source, first.source_url,
             first.published_year, first.language, first.topic, first.tags),
            ("테스트 가이드라인", "0장", 0, "조각 0 본문", "테스트 학회", TEST_URL + "doc-a",
             2024, "ko", "수면장애 관리", ["수면장애"]),
        )
        self.assertFalse(hasattr(first, "embedding"))
        self.assertEqual(functions.get_guidelines_by_ids([]), [])

    def test_wrong_dimension_and_blank_content_are_rejected_before_db(self):
        for label, make in [
            ("768차원", lambda: chunk(20, [0.1] * 768)),
            ("1536차원", lambda: chunk(20, [0.1] * 1536)),
            ("공백 본문", lambda: chunk(20, vector(3), content="   \n")),
            ("빈 본문", lambda: chunk(20, vector(3), content="")),
        ]:
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    functions.save_guidelines([chunk(21, vector(4)), make()])
        self.assertEqual(len(_rows()), 3)  # 같은 배치의 멀쩡한 조각(21번)도 저장되지 않는다
        with self.assertRaises(ValueError):
            functions.search_similar_guidelines([0.1] * 768)


if __name__ == "__main__":
    unittest.main()
