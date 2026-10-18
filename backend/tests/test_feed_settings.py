"""피드 설정 API·조회 전파·입력 검증. DB I/O만 대체하고 실제 라우터/도메인 함수를 실행한다."""
import unittest
from datetime import datetime
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.users import router
from app.db import user_functions
from app.user_schemas import APP_TIMEZONE


class FeedSettingsTests(unittest.TestCase):
    def setUp(self):
        self.user_id = uuid4()
        now = datetime.now(APP_TIMEZONE)
        self.row = dict(id=self.user_id, name="테스트 간병인", email="feed-settings@example.com",
                        phone_number=None, feed_refresh_days=3, created_at=now,
                        updated_at=now, patient_ids=[])

        def read(query, params):
            if params["user_id"] != self.user_id:
                return None
            return {**self.row, "user_id": self.user_id}

        def update(table, key_column, key, fields):
            self.assertEqual((table, key_column), ("users", "id"))
            if key != self.user_id:
                return None
            self.row.update(fields)
            return self.row.copy()

        patches = [
            patch.object(user_functions, "_fetch_one", side_effect=read),
            patch.object(user_functions, "_update_row", side_effect=update),
            patch.object(user_functions, "get_caregiver_profile", return_value=None),
            patch.object(user_functions, "list_patients", return_value=[]),
            patch("psycopg.connect", side_effect=AssertionError("오프라인 테스트: DB 연결 금지")),
        ]
        self.read, self.update, *_ = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.url = f"/users/{self.user_id}/feed-settings"

    def test_default_and_patch_are_visible_in_all_user_responses(self):
        before = self.client.get(self.url)
        self.assertEqual(before.status_code, 200)
        self.assertEqual(before.json(), {"user_id": str(self.user_id), "feed_refresh_days": 3})
        for days in (1, 5, 7):
            with self.subTest(days=days):
                response = self.client.patch(self.url, json={"feed_refresh_days": days})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"user_id": str(self.user_id), "feed_refresh_days": days})
                for url in (self.url, f"/feed/{self.user_id}", f"/feed_user/{self.user_id}"):
                    body = self.client.get(url)
                    self.assertEqual(body.status_code, 200)
                    self.assertEqual(body.json()["feed_refresh_days"], days)
                    self.assertNotIn("email", body.json())
                    self.assertNotIn("phone_number", body.json())
                self.assertEqual(user_functions.get_user(self.user_id).feed_refresh_days, days)
        self.assertEqual(self.row["name"], "테스트 간병인")
        self.assertEqual(self.row["email"], "feed-settings@example.com")

    def test_bad_bodies_never_write(self):
        bodies = [{}, {"feed_refresh_days": 5, "name": "다른 이름"}]
        bodies += [{"feed_refresh_days": v} for v in (None, 0, 8, -1, 1.5, 3.0, True, False, "5", [], {})]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.client.patch(self.url, json=body).status_code, 422)
        self.update.assert_not_called()
        self.assertEqual(self.row["feed_refresh_days"], 3)

    def test_missing_user_is_404_and_invalid_uuid_is_422(self):
        missing = f"/users/{uuid4()}/feed-settings"
        for url, status in ((missing, 404), ("/users/invalid/feed-settings", 422)):
            self.assertEqual(self.client.get(url).status_code, status)
            self.assertEqual(self.client.patch(url, json={"feed_refresh_days": 5}).status_code, status)
        self.assertEqual(self.row["feed_refresh_days"], 3)

    def test_direct_function_validates_before_db_write(self):
        for value in (None, 0, 8, True, "5", 3.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                user_functions.update_user_feed_settings(self.user_id, {"feed_refresh_days": value})
        self.update.assert_not_called()
        self.assertIsNone(user_functions.get_user_feed_settings("invalid"))
        self.assertIsNone(user_functions.update_user_feed_settings("invalid", {"feed_refresh_days": 5}))


if __name__ == "__main__":
    unittest.main()
