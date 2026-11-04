"""피드 저장본 함수(save_feed_batch / get_latest_feed_batch / get_recent_feed_papers) 테스트 (실 DB). 박주현 담당.

실행 (backend/ 에서):  python -m unittest tests.test_feed_cache

DB가 꺼져 있거나, 009가 적용되지 않았거나, 논문이 3편 미만이면 **실패가 아니라 skip** 한다.
테스트용 사용자('test-feed-cache-*@example.com')를 만들고 끝나면 지운다 (feed_items는 CASCADE로 같이 지워진다).
"""
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.connection import get_connection
from app.db.feed_read import get_latest_feed_batch, get_recent_feed_papers, save_feed_batch
from app.feed_schemas import FeedItemIn

EMAIL_PREFIX = "test-feed-cache-"
DAYS3 = timedelta(days=3)


def setUpModule():
    global PAPERS
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.columns"
                        " WHERE table_name = 'feed_items' AND column_name = 'batch_id'")
            if cur.fetchone() is None:
                raise unittest.SkipTest("009_feed_items_cache.sql이 적용되지 않았습니다")
            cur.execute("SELECT id FROM papers ORDER BY id LIMIT 3")
            PAPERS = [row[0] for row in cur.fetchall()]
    except unittest.SkipTest:
        raise
    except Exception as err:
        raise unittest.SkipTest(f"DB를 읽을 수 없습니다 ({type(err).__name__}) — docker compose up -d")
    if len(PAPERS) < 3:
        raise unittest.SkipTest("논문이 3편 미만입니다 — 팀 덤프 복원 필요")


class _UserTestCase(unittest.TestCase):
    """테스트용 사용자를 만들고 끝나면 지운다."""

    def setUp(self):
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id",
                        ("테스트 간병인", f"{EMAIL_PREFIX}{uuid4().hex[:12]}@example.com"))
            self.user = cur.fetchone()[0]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE email LIKE %s", (EMAIL_PREFIX + "%",))


class FeedCacheDbTests(_UserTestCase):
    def save(self, papers, reason="이유"):
        return save_feed_batch(self.user, [
            FeedItemIn(paper_id=p, personal_reason=f"{reason} {i}", category="care") for i, p in enumerate(papers)])

    def latest(self, at, max_age=DAYS3):
        return get_latest_feed_batch(self.user, max_age=max_age, reference_time=at)

    def rows(self):
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM feed_items WHERE user_id = %s", (self.user,))
            return cur.fetchone()[0]

    def test_save_then_latest_in_rank_order_with_details(self):
        saved = self.save([PAPERS[2], PAPERS[0]])
        batch = self.latest(saved.generated_at + timedelta(days=2))
        self.assertEqual((batch.batch_id, batch.generated_at), (saved.batch_id, saved.generated_at))
        self.assertEqual([i.rank for i in batch.items], [1, 2])
        self.assertEqual([i.paper.id for i in batch.items], [PAPERS[2], PAPERS[0]])
        self.assertEqual([i.personal_reason for i in batch.items], ["이유 0", "이유 1"])
        self.assertTrue(all(i.category == "care" and i.paper.title and not i.is_saved for i in batch.items))

    def test_period_and_newest_batch(self):
        old = self.save([PAPERS[0]])
        self.assertIsNone(self.latest(old.generated_at + DAYS3))  # 기간이 지났다 (경계 포함)
        self.assertIsNone(get_latest_feed_batch(uuid4(), max_age=DAYS3, reference_time=old.generated_at))
        new = self.save([PAPERS[1]])
        self.assertEqual(self.latest(new.generated_at).batch_id, new.batch_id)

    def test_bookmark_carries_over_and_old_rows_stay(self):
        first = self.save([PAPERS[0], PAPERS[1]])
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE feed_items SET is_saved = true WHERE batch_id = %s AND paper_id = %s",
                        (first.batch_id, PAPERS[0]))
        second = self.save([PAPERS[1], PAPERS[0], PAPERS[2]])
        batch = self.latest(second.generated_at)
        self.assertEqual({i.paper.id: i.is_saved for i in batch.items},
                         {PAPERS[0]: True, PAPERS[1]: False, PAPERS[2]: False})
        self.assertEqual(self.rows(), 5)  # 예전 묶음(북마크 행 포함)은 지우지 않는다

    def test_bad_paper_saves_nothing(self):
        with self.assertRaises(ValueError):
            save_feed_batch(self.user, [FeedItemIn(paper_id=PAPERS[0]), FeedItemIn(paper_id=uuid4())])
        self.assertEqual(self.rows(), 0)
        with self.assertRaises(ValueError):
            save_feed_batch(self.user, [])

    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            get_latest_feed_batch(self.user, max_age=DAYS3, reference_time=datetime(2026, 10, 15))
        with self.assertRaises(ValueError):
            get_latest_feed_batch(self.user, max_age=timedelta(0), reference_time=datetime.now(timezone.utc))


