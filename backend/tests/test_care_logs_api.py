"""앱 돌봄 기록 API (추가·최근 목록). DB 함수만 대체하고 실제 라우터·입력 검증을 실행한다."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.users import router
from app.db import feed_read, user_functions
from app.feed_schemas import FeedReadCareLog, FeedReadCollection
from tests.auth_helpers import allow_business_test_requests


class CareLogApiTests(unittest.TestCase):
    def setUp(self):
        self.user_id = uuid4()
        self.patient_id = uuid4()
        self.other_patient_id = uuid4()

        def is_caregiver_of(user_id, patient_id):
            return user_id == self.user_id and patient_id == self.patient_id

        patches = [
            patch.object(user_functions, "is_caregiver_of", side_effect=is_caregiver_of),
            patch.object(feed_read, "save_care_log", return_value=uuid4()),
            patch.object(user_functions, "get_recent_care_logs"),
            patch("psycopg.connect", side_effect=AssertionError("오프라인 테스트: DB 연결 금지")),
        ]
        _, self.save, self.recent, _ = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.app = FastAPI()
        self.app.include_router(router)
        allow_business_test_requests(self, self.app)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.url = f"/users/{self.user_id}/patients/{self.patient_id}/care-logs"

    # ---------- 추가 ----------

    def test_create_saves_patient_care_log(self):
        at = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(microsecond=0)
        response = self.client.post(self.url, json={"content": "  새벽 3시에 깨서 거실을 서성이셨다.  ",
                                                    "mood_tag": "지침", "logged_at": at.isoformat()})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["content"], "새벽 3시에 깨서 거실을 서성이셨다.")  # 앞뒤 공백 제거
        self.assertEqual(body["mood_tag"], "지침")
        self.assertEqual(datetime.fromisoformat(body["logged_at"]), at)
        kwargs = self.save.call_args.kwargs
        self.assertEqual(kwargs["log_type"], "patient_care")  # 챗봇 Tool이 읽는 종류
        self.assertEqual((kwargs["user_id"], kwargs["patient_id"]), (self.user_id, self.patient_id))
        self.assertEqual(kwargs["logged_at"], at)

    def test_create_without_time_uses_now_and_returns_it(self):
        before = datetime.now(timezone.utc)
        response = self.client.post(self.url, json={"content": "저녁 약을 잘 드셨다."})
        self.assertEqual(response.status_code, 201)
        saved_at = self.save.call_args.kwargs["logged_at"]
        self.assertLessEqual(before, saved_at)
        self.assertEqual(datetime.fromisoformat(response.json()["logged_at"]), saved_at)
        self.assertIsNone(response.json()["mood_tag"])

    def test_create_rejects_bad_bodies_without_writing(self):
        now = datetime.now(timezone.utc)
        bodies = [
            {},
            {"content": "   "},
            {"content": "가" * 1001},
            {"content": "기록", "mood_tag": "화남"},
            {"content": "기록", "logged_at": (now + timedelta(hours=1)).isoformat()},
            {"content": "기록", "logged_at": (now - timedelta(days=91)).isoformat()},
            {"content": "기록", "logged_at": "2026-11-18T09:00:00"},  # 시간대 없음
            {"content": "기록", "log_type": "caregiver_selfcare"},  # 종류는 서버가 정한다
            {"content": 3},
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.client.post(self.url, json=body).status_code, 422)
        self.save.assert_not_called()

    def test_create_for_other_patient_is_404(self):
        url = f"/users/{self.user_id}/patients/{self.other_patient_id}/care-logs"
        self.assertEqual(self.client.post(url, json={"content": "기록"}).status_code, 404)
        self.save.assert_not_called()

    def test_create_when_patient_removed_meanwhile_is_404(self):
        self.save.side_effect = ValueError("이 간병인의 환자가 아닙니다")
        self.assertEqual(self.client.post(self.url, json={"content": "기록"}).status_code, 404)

    # ---------- 목록 ----------

    def test_list_returns_recent_logs_from_chatbot_query(self):
        at = datetime.now(timezone.utc) - timedelta(days=1)
        self.recent.return_value = FeedReadCollection[FeedReadCareLog](
            items=[FeedReadCareLog(user_id=self.user_id, patient_id=self.patient_id, log_type="patient_care",
                                   content="해 질 녘에 불안해하셨다.", mood_tag="불안", logged_at=at)],
            total_count=12,
        )
        response = self.client.get(self.url, params={"days": 7, "limit": 1})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total_count"], 12)
        self.assertEqual(body["items"][0]["content"], "해 질 녘에 불안해하셨다.")
        self.assertEqual(body["items"][0]["mood_tag"], "불안")
        kwargs = self.recent.call_args.kwargs
        self.assertEqual(kwargs["limit"], 1)
        self.assertAlmostEqual((kwargs["end_at"] - kwargs["start_at"]).total_seconds(),
                               timedelta(days=7, minutes=10).total_seconds(), delta=5)

    def test_list_for_other_patient_is_404(self):
        self.recent.side_effect = LookupError("환자 돌봄 기록을 조회할 수 없습니다.")
        url = f"/users/{self.user_id}/patients/{self.other_patient_id}/care-logs"
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_list_rejects_bad_ranges(self):
        for params in ({"days": 0}, {"days": 91}, {"limit": 0}, {"limit": 31}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(self.url, params=params).status_code, 422)
        self.recent.assert_not_called()

    # ---------- 기간 조회 (달력) ----------

    def _fake_db(self, logs):
        """get_recent_care_logs 대신: [start_at, end_at), 최신순, limit건 + 자르기 전 건수"""
        def recent(*, user_id, patient_id, start_at, end_at, limit):
            hits = sorted((l for l in logs if start_at <= l.logged_at < end_at), key=lambda l: l.logged_at, reverse=True)
            return FeedReadCollection[FeedReadCareLog](items=hits[:limit], total_count=len(hits))
        self.recent.side_effect = recent

    def _log(self, at, content):
        return FeedReadCareLog(user_id=self.user_id, patient_id=self.patient_id, log_type="patient_care",
                               content=content, mood_tag=None, logged_at=at)

    def test_range_returns_every_log_in_kst_dates_across_pages(self):
        kst = timezone(timedelta(hours=9))
        inside = [self._log(datetime(2026, 11, 1, 8, 0, tzinfo=kst) + timedelta(hours=7 * i), f"기록 {i}")
                  for i in range(75)]  # 11/1 08:00부터 약 22일, 한 번에 30건씩 3번 나눠 불러야 한다
        same_time = [self._log(datetime(2026, 11, 20, 9, 0, tzinfo=kst), f"같은 시각 {i}") for i in range(2)]
        outside = [self._log(datetime(2026, 10, 31, 23, 59, tzinfo=kst), "전달 마지막 밤"),
                   self._log(datetime(2026, 12, 1, 0, 0, tzinfo=kst), "다음 달 첫 시각")]
        self._fake_db(inside + same_time + outside)
        response = self.client.get(self.url, params={"start": "2026-11-01", "end": "2026-11-30"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        contents = [i["content"] for i in body["items"]]
        self.assertEqual(len(contents), 77)
        self.assertEqual(len(set(contents)), 77)  # 겹쳐 부른 경계에서 중복 없음
        self.assertNotIn("전달 마지막 밤", contents)
        self.assertNotIn("다음 달 첫 시각", contents)
        self.assertEqual(body["total_count"], 77)
        self.assertGreaterEqual(self.recent.call_count, 3)

    def test_range_requires_both_dates_and_limited_span(self):
        for params in ({"start": "2026-11-01"}, {"end": "2026-11-30"}, {"start": "2026-11-30", "end": "2026-11-01"},
                       {"start": "2026-10-01", "end": "2026-11-30"}, {"start": "2026-13-01", "end": "2026-11-30"}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(self.url, params=params).status_code, 422)
        self.recent.assert_not_called()

    # ---------- 인증 ----------

    def test_requires_login(self):
        self.app.dependency_overrides.clear()  # 실제 인증 의존성
        self.assertEqual(self.client.post(self.url, json={"content": "기록"}).status_code, 401)
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.save.assert_not_called()
        self.recent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
