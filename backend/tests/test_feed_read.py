"""app/db/feed_read.py 테스트 (실 DB). 박주현 담당.

실행 (backend/ 에서):
    python -m unittest tests.test_feed_read

로컬 DB(docker compose up -d)가 떠 있어야 하고 001~006이 적용돼 있어야 한다.
테스트용 사용자·환자·기록은 이 파일이 직접 만들고 **끝나면 지운다**
(이메일이 'test-feed-read-*@example.com'인 사용자만 건드린다. 샘플 시드 데이터는 손대지 않는다).
"""
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg

from app.db.connection import get_connection
from app.db.feed_read import (
    FeedPatientLimitExceeded,
    FeedReadError,
    FeedUserNotFound,
    get_feed_context_snapshot,
    save_care_log,
    save_profile_summary,
)
from app.feed_schemas import MAX_MANAGED_PATIENTS

EMAIL_PREFIX = "test-feed-read-"

# 기준 시각은 테스트 안에서 고정한다 (실행 시각에 따라 결과가 흔들리지 않게).
REFERENCE_TIME = datetime(2026, 10, 12, 3, 0, 0, tzinfo=timezone.utc)
REFERENCE_DATE = date(2026, 10, 12)
PERIOD_END = REFERENCE_TIME
PERIOD_START = REFERENCE_TIME - timedelta(hours=720)  # 웅 님 Loader와 같은 30일


def snapshot(user_id: UUID, **overrides):
    kwargs = dict(
        user_id=user_id,
        reference_time=REFERENCE_TIME,
        reference_date=REFERENCE_DATE,
        recent_period_start=PERIOD_START,
        recent_period_end=PERIOD_END,
    )
    kwargs.update(overrides)
    return get_feed_context_snapshot(**kwargs)


class FeedReadTestBase(unittest.TestCase):
    """테스트용 사용자를 만들고, 끝나면 지운다."""

    def setUp(self):
        self.user_id = self._create_user()

    def tearDown(self):
        # users를 지우면 환자·기록·요약이 ON DELETE CASCADE로 함께 지워진다.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE email LIKE %s", (EMAIL_PREFIX + "%",))

    def _create_user(self) -> UUID:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (name, email) VALUES (%s, %s) RETURNING id",
                    ("테스트 간병인", f"{EMAIL_PREFIX}{uuid4().hex[:12]}@example.com"),
                )
                return cur.fetchone()[0]

    def _add_patient(self, caregiver_id: UUID = None, **cols) -> UUID:
        values = {"caregiver_id": caregiver_id or self.user_id, "name": "테스트 환자", **cols}
        keys = ", ".join(values)
        marks = ", ".join(f"%({k})s" for k in values)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO patient_profiles ({keys}) VALUES ({marks}) RETURNING id", values
                )
                return cur.fetchone()[0]

    def _insert(self, table: str, **values) -> UUID:
        keys = ", ".join(values)
        marks = ", ".join(f"%({k})s" for k in values)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"INSERT INTO {table} ({keys}) VALUES ({marks}) RETURNING id", values)
                return cur.fetchone()[0]

    def _add_caregiver_profile(self, **cols):
        values = {"user_id": self.user_id, **cols}
        keys = ", ".join(values)
        marks = ", ".join(f"%({k})s" for k in values)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"INSERT INTO caregiver_profiles ({keys}) VALUES ({marks})", values)

    def _save_summary(self, period_start: date, period_end: date, summary: str,
                      tags: list[str], generated_at: datetime = None) -> UUID:
        """월간 요약을 저장하고 generated_at까지 고정한다.

        save_profile_summary()는 generated_at에 DB의 now()를 찍는다. 테스트는 기준 시각을
        고정값(REFERENCE_TIME)으로 쓰기 때문에, 실제 실행 시각이 그보다 늦으면
        'reference_time 이후에 생성된 요약'으로 제외된다. 그래서 테스트가 직접 정해 준다.
        """
        summary_id = save_profile_summary(self.user_id, period_start, period_end, summary, tags)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE profile_summaries SET generated_at = %s WHERE id = %s",
                            (generated_at or (REFERENCE_TIME - timedelta(days=1)), summary_id))
        return summary_id


# ============================================================
# 최근 추천 이력 (recently_recommended)
# ============================================================


