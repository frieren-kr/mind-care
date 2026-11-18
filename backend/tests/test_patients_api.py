"""돌보는 분 추가·수정 API. DB I/O만 대체하고 실제 라우터·입력 검증·도메인 함수를 실행한다."""
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.users import router
from app.db import user_functions
from app.user_schemas import APP_TIMEZONE
from tests.auth_helpers import allow_business_test_requests


class PatientApiTests(unittest.TestCase):
    def setUp(self):
        self.user_id = uuid4()
        self.other_user_id = uuid4()
        self.now = datetime.now(APP_TIMEZONE)
        self.patients: dict[UUID, dict] = {}
        self.mine = self._row(self.user_id, name="어머니", dementia_stage="경도", symptoms=["수면장애"])
        self.others = self._row(self.other_user_id, name="다른 분")

        def insert(table, values):
            self.assertEqual(table, "patient_profiles")
            return self._row(**values)

        def update(table, key_column, key, fields):
            self.assertEqual((table, key_column), ("patient_profiles", "id"))
            row = self.patients.get(key)
            if row is None:
                return None
            row.update(fields, updated_at=self.now)
            return row.copy()

        def fetch_one(query, params):
            # is_caregiver_of()와 get_patient()만 이 테스트에서 부른다
            if "EXISTS" in query:
                row = self.patients.get(params["patient_id"])
                return {"ok": row is not None and row["caregiver_id"] == params["user_id"]}
            row = self.patients.get(params["id"])
            return row.copy() if row else None

        patches = [
            patch.object(user_functions, "_insert_row", side_effect=insert),
            patch.object(user_functions, "_update_row", side_effect=update),
            patch.object(user_functions, "_fetch_one", side_effect=fetch_one),
            patch("psycopg.connect", side_effect=AssertionError("오프라인 테스트: DB 연결 금지")),
        ]
        self.insert, self.update, *_ = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.app = FastAPI()
        self.app.include_router(router)
        allow_business_test_requests(self, self.app)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.create_url = f"/users/{self.user_id}/patients"

    def _row(self, caregiver_id, **fields):
        row = dict(id=uuid4(), caregiver_id=caregiver_id, name=None, dementia_stage=None, diagnosis_date=None,
                   symptoms=[], interests=[], created_at=self.now, updated_at=self.now)
        row.update(fields)
        self.patients[row["id"]] = row
        return row

    def _update_url(self, patient_id):
        return f"/users/{self.user_id}/patients/{patient_id}"

    # ---------- 추가 ----------

    def test_create_uses_logged_in_user_as_caregiver(self):
        body = {"name": " 아버지 ", "dementia_stage": "중증", "diagnosis_date": "2024-03-02",
                "symptoms": ["배회", " 배회 ", "낙상 위험"], "interests": ["운동"]}
        response = self.client.post(self.create_url, json=body)
        self.assertEqual(response.status_code, 201)
        saved = response.json()
        self.assertEqual(saved["caregiver_id"], str(self.user_id))
        self.assertEqual(saved["name"], "아버지")  # 앞뒤 공백 제거
        self.assertEqual(saved["symptoms"], ["배회", "낙상 위험"])  # 중복 제거
        self.assertEqual(saved["diagnosis_date"], "2024-03-02")
        values = self.insert.call_args.args[1]
        self.assertEqual(values["caregiver_id"], self.user_id)

    def test_create_with_empty_body_saves_empty_lists(self):
        response = self.client.post(self.create_url, json={})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["symptoms"], [])
        self.assertEqual(response.json()["interests"], [])

    def test_create_rejects_bad_bodies_without_writing(self):
        future = (date.today() + timedelta(days=3)).isoformat()
        bodies = [
            {"caregiver_id": str(self.other_user_id)},  # 다른 사람 계정에 넣으려는 시도
            {"caregiver_id": str(self.user_id)},  # 본인 id여도 본문으로는 받지 않는다
            {"name": "어머니", "unknown": 1},
            {"dementia_stage": "아주 심함"},
            {"diagnosis_date": future},
            {"diagnosis_date": "2024-13-40"},
            {"symptoms": "수면장애"},
            {"symptoms": [1, 2]},
            {"name": 3},
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(self.client.post(self.create_url, json=body).status_code, 422)
        self.insert.assert_not_called()

    # ---------- 수정 ----------

    def test_update_changes_only_sent_fields(self):
        response = self.client.patch(self._update_url(self.mine["id"]), json={"dementia_stage": "중등도"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["dementia_stage"], "중등도")
        self.assertEqual(body["name"], "어머니")
        self.assertEqual(body["symptoms"], ["수면장애"])
        self.assertEqual(self.update.call_args.args[3], {"dementia_stage": "중등도"})

    def test_update_null_clears_and_lists_replace(self):
        response = self.client.patch(self._update_url(self.mine["id"]),
                                     json={"name": None, "symptoms": ["배회"], "interests": None})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["name"])
        self.assertEqual(body["symptoms"], ["배회"])
        self.assertEqual(body["interests"], [])

    def test_update_other_or_missing_patient_is_404(self):
        for patient_id in (self.others["id"], uuid4()):
            with self.subTest(patient_id=patient_id):
                response = self.client.patch(self._update_url(patient_id), json={"name": "바꿈"})
                self.assertEqual(response.status_code, 404)
        self.update.assert_not_called()
        self.assertEqual(self.others["name"], "다른 분")

    def test_update_rejects_bad_bodies_without_writing(self):
        future = (date.today() + timedelta(days=1)).isoformat()
        for body in ({"caregiver_id": str(self.other_user_id)}, {"dementia_stage": "아주 심함"},
                     {"diagnosis_date": future}, {"unknown": True}):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch(self._update_url(self.mine["id"]), json=body).status_code, 422)
        self.update.assert_not_called()

    def test_update_deleted_between_check_and_update_is_404(self):
        # 소유 확인은 통과했는데 수정 직전에 환자가 지워진 경우 (update_patient가 None)
        with patch.object(user_functions, "update_patient", return_value=None):
            response = self.client.patch(self._update_url(self.mine["id"]), json={"name": "바꿈"})
        self.assertEqual(response.status_code, 404)

    # ---------- 인증 ----------

    def test_requires_login(self):
        self.app.dependency_overrides.clear()  # 실제 인증 의존성
        self.assertEqual(self.client.post(self.create_url, json={}).status_code, 401)
        self.assertEqual(self.client.patch(self._update_url(self.mine["id"]), json={}).status_code, 401)
        self.insert.assert_not_called()
        self.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