class RecentFeedPapersDbTests(_UserTestCase):
    """get_recent_feed_papers(). 묶음 시각은 저장 뒤 직접 고친다 (실행 시각에 흔들리지 않게)."""

    NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

    def save_at(self, papers, at, user=None):
        saved = save_feed_batch(user or self.user, [FeedItemIn(paper_id=p) for p in papers])
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("UPDATE feed_items SET created_at = %s WHERE batch_id = %s", (at, saved.batch_id))

    def recent(self, **kw):
        return get_recent_feed_papers(self.user, reference_time=self.NOW, **kw)

    def test_period_in_out_and_future_excluded(self):
        self.save_at([PAPERS[0]], self.NOW - timedelta(days=7))    # 경계: 제외
        self.save_at([PAPERS[1]], self.NOW - timedelta(days=6))
        self.save_at([PAPERS[2]], self.NOW + timedelta(minutes=1))  # 미래: 제외
        self.assertEqual([r.paper_id for r in self.recent()], [PAPERS[1]])
        self.assertEqual([r.paper_id for r in self.recent(days=8)], [PAPERS[1], PAPERS[0]])

    def test_same_paper_in_many_batches_is_one_row(self):
        self.save_at([PAPERS[0], PAPERS[1]], self.NOW - timedelta(days=5))
        self.save_at([PAPERS[0]], self.NOW - timedelta(days=1))
        rows = self.recent()
        self.assertEqual([(r.paper_id, r.times_recommended) for r in rows], [(PAPERS[0], 2), (PAPERS[1], 1)])
        self.assertEqual(rows[0].last_recommended_at, self.NOW - timedelta(days=1))
        self.assertTrue(all(r.title for r in rows))
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT source, external_id FROM papers WHERE id = %s", (PAPERS[0],))
            source, external_id = cur.fetchone()
        self.assertEqual(rows[0].pmid, external_id if source == "pubmed" else None)

    def test_limit(self):
        for i, p in enumerate(PAPERS):
            self.save_at([p], self.NOW - timedelta(days=i + 1))
        self.assertEqual([r.paper_id for r in self.recent(limit=2)], PAPERS[:2])

    def test_other_users_history_is_not_mixed(self):
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id",
                        ("다른 간병인", f"{EMAIL_PREFIX}{uuid4().hex[:12]}@example.com"))
            other = cur.fetchone()[0]
        self.save_at([PAPERS[0]], self.NOW - timedelta(days=1), user=other)
        self.save_at([PAPERS[1]], self.NOW - timedelta(days=1))
        self.assertEqual([r.paper_id for r in self.recent()], [PAPERS[1]])
        self.assertEqual(get_recent_feed_papers(uuid4(), reference_time=self.NOW), [])

    def test_bad_arguments_and_default_now(self):
        for kw in ({"days": 0}, {"limit": 0}, {"days": True}, {"reference_time": datetime(2026, 10, 20)}):
            with self.assertRaises(ValueError):
                get_recent_feed_papers(self.user, **kw)
        save_feed_batch(self.user, [FeedItemIn(paper_id=PAPERS[0])])  # created_at = now()
        self.assertEqual([r.paper_id for r in get_recent_feed_papers(self.user)], [PAPERS[0]])


if __name__ == "__main__":
    unittest.main()