class RecentlyRecommendedTests(FeedReadTestBase):
    """스냅샷의 recently_recommended. feed_items 시각은 REFERENCE_TIME 기준으로 직접 넣는다."""

    def setUp(self):
        super().setUp()
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM papers ORDER BY id LIMIT 2")
            self.papers = [row[0] for row in cur.fetchall()]
        if len(self.papers) < 2:
            self.skipTest("논문이 2편 미만입니다 — 팀 덤프 복원 필요")

    def _recommend(self, paper_id, days_ago, user_id=None):
        self._insert("feed_items", user_id=user_id or self.user_id, paper_id=paper_id,
                     created_at=REFERENCE_TIME - timedelta(days=days_ago))

    def test_no_history_is_empty_list(self):
        self.assertEqual(snapshot(self.user_id).recently_recommended, [])

    def test_recent_papers_are_included_in_order(self):
        self._recommend(self.papers[0], 10)
        self._recommend(self.papers[1], 2)
        self._recommend(self.papers[0], 5)
        self._recommend(self.papers[1], 31)   # 기간 밖
        self._recommend(self.papers[0], -1)   # 미래
        recent = snapshot(self.user_id).recently_recommended
        self.assertEqual([(r.paper_id, r.times_recommended) for r in recent],
                         [(self.papers[1], 1), (self.papers[0], 2)])
        self.assertEqual(recent[1].last_recommended_at, REFERENCE_TIME - timedelta(days=5))

    def test_other_users_history_is_not_mixed(self):
        other = self._create_user()
        self._recommend(self.papers[0], 1, user_id=other)
        self.assertEqual(snapshot(self.user_id).recently_recommended, [])
        self.assertEqual([r.paper_id for r in snapshot(other).recently_recommended], [self.papers[0]])


# ============================================================
# 기본 동작 / 빈 결과
# ============================================================


class BasicTests(FeedReadTestBase):
    def test_unknown_user_raises_not_found(self):
        """없는 사용자는 빈 스냅샷이 아니라 FeedUserNotFound다."""
        with self.assertRaises(FeedUserNotFound) as ctx:
            snapshot(uuid4())
        # 조회 실패(FeedReadError)와 섞이지 않아야 한다.
        self.assertNotIsInstance(ctx.exception, FeedReadError)

    def test_no_caregiver_profile_is_none(self):
        """자가점검을 안 한 사용자는 caregiver_profile이 None (실패가 아니다)."""
        self.assertIsNone(snapshot(self.user_id).caregiver_profile)

    def test_no_patients_is_empty_collection(self):
        """환자가 0명이면 빈 목록 + total_count 0."""
        snap = snapshot(self.user_id)
        self.assertEqual(snap.managed_patients.items, [])
        self.assertEqual(snap.managed_patients.total_count, 0)
        self.assertEqual(snap.recent_care_logs.total_count, 0)
        self.assertIsNone(snap.long_term_summary)

    def test_caregiver_profile_and_arrays(self):
        """배열 칸은 COALESCE로 빈 목록이 되고, 점수는 int로 온다."""
        self._add_caregiver_profile(relationship="자녀", burden_score=24, mood_score=6,
                                    lifestyle_tags=None)
        self._add_patient(symptoms=None, interests=None, dementia_stage="경도")
        snap = snapshot(self.user_id)
        self.assertEqual(snap.caregiver_profile.lifestyle_tags, [])
        self.assertEqual(snap.caregiver_profile.burden_score, 24)
        self.assertEqual(snap.managed_patients.items[0].symptoms, [])
        self.assertEqual(snap.managed_patients.items[0].interests, [])

    def test_patient_scope_excludes_other_caregivers(self):
        """다른 간병인의 환자는 안 담긴다."""
        other = self._create_user()
        self._add_patient(caregiver_id=other)
        mine = self._add_patient()
        snap = snapshot(self.user_id)
        self.assertEqual([p.patient_id for p in snap.managed_patients.items], [mine])

    def test_invalid_arguments_raise_value_error(self):
        """인자 검사는 DB에 가기 전에 ValueError."""
        naive = PERIOD_START.replace(tzinfo=None)
        cases = {
            "시간대 없음": {"recent_period_start": naive},
            "start >= end": {"recent_period_start": PERIOD_END},
            "end > reference_time": {"recent_period_end": REFERENCE_TIME + timedelta(hours=1)},
        }
        for label, override in cases.items():
            with self.subTest(label), self.assertRaises(ValueError):
                snapshot(self.user_id, **override)


# ============================================================
# 상한(I)과 total_count(E)
# ============================================================


