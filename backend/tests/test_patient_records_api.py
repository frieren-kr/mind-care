"""앱 입력 API: 검사 결과·복용 약·진료 예약. DB I/O만 대체하고 실제 라우터·검증·저장 함수를 실행한다."""
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.users import router
from app.db import user_functions
from app.user_schemas import APP_TIMEZONE
from tests.auth_helpers import allow_business_test_requests


class PatientRecordsApiTests(unittest.TestCase):
    def setUp(self):
        self.user_id = uuid4()
        self.patient_id = uuid4()
        self.other_patient_id = uuid4()  # 같은 간병인의 다른 환자
        self.now = datetime.now(APP_TIMEZONE)
        self.today = self.now.date()
        self.rows: dict = {}  # id → row (medications·medical_visits)
        self.med = self._store("medications", patient_id=self.patient_id, drug_name="도네페질", dosage="5mg",
                               frequency="1일 1회 취침 전", is_taking=True, start_date=None, end_date=None, note=None)
        self.other_med = self._store("medications", patient_id=self.other_patient_id, drug_name="메만틴",
                                     dosage=None, frequency=None, is_taking=True, start_date=None, end_date=None,
                                     note=None)
        visit_fields = dict.fromkeys(("hospital_name", "department", "visit_reason", "diagnosis", "treatment_content",
                                      "test_summary", "medication_change", "doctor_note", "follow_up_plan",
                                      "visit_content"))
        self.visit = self._store("medical_visits", patient_id=self.patient_id, visit_date=self.today + timedelta(days=3),
                                 is_visited=False, **visit_fields)
        self.past_visit = self._store("medical_visits", patient_id=self.patient_id,
                                      visit_date=self.today - timedelta(days=1), is_visited=False, **visit_fields)

        def insert(table, values):
            return self._store(table, **values)

        def update(table, key_column, key, fields):
            row = self.rows.get(key)
            if row is None:
                return None
            row.update(fields, updated_at=self.now)
            return row.copy()

        def fetch_one(query, params):
            row = self.rows.get(params.get("id"))
            return row.copy() if row else None

        def is_caregiver_of(user_id, patient_id):
            return user_id == self.user_id and patient_id in (self.patient_id, self.other_patient_id)

        patches = [
            patch.object(user_functions, "_insert_row", side_effect=insert),
            patch.object(user_functions, "_update_row", side_effect=update),
            patch.object(user_functions, "_fetch_one", side_effect=fetch_one),
            patch.object(user_functions, "is_caregiver_of", side_effect=is_caregiver_of),
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
        self.base = f"/users/{self.user_id}/patients/{self.patient_id}"

    def _store(self, table, **values):
        row = {"id": uuid4(), "created_at": self.now, "updated_at": self.now, **values}
        if table == "clinical_assessments":
            row.pop("updated_at")
        self.rows[row["id"]] = row
        return row

    # ---------- 검사 결과 ----------

    def test_self_check_result_is_saved_as_kdsq_c(self):
        body = {"assessment_type": "kdsq-c", "score": 7, "assessed_at": self.today.isoformat(),
                "assessed_by": "보호자 자가점검 (앱)", "result_detail": "기억력 3/8"}
        response = self.client.post(f"{self.base}/assessments", json=body)
        self.assertEqual(response.status_code, 201)
        saved = response.json()
        self.assertEqual(saved["assessment_type"], "KDSQ-C")  # 표준 이름으로
        self.assertEqual(saved["score"], 7)
        self.assertEqual(saved["patient_id"], str(self.patient_id))
        self.assertEqual(self.insert.call_args.args[0], "clinical_assessments")

    def test_assessment_rejects_bad_bodies_without_writing(self):
        today = self.today.isoformat()
        for body in (
            {"assessment_type": "KDSQ-C", "score": 31, "assessed_at": today},  # 범위 0~30
            {"assessment_type": "KDSQ-C", "score": 7},  # 날짜 필수
            {"score": 7, "assessed_at": today},  # 검사명 필수
            {"assessment_type": "없는검사", "assessed_at": today},
            {"assessment_type": "KDSQ-C", "assessed_at": (self.today + timedelta(days=1)).isoformat()},
            {"assessment_type": "KDSQ-C", "assessed_at": today, "patient_id": str(self.other_patient_id)},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(f"{self.base}/assessments", json=body).status_code, 422)
        self.insert.assert_not_called()

    # ---------- 약 ----------

    def test_add_medication(self):
        response = self.client.post(f"{self.base}/medications",
                                    json={"drug_name": " 메만틴 ", "dosage": "10mg", "frequency": "1일 2회"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["drug_name"], "메만틴")
        self.assertTrue(response.json()["is_taking"])
        self.assertEqual(self.insert.call_args.args[1]["patient_id"], self.patient_id)

    def test_add_medication_requires_name(self):
        for body in ({}, {"drug_name": "  "}, {"drug_name": "약", "is_taking": "yes"}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(f"{self.base}/medications", json=body).status_code, 422)
        self.insert.assert_not_called()

    def test_stop_medication(self):
        response = self.client.patch(f"{self.base}/medications/{self.med['id']}",
                                     json={"is_taking": False, "end_date": self.today.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["is_taking"])
        self.assertEqual(response.json()["end_date"], self.today.isoformat())

    def test_stop_medication_with_future_end_date_is_422(self):
        future = (self.today + timedelta(days=2)).isoformat()
        response = self.client.patch(f"{self.base}/medications/{self.med['id']}",
                                     json={"is_taking": False, "end_date": future})
        self.assertEqual(response.status_code, 422)
        self.update.assert_not_called()

    def test_medication_of_other_patient_or_unknown_is_404(self):
        # 같은 간병인이어도 주소의 환자 것이 아니면 404 (다른 환자 약을 이 주소로 못 바꾼다)
        for med_id in (self.other_med["id"], uuid4()):
            with self.subTest(med_id=med_id):
                response = self.client.patch(f"{self.base}/medications/{med_id}", json={"dosage": "1정"})
                self.assertEqual(response.status_code, 404)
        self.update.assert_not_called()

    # ---------- 진료 ----------

    def test_add_upcoming_visit(self):
        day = (self.today + timedelta(days=10)).isoformat()
        response = self.client.post(f"{self.base}/visits",
                                    json={"visit_date": day, "hospital_name": "서울신경과", "department": "신경과"})
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["is_visited"])
        self.assertEqual(response.json()["visit_date"], day)

    def test_future_visit_cannot_be_marked_visited(self):
        day = (self.today + timedelta(days=1)).isoformat()
        self.assertEqual(self.client.post(f"{self.base}/visits", json={"visit_date": day, "is_visited": True})
                         .status_code, 422)
        self.assertEqual(self.client.patch(f"{self.base}/visits/{self.visit['id']}", json={"is_visited": True})
                         .status_code, 422)
        self.insert.assert_not_called()
        self.update.assert_not_called()

    def test_mark_past_visit_as_visited_with_notes(self):
        response = self.client.patch(f"{self.base}/visits/{self.past_visit['id']}",
                                     json={"is_visited": True, "doctor_note": "수면 기록을 2주 더 보기로 했다."})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["is_visited"])
        self.assertEqual(response.json()["doctor_note"], "수면 기록을 2주 더 보기로 했다.")

    def test_visit_requires_date_and_known_fields(self):
        for body in ({"hospital_name": "병원"}, {"visit_date": "2026-02-30"}, {"visit_date": "2026-12-01", "x": 1}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(f"{self.base}/visits", json=body).status_code, 422)
        self.insert.assert_not_called()

    def test_unknown_visit_is_404(self):
        self.assertEqual(self.client.patch(f"{self.base}/visits/{uuid4()}", json={"doctor_note": "메모"}).status_code,
                         404)

    # ---------- 공통 ----------

    def test_not_my_patient_is_404_everywhere(self):
        base = f"/users/{self.user_id}/patients/{uuid4()}"
        today = date.today().isoformat()
        calls = [
            ("post", f"{base}/assessments", {"assessment_type": "KDSQ-C", "score": 3, "assessed_at": today}),
            ("post", f"{base}/medications", {"drug_name": "약"}),
            ("patch", f"{base}/medications/{self.med['id']}", {"dosage": "1정"}),
            ("post", f"{base}/visits", {"visit_date": today}),
            ("patch", f"{base}/visits/{self.visit['id']}", {"hospital_name": "병원"}),
        ]
        for method, url, body in calls:
            with self.subTest(url=url):
                self.assertEqual(getattr(self.client, method)(url, json=body).status_code, 404)
        self.insert.assert_not_called()
        self.update.assert_not_called()

    def test_requires_login(self):
        self.app.dependency_overrides.clear()
        self.assertEqual(self.client.post(f"{self.base}/medications", json={"drug_name": "약"}).status_code, 401)
        self.assertEqual(self.client.post(f"{self.base}/visits", json={"visit_date": "2026-12-01"}).status_code, 401)
        self.insert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