class CapAndCountTests(FeedReadTestBase):
    def test_assessment_cap_keeps_full_total_count(self):
        """평가 25건 → items 20건, total_count 25건."""
        patient = self._add_patient()
        for i in range(25):
            self._insert("clinical_assessments", patient_id=patient, assessment_type="CDR",
                         score=Decimal("0.5"), assessed_at=REFERENCE_DATE - timedelta(days=i))
        col = snapshot(self.user_id).managed_patients.items[0].clinical_assessments
        self.assertEqual(len(col.items), 20)
        self.assertEqual(col.total_count, 25)
        self.assertTrue(col.is_truncated)

    def test_assessment_window_and_order(self):
        """365일 밖은 제외. 정렬은 assessed_at DESC."""
        patient = self._add_patient()
        inside = REFERENCE_DATE - timedelta(days=364)
        outside = REFERENCE_DATE - timedelta(days=366)
        future = REFERENCE_DATE + timedelta(days=1)
        for d in (inside, outside, future, REFERENCE_DATE):
            self._insert("clinical_assessments", patient_id=patient, assessment_type="CDR",
                         assessed_at=d)
        col = snapshot(self.user_id).managed_patients.items[0].clinical_assessments
        self.assertEqual(col.total_count, 2, "365일 이내 + 미래 아닌 것만")
        self.assertEqual([r.assessed_at for r in col.items], [REFERENCE_DATE, inside])

    def test_score_stays_decimal(self):
        """score는 Decimal 그대로 (float로 바꾸지 않는다)."""
        patient = self._add_patient()
        self._insert("clinical_assessments", patient_id=patient, assessment_type="K-QDRS",
                     score=Decimal("22.00"), assessed_at=REFERENCE_DATE)
        score = snapshot(self.user_id).managed_patients.items[0].clinical_assessments.items[0].score
        self.assertIsInstance(score, Decimal)
        self.assertEqual(score, Decimal("22.00"))

    def test_counts_are_zero_for_patient_without_records(self):
        """기록이 없는 환자도 0으로 채워진다 (쿼리 결과에 안 나와도)."""
        self._add_patient()
        patient = snapshot(self.user_id).managed_patients.items[0]
        for col in (patient.clinical_assessments, patient.safety_events,
                    patient.medications, patient.medical_visits):
            self.assertEqual(col.items, [])
            self.assertEqual(col.total_count, 0)

    def test_query_count_is_fixed_regardless_of_patients(self):
        """환자가 늘어도 쿼리 수가 늘지 않는다 (창 함수 + GROUP BY)."""
        for _ in range(5):
            patient = self._add_patient()
            self._insert("clinical_assessments", patient_id=patient, assessment_type="CDR",
                         assessed_at=REFERENCE_DATE)
        snap = snapshot(self.user_id)
        self.assertEqual(len(snap.managed_patients.items), 5)
        for patient in snap.managed_patients.items:
            self.assertEqual(patient.clinical_assessments.total_count, 1)


# ============================================================
# 컬렉션별 적격 조건
# ============================================================


class SafetyEventTests(FeedReadTestBase):
    def test_all_false_day_is_excluded(self):
        """플래그가 전부 false인 날(= 확인했고 사건 없음)은 적격이 아니다."""
        patient = self._add_patient()
        self._insert("safety_events", patient_id=patient, event_date=REFERENCE_DATE,
                     has_fall=False, has_wandering=False, has_missing=False, note="이상 없음")
        event_day = REFERENCE_DATE - timedelta(days=1)
        self._insert("safety_events", patient_id=patient, event_date=event_day,
                     has_fall=False, has_wandering=True, has_missing=False)
        col = snapshot(self.user_id).managed_patients.items[0].safety_events
        self.assertEqual(col.total_count, 1, "전부 false인 날은 total_count에도 안 들어간다")
        self.assertEqual([r.event_date for r in col.items], [event_day])

    def test_window_is_180_days(self):
        patient = self._add_patient()
        inside = REFERENCE_DATE - timedelta(days=179)
        outside = REFERENCE_DATE - timedelta(days=181)
        for d in (inside, outside):
            self._insert("safety_events", patient_id=patient, event_date=d, has_fall=True)
        col = snapshot(self.user_id).managed_patients.items[0].safety_events
        self.assertEqual([r.event_date for r in col.items], [inside])


class MedicationTests(FeedReadTestBase):
    def test_stopped_within_90_days_included_but_not_91(self):
        """89일 전 중단 약은 포함, 91일 전 중단 약은 제외."""
        patient = self._add_patient()
        self._insert("medications", patient_id=patient, drug_name="89일전중단",
                     is_taking=False, end_date=REFERENCE_DATE - timedelta(days=89))
        self._insert("medications", patient_id=patient, drug_name="91일전중단",
                     is_taking=False, end_date=REFERENCE_DATE - timedelta(days=91))
        col = snapshot(self.user_id).managed_patients.items[0].medications
        self.assertEqual(col.total_count, 1)
        self.assertEqual([r.drug_name for r in col.items], ["89일전중단"])

    def test_currently_taking_always_included_and_sorted_first(self):
        """복용 중인 약은 기간과 무관하게 포함되고, is_taking DESC로 먼저 온다."""
        patient = self._add_patient()
        self._insert("medications", patient_id=patient, drug_name="복용중-오래된",
                     is_taking=True, start_date=REFERENCE_DATE - timedelta(days=900))
        self._insert("medications", patient_id=patient, drug_name="최근중단",
                     is_taking=False, end_date=REFERENCE_DATE - timedelta(days=10))
        col = snapshot(self.user_id).managed_patients.items[0].medications
        self.assertEqual(col.total_count, 2)
        self.assertEqual([r.drug_name for r in col.items], ["복용중-오래된", "최근중단"])
        self.assertEqual([r.is_taking for r in col.items], [True, False])

    def test_stopped_with_future_or_missing_end_date_is_excluded(self):
        """중단인데 종료일이 미래이거나 NULL이면 제외 (total_count에도 안 들어간다)."""
        patient = self._add_patient()
        self._insert("medications", patient_id=patient, drug_name="중단-종료일미래",
                     is_taking=False, end_date=REFERENCE_DATE + timedelta(days=1))
        self._insert("medications", patient_id=patient, drug_name="중단-종료일없음",
                     is_taking=False)
        self._insert("medications", patient_id=patient, drug_name="오늘중단",
                     is_taking=False, end_date=REFERENCE_DATE)
        self._insert("medications", patient_id=patient, drug_name="90일전중단",
                     is_taking=False, end_date=REFERENCE_DATE - timedelta(days=90))
        col = snapshot(self.user_id).managed_patients.items[0].medications
        self.assertEqual(col.total_count, 2)
        self.assertEqual({r.drug_name for r in col.items}, {"오늘중단", "90일전중단"})

    def test_extra_columns_are_returned(self):
        """추가 칸(dosage, frequency)이 담긴다."""
        patient = self._add_patient()
        self._insert("medications", patient_id=patient, drug_name="도네페질", is_taking=True,
                     dosage="1정", frequency="1일 1회 취침 전")
        med = snapshot(self.user_id).managed_patients.items[0].medications.items[0]
        self.assertEqual((med.dosage, med.frequency), ("1정", "1일 1회 취침 전"))


class MedicalVisitTests(FeedReadTestBase):
    def test_upcoming_visits_keep_their_slots(self):
        """지난 진료 25건 + 예약 3건 → 예약 3건 모두 포함되고 총 20건."""
        patient = self._add_patient()
        for i in range(1, 26):
            self._insert("medical_visits", patient_id=patient, is_visited=True,
                         visit_date=REFERENCE_DATE - timedelta(days=i))
        upcoming = [REFERENCE_DATE + timedelta(days=d) for d in (5, 30, 80)]
        for d in upcoming:
            self._insert("medical_visits", patient_id=patient, is_visited=False, visit_date=d)

        col = snapshot(self.user_id).managed_patients.items[0].medical_visits
        self.assertEqual(col.total_count, 28)
        self.assertEqual(len(col.items), 20)
        dates = [r.visit_date for r in col.items]
        for d in upcoming:
            self.assertIn(d, dates, "앞으로의 예약은 자리를 먼저 받는다")
        self.assertEqual(sum(1 for d in dates if d > REFERENCE_DATE), 3)
        self.assertEqual(sum(1 for d in dates if d <= REFERENCE_DATE), 17)
        self.assertEqual(dates, sorted(dates, reverse=True), "최종 순서는 visit_date DESC")

    def test_upcoming_slots_are_capped_at_five(self):
        """예약이 많아도 먼저 받는 자리는 5건까지. 나머지는 지난 진료가 채운다."""
        patient = self._add_patient()
        for i in range(1, 31):
            self._insert("medical_visits", patient_id=patient, is_visited=True,
                         visit_date=REFERENCE_DATE - timedelta(days=i))
        for d in range(1, 11):
            self._insert("medical_visits", patient_id=patient, is_visited=False,
                         visit_date=REFERENCE_DATE + timedelta(days=d))
        col = snapshot(self.user_id).managed_patients.items[0].medical_visits
        dates = [r.visit_date for r in col.items]
        self.assertEqual(len(dates), 20)
        self.assertEqual(sum(1 for d in dates if d > REFERENCE_DATE), 5)
        # 가까운 예약부터 고른다 (+1 ~ +5일)
        self.assertEqual(sorted(d for d in dates if d > REFERENCE_DATE),
                         [REFERENCE_DATE + timedelta(days=i) for i in range(1, 6)])

    def test_past_shortfall_is_filled_with_upcoming(self):
        """지난 진료가 모자라면 남은 자리를 예약이 채운다."""
        patient = self._add_patient()
        self._insert("medical_visits", patient_id=patient, is_visited=True,
                     visit_date=REFERENCE_DATE - timedelta(days=3))
        for d in range(1, 9):
            self._insert("medical_visits", patient_id=patient, is_visited=False,
                         visit_date=REFERENCE_DATE + timedelta(days=d))
        col = snapshot(self.user_id).managed_patients.items[0].medical_visits
        self.assertEqual(col.total_count, 9)
        self.assertEqual(len(col.items), 9, "적격이 상한보다 적으면 전부 담긴다")

    def test_window_bounds(self):
        """[reference_date - 365일, reference_date + 90일] 밖은 제외."""
        patient = self._add_patient()
        self._insert("medical_visits", patient_id=patient, is_visited=True,
                     visit_date=REFERENCE_DATE - timedelta(days=366))
        self._insert("medical_visits", patient_id=patient, is_visited=False,
                     visit_date=REFERENCE_DATE + timedelta(days=91))
        inside = REFERENCE_DATE + timedelta(days=89)
        self._insert("medical_visits", patient_id=patient, is_visited=False, visit_date=inside)
        col = snapshot(self.user_id).managed_patients.items[0].medical_visits
        self.assertEqual([r.visit_date for r in col.items], [inside])

    def test_state_and_date_must_agree(self):
        """과거+미방문, 미래+방문완료는 제외. 오늘은 미방문이면 예정, 방문완료면 지난 진료."""
        patient = self._add_patient()
        for label, visited, days in (("과거+미방문", False, -1), ("미래+방문완료", True, 1),
                                     ("오늘+미방문", False, 0), ("오늘+방문완료", True, 0),
                                     ("과거+방문완료", True, -1), ("미래+미방문", False, 1)):
            self._insert("medical_visits", patient_id=patient, is_visited=visited,
                         visit_date=REFERENCE_DATE + timedelta(days=days), visit_content=label)
        col = snapshot(self.user_id).managed_patients.items[0].medical_visits
        self.assertEqual(col.total_count, 4, "제외된 조합은 total_count에도 안 들어간다")
        self.assertEqual({r.visit_content for r in col.items},
                         {"오늘+미방문", "오늘+방문완료", "과거+방문완료", "미래+미방문"})

    def test_excluded_visits_do_not_take_slots(self):
        """제외된 조합이 20건 자리를 차지하지 않는다. 오늘 미방문은 예정 쪽 자리(5건)를 쓴다."""
        patient = self._add_patient()
        for i in range(1, 31):
            self._insert("medical_visits", patient_id=patient, is_visited=True,
                         visit_date=REFERENCE_DATE - timedelta(days=i))
            self._insert("medical_visits", patient_id=patient, is_visited=False,
                         visit_date=REFERENCE_DATE - timedelta(days=i))   # 과거+미방문
        for d in range(0, 10):
            self._insert("medical_visits", patient_id=patient, is_visited=False,
                         visit_date=REFERENCE_DATE + timedelta(days=d))
            self._insert("medical_visits", patient_id=patient, is_visited=True,
                         visit_date=REFERENCE_DATE + timedelta(days=d + 1))  # 미래+방문완료
        col = snapshot(self.user_id).managed_patients.items[0].medical_visits
        self.assertEqual(col.total_count, 40)
        self.assertEqual(len(col.items), 20)
        upcoming = sorted(r.visit_date for r in col.items if not r.is_visited)
        self.assertEqual(upcoming, [REFERENCE_DATE + timedelta(days=d) for d in range(0, 5)])
        self.assertTrue(all(r.visit_date < REFERENCE_DATE for r in col.items if r.is_visited))

    def test_extra_detail_columns_are_returned(self):
        """005 진료 상세 칸이 담긴다."""
        patient = self._add_patient()
        self._insert("medical_visits", patient_id=patient, is_visited=True,
                     visit_date=REFERENCE_DATE, department="신경과",
                     visit_content="기타 메모", diagnosis="알츠하이머형 치매",
                     medication_change="도네페질 5mg → 10mg", follow_up_plan="3개월 후 재방문")
        visit = snapshot(self.user_id).managed_patients.items[0].medical_visits.items[0]
        self.assertEqual(visit.diagnosis, "알츠하이머형 치매")
        self.assertEqual(visit.medication_change, "도네페질 5mg → 10mg")
        self.assertEqual(visit.follow_up_plan, "3개월 후 재방문")
        self.assertEqual(visit.visit_content, "기타 메모")


class CareLogTests(FeedReadTestBase):
    def test_unlinked_logs_are_included(self):
        """patient_id가 NULL인 기록(간병인 본인)은 그대로 담긴다."""
        save_care_log(user_id=self.user_id, log_type="caregiver_selfcare",
                      content="오늘 지쳤다", logged_at=REFERENCE_TIME - timedelta(days=1))
        col = snapshot(self.user_id).recent_care_logs
        self.assertEqual(col.total_count, 1)
        self.assertIsNone(col.items[0].patient_id)

    def test_logs_of_other_caregivers_patient_are_excluded(self):
        """다른 간병인의 환자에 붙은 기록은 적격이 아니다 (total_count에도 안 들어간다)."""
        other_user = self._create_user()
        other_patient = self._add_patient(caregiver_id=other_user)
        # 내 user_id + 남의 환자 조합을 직접 넣는다 (save_care_log는 막으므로).
        self._insert("care_logs", user_id=self.user_id, patient_id=other_patient,
                     log_type="patient_care", logged_at=REFERENCE_TIME - timedelta(days=1))
        mine = self._add_patient()
        self._insert("care_logs", user_id=self.user_id, patient_id=mine,
                     log_type="patient_care", logged_at=REFERENCE_TIME - timedelta(days=1))
        col = snapshot(self.user_id).recent_care_logs
        self.assertEqual(col.total_count, 1)
        self.assertEqual(col.items[0].patient_id, mine)

    def test_period_is_half_open(self):
        """기간은 [start, end) — 시작은 포함, 끝은 제외."""
        for label, when in (("start 정확히", PERIOD_START),
                            ("end 정확히", PERIOD_END),
                            ("start 직전", PERIOD_START - timedelta(seconds=1)),
                            ("end 직전", PERIOD_END - timedelta(seconds=1))):
            self._insert("care_logs", user_id=self.user_id, log_type="caregiver_selfcare",
                         content=label, logged_at=when)
        col = snapshot(self.user_id).recent_care_logs
        contents = {r.content for r in col.items}
        self.assertIn("start 정확히", contents, "start는 포함")
        self.assertIn("end 직전", contents)
        self.assertNotIn("end 정확히", contents, "end는 제외")
        self.assertNotIn("start 직전", contents)
        self.assertEqual(col.total_count, 2)

    def test_order_is_logged_at_desc(self):
        for days in (5, 1, 3):
            self._insert("care_logs", user_id=self.user_id, log_type="caregiver_selfcare",
                         content=f"{days}일 전", logged_at=REFERENCE_TIME - timedelta(days=days))
        items = snapshot(self.user_id).recent_care_logs.items
        self.assertEqual([r.content for r in items], ["1일 전", "3일 전", "5일 전"])


class LongTermSummaryTests(FeedReadTestBase):
    def test_picks_most_recent_finished_period(self):
        """이미 끝난 기간 중 가장 최근 1건."""
        self._save_summary(date(2026, 8, 1), date(2026, 8, 31), "8월", ["a"])
        self._save_summary(date(2026, 9, 1), date(2026, 9, 30), "9월", ["b"])
        summary = snapshot(self.user_id).long_term_summary
        self.assertEqual(summary.summary, "9월")
        self.assertEqual(summary.updated_tags, ["b"])

    def test_unfinished_period_is_excluded(self):
        """period_end가 reference_date보다 앞이어야 한다 (아직 안 끝난 기간은 제외)."""
        self._save_summary(date(2026, 10, 1), REFERENCE_DATE, "이번달", [])
        self.assertIsNone(snapshot(self.user_id).long_term_summary)

    def test_summary_generated_after_reference_time_is_excluded(self):
        """reference_time 이후에 생성된 요약은 제외."""
        self._save_summary(date(2026, 9, 1), date(2026, 9, 30), "9월", [],
                           generated_at=REFERENCE_TIME + timedelta(hours=1))
        self.assertIsNone(snapshot(self.user_id).long_term_summary)

    def test_upsert_keeps_one_row(self):
        """같은 기간을 다시 저장하면 행이 늘지 않고 갱신된다."""
        first = save_profile_summary(self.user_id, date(2026, 9, 1), date(2026, 9, 30), "처음", ["x"])
        second = self._save_summary(date(2026, 9, 1), date(2026, 9, 30), "갱신", ["y"])
        self.assertEqual(first, second, "id가 그대로여야 한다")
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM profile_summaries WHERE user_id = %s",
                            (self.user_id,))
                self.assertEqual(cur.fetchone()[0], 1)
        summary = snapshot(self.user_id).long_term_summary
        self.assertEqual((summary.summary, summary.updated_tags), ("갱신", ["y"]))

    def test_reversed_period_raises(self):
        with self.assertRaises(ValueError):
            save_profile_summary(self.user_id, date(2026, 9, 30), date(2026, 9, 1), "거꾸로", [])


class SaveCareLogTests(FeedReadTestBase):
    def test_rejects_patient_of_another_caregiver(self):
        other_patient = self._add_patient(caregiver_id=self._create_user())
        with self.assertRaises(ValueError):
            save_care_log(user_id=self.user_id, log_type="patient_care",
                          patient_id=other_patient, content="남의 환자")

    def test_rejects_empty_log_type(self):
        with self.assertRaises(ValueError):
            save_care_log(user_id=self.user_id, log_type="   ")

    def test_rejects_unknown_user(self):
        with self.assertRaises(ValueError):
            save_care_log(user_id=uuid4(), log_type="caregiver_selfcare")

    def test_saves_with_patient(self):
        patient = self._add_patient()
        log_id = save_care_log(user_id=self.user_id, log_type="patient_care",
                               content="산책", mood_tag="뿌듯함", patient_id=patient,
                               logged_at=REFERENCE_TIME - timedelta(days=2))
        self.assertIsInstance(log_id, UUID)
        item = snapshot(self.user_id).recent_care_logs.items[0]
        self.assertEqual((item.patient_id, item.content, item.mood_tag),
                         (patient, "산책", "뿌듯함"))


# ============================================================
# 환자 수 상한 / 트랜잭션
# ============================================================


class PatientLimitTests(FeedReadTestBase):
    def test_limit_is_not_exceeded(self):
        for _ in range(MAX_MANAGED_PATIENTS):
            self._add_patient()
        snap = snapshot(self.user_id)
        self.assertEqual(len(snap.managed_patients.items), MAX_MANAGED_PATIENTS)
        self.assertEqual(snap.managed_patients.total_count, MAX_MANAGED_PATIENTS)

    def test_over_limit_raises_instead_of_truncating(self):
        """환자 21명이면 자르지 않고 FeedPatientLimitExceeded."""
        for _ in range(MAX_MANAGED_PATIENTS + 1):
            self._add_patient()
        with self.assertRaises(FeedPatientLimitExceeded) as ctx:
            snapshot(self.user_id)
        self.assertEqual(ctx.exception.patient_count, MAX_MANAGED_PATIENTS + 1)
        # 조회 실패 계열이라 FeedReadError로도 잡힌다.
        self.assertIsInstance(ctx.exception, FeedReadError)


class TransactionTests(FeedReadTestBase):
    def test_transaction_is_read_only(self):
        """조회에 쓰는 트랜잭션 설정으로는 쓰기가 DB 레벨에서 막힌다."""
        with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
            with get_connection() as conn:
                conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
                conn.read_only = True
                with conn.cursor() as cur:
                    cur.execute("UPDATE users SET name = 'x' WHERE id = %s", (self.user_id,))

    def test_all_reads_share_one_snapshot(self):
        """모든 조회가 같은 스냅샷을 본다 (REPEATABLE READ)."""
        with get_connection() as conn:
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            with conn.cursor() as cur:
                cur.execute("SELECT pg_current_snapshot()")
                first = cur.fetchone()[0]
                # 다른 커넥션이 중간에 커밋해도
                with get_connection() as other:
                    with other.cursor() as ocur:
                        ocur.execute("INSERT INTO users (name, email) VALUES (%s, %s)",
                                     ("중간 삽입", f"{EMAIL_PREFIX}{uuid4().hex[:12]}@example.com"))
                cur.execute("SELECT pg_current_snapshot()")
                self.assertEqual(cur.fetchone()[0], first)


# ============================================================
# AI 쪽 Source DTO 호환성
# ============================================================


class SourceDtoCompatibilityTests(FeedReadTestBase):
    """결과를 허웅 님 Source DTO로 옮겼을 때 검증을 통과하는지.

    추가 칸(방문 7개, 복약 2개)만 빼면 그대로 들어가야 한다.
    """

    def test_snapshot_maps_onto_source_dtos(self):
        from app.ai.agent.workflows import feed_context_source as s

        self._add_caregiver_profile(relationship="자녀", burden_score=24, mood_score=6,
                                    lifestyle_tags=["직장병행"])
        patient = self._add_patient(dementia_stage="경도", diagnosis_date=date(2025, 3, 1),
                                    symptoms=["수면장애"], interests=["운동"])
        self._insert("clinical_assessments", patient_id=patient, assessment_type="CDR",
                     score=Decimal("0.50"), result_detail="기억력 저하",
                     assessed_at=REFERENCE_DATE - timedelta(days=10))
        self._insert("safety_events", patient_id=patient,
                     event_date=REFERENCE_DATE - timedelta(days=3), has_wandering=True,
                     note="새벽 배회")
        self._insert("medications", patient_id=patient, drug_name="도네페질", is_taking=True,
                     start_date=REFERENCE_DATE - timedelta(days=100), dosage="1정",
                     frequency="1일 1회")
        self._insert("medical_visits", patient_id=patient, is_visited=True,
                     visit_date=REFERENCE_DATE - timedelta(days=20), department="신경과",
                     diagnosis="알츠하이머형 치매", visit_content="기타 메모")
        save_care_log(user_id=self.user_id, log_type="patient_care", content="산책",
                      mood_tag="뿌듯함", patient_id=patient,
                      logged_at=REFERENCE_TIME - timedelta(days=2))
        save_care_log(user_id=self.user_id, log_type="caregiver_selfcare", content="지쳤다",
                      logged_at=REFERENCE_TIME - timedelta(days=1))
        self._save_summary(date(2026, 9, 1), date(2026, 9, 30), "9월 요약", ["수면장애"])

        snap = snapshot(self.user_id)

        # --- adapter: 추가 칸만 빼고 그대로 옮긴다 ---
        def collection(model, items, total):
            return s.SourceCollection[model](items=items, total_count=total)

        def pick(row, model):
            fields = set(model.model_fields)
            return model(**{k: v for k, v in row.model_dump().items() if k in fields})

        source_patients = [
            s.SourceManagedPatient(
                patient_id=p.patient_id, caregiver_id=p.caregiver_id,
                dementia_stage=p.dementia_stage, diagnosis_date=p.diagnosis_date,
                symptoms=p.symptoms, interests=p.interests, updated_at=p.updated_at,
                clinical_assessments=collection(
                    s.SourceClinicalAssessment,
                    [pick(r, s.SourceClinicalAssessment) for r in p.clinical_assessments.items],
                    p.clinical_assessments.total_count),
                safety_events=collection(
                    s.SourceSafetyEvent,
                    [pick(r, s.SourceSafetyEvent) for r in p.safety_events.items],
                    p.safety_events.total_count),
                medications=collection(
                    s.SourceMedication,
                    [pick(r, s.SourceMedication) for r in p.medications.items],
                    p.medications.total_count),
                medical_visits=collection(
                    s.SourceMedicalVisit,
                    [pick(r, s.SourceMedicalVisit) for r in p.medical_visits.items],
                    p.medical_visits.total_count),
            )
            for p in snap.managed_patients.items
        ]

        source_snapshot = s.FeedContextSourceSnapshot(
            caregiver_profile=pick(snap.caregiver_profile, s.SourceCaregiverProfile),
            managed_patients=collection(s.SourceManagedPatient, source_patients,
                                        snap.managed_patients.total_count),
            recent_care_logs=collection(
                s.SourceCareLog,
                [pick(r, s.SourceCareLog) for r in snap.recent_care_logs.items],
                snap.recent_care_logs.total_count),
            long_term_summary=pick(snap.long_term_summary, s.SourceLongTermSummary),
        )

        # Loader가 하는 재검증 + 불변식 검사를 그대로 통과해야 한다.
        revalidated = s.FeedContextSourceSnapshot.model_validate(source_snapshot)
        from app.ai.agent.workflows.feed_context_loader import _validate_source_invariants
        _validate_source_invariants(revalidated, self.user_id)

        self.assertEqual(revalidated.managed_patients.total_count, 1)
        self.assertEqual(len(revalidated.recent_care_logs.items), 2)
        self.assertEqual(revalidated.long_term_summary.summary, "9월 요약")
        self.assertEqual(revalidated.managed_patients.items[0].medications.items[0].drug_name,
                         "도네페질")

    def test_production_source_accepts_boundary_records(self):
        """정책 경계에 걸친 기록이 DB에 있어도 웅 님 ProductionFeedContextSource가 거부하지 않는다.

        adapter는 과거+미방문 / 미래+방문완료 / 종료일이 미래인 중단 약이 하나라도 오면
        스냅샷 전체를 거부한다. 조회 단계에서 그 조합이 아예 안 나가야 한다.
        """
        from app.ai.agent.schemas import AgentContext
        from app.ai.agent.workflows.feed_context_backend_source import ProductionFeedContextSource
        from app.ai.agent.workflows.feed_context_loader import DefaultFeedContextLoader
        from app.ai.agent.workflows.feed_context_prepare import prepare_production_feed_input
        from app.user_schemas import APP_TIMEZONE

        patient = self._add_patient(dementia_stage="경도", symptoms=["배회"])
        for visited, days in ((False, -1), (True, 1), (False, 0), (True, 0), (True, -1), (False, 1)):
            self._insert("medical_visits", patient_id=patient, is_visited=visited,
                         visit_date=REFERENCE_DATE + timedelta(days=days))
        for taking, end in ((False, 1), (False, None), (False, 0), (True, 1), (True, None)):
            self._insert("medications", patient_id=patient, drug_name="약", is_taking=taking,
                         end_date=None if end is None else REFERENCE_DATE + timedelta(days=end))
        self._insert("safety_events", patient_id=patient, event_date=REFERENCE_DATE,
                     has_fall=False, has_wandering=False, has_missing=False)
        save_care_log(user_id=self.user_id, log_type="patient_care", content="산책",
                      patient_id=patient, logged_at=REFERENCE_TIME - timedelta(days=2))

        source = ProductionFeedContextSource(read_snapshot=get_feed_context_snapshot)
        loaded = source.load_snapshot(
            user_id=self.user_id, reference_time=REFERENCE_TIME, reference_date=REFERENCE_DATE,
            recent_period_start=PERIOD_START, recent_period_end=PERIOD_END)
        got = loaded.managed_patients.items[0]
        self.assertEqual((got.medical_visits.total_count, len(got.medical_visits.items)), (4, 4))
        self.assertEqual((got.medications.total_count, len(got.medications.items)), (3, 3))
        self.assertEqual(got.safety_events.total_count, 0)

        # Loader → Prepare(정식 Stage 1)까지 같은 기준 시각으로 통과해야 한다.
        loader = DefaultFeedContextLoader(source=source, clock=lambda: REFERENCE_TIME,
                                          service_timezone=APP_TIMEZONE)
        context = AgentContext(request_id="test", user_id=self.user_id, patient_id=None)
        self.assertIn('"feed_scope": "caregiver_all"',
                      prepare_production_feed_input(context, loader))

    def test_field_names_match_source_dtos(self):
        """Source DTO의 모든 칸이 내 DTO에도 같은 이름으로 있는지 (빠진 칸 조기 발견)."""
        from app.ai.agent.workflows import feed_context_source as s
        from app import feed_schemas as f

        pairs = [
            (s.SourceCaregiverProfile, f.FeedReadCaregiverProfile),
            (s.SourceClinicalAssessment, f.FeedReadClinicalAssessment),
            (s.SourceSafetyEvent, f.FeedReadSafetyEvent),
            (s.SourceMedication, f.FeedReadMedication),
            (s.SourceMedicalVisit, f.FeedReadMedicalVisit),
            (s.SourceCareLog, f.FeedReadCareLog),
            (s.SourceLongTermSummary, f.FeedReadLongTermSummary),
        ]
        for source, mine in pairs:
            with self.subTest(source.__name__):
                missing = set(source.model_fields) - set(mine.model_fields)
                self.assertEqual(missing, set(), f"{mine.__name__}에 없는 칸: {missing}")

        # 환자 DTO는 자식 컬렉션 이름까지 같아야 한다.
        missing = set(s.SourceManagedPatient.model_fields) - set(
            f.FeedReadManagedPatient.model_fields)
        self.assertEqual(missing, set())


if __name__ == "__main__":
    unittest.main()
