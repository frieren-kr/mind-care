"""피드 개인화용 일괄 조회 + 돌봄 기록·월간 요약 쓰기 (박주현 담당).

★ 팀 규칙 ★
AI/백엔드 팀원은 DB에 직접 SQL을 쓰지 않는다. 이 모듈의 함수를 통해서만 접근한다.
입출력 형식은 `app/feed_schemas.py`가 정본이다.

이 모듈이 따로 있는 이유 (AI 담당 요구사항):
- `user_functions.py`의 조회 함수는 **쿼리마다 커넥션을 새로 연다**. 그러면 조회들이
  서로 다른 시점을 보게 되어 "한 스냅샷"이 성립하지 않는다.
- `get_user_profile_context()`는 자체 clock을 쓰고, CareLog·월간 요약이 없고,
  최신 평가·현재 복약만 고른다 (현재 상태 요약용 집계라 피드 입력으로 쓸 수 없다).
그래서 **읽기 전용 REPEATABLE READ 트랜잭션 하나**로 전부 읽는 경로를 새로 두었다.

이 모듈은 `app/ai/`를 import 하지 않는다 (db → ai 방향 의존은 만들지 않는다).
"""
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID

import psycopg
from psycopg import IsolationLevel
from psycopg.errors import ForeignKeyViolation
from psycopg.rows import dict_row
from pydantic import ValidationError

from app.db.connection import get_connection
from app.feed_schemas import (
    ASSESSMENT_WINDOW_DAYS,
    DEFAULT_CAPS,
    MAX_MANAGED_PATIENTS,
    MEDICATION_STOPPED_WITHIN_DAYS,
    SAFETY_WINDOW_DAYS,
    VISIT_FUTURE_WINDOW_DAYS,
    VISIT_PAST_WINDOW_DAYS,
    FeedReadCaregiverProfile,
    FeedReadCareLog,
    FeedReadClinicalAssessment,
    FeedReadCollection,
    FeedReadLongTermSummary,
    FeedReadManagedPatient,
    FeedReadMedicalVisit,
    FeedReadMedication,
    FeedReadSafetyEvent,
    FeedReadSnapshot,
)

logger = logging.getLogger(__name__)


# ============================================================
# 예외 — 실패를 빈 결과로 바꾸지 않기 위해 종류를 나눈다
# ============================================================


class FeedReadError(RuntimeError):
    """조회·건수·DTO 검증이 실패했다. 빈 결과로 바꾸지 않는다. (API → 500)"""


class FeedUserNotFound(LookupError):
    """user_id가 users에 없다. 조회 실패와 구분된다. (API → 404)

    LookupError 계열이라 `except FeedReadError`에 걸리지 않는다.
    """

    def __init__(self, user_id: UUID):
        self.user_id = user_id
        super().__init__(f"존재하지 않는 사용자입니다: user_id={user_id}")


class FeedPatientLimitExceeded(FeedReadError):
    """관리 환자가 상한을 넘었다. 환자 목록은 완전해야 하므로 자르지 않고 실패시킨다."""

    def __init__(self, user_id: UUID, patient_count: int, limit: int = MAX_MANAGED_PATIENTS):
        self.user_id = user_id
        self.patient_count = patient_count
        self.limit = limit
        super().__init__(
            f"관리 환자가 너무 많습니다: {patient_count}명 (상한 {limit}명, user_id={user_id})"
        )


# ============================================================
# SQL
# ============================================================

_USER_EXISTS = "SELECT 1 AS ok FROM users WHERE id = %(user_id)s"

_CAREGIVER_PROFILE = """
    SELECT user_id, relationship, burden_score, mood_score,
           COALESCE(lifestyle_tags, '{}') AS lifestyle_tags, updated_at
      FROM caregiver_profiles
     WHERE user_id = %(user_id)s
"""

# 환자는 상한으로 자르지 않는다 (Loader가 total_count == len(items)를 요구한다).
_PATIENTS = """
    SELECT id AS patient_id, caregiver_id, dementia_stage, diagnosis_date,
           COALESCE(symptoms, '{}') AS symptoms,
           COALESCE(interests, '{}') AS interests,
           updated_at
      FROM patient_profiles
     WHERE caregiver_id = %(user_id)s
     ORDER BY created_at, id
"""

# 자식 기록 4종: 환자 수와 무관하게 쿼리 수를 고정하려고 patient_id = ANY(...)로 한 번에 읽고,
# 환자별 상한은 창 함수(row_number)로 건다. 건수는 GROUP BY로 한 번에 센다.

_ASSESSMENT_WHERE = """
     WHERE patient_id = ANY(%(patient_ids)s)
       AND assessed_at <= %(reference_date)s
       AND assessed_at >= %(reference_date)s - make_interval(days => %(window_days)s)
"""
_ASSESSMENTS = f"""
    SELECT patient_id, assessment_type, score, result_detail, assessed_at
      FROM (
        SELECT patient_id, assessment_type, score, result_detail, assessed_at,
               row_number() OVER (PARTITION BY patient_id
                                  ORDER BY assessed_at DESC, created_at DESC, id) AS rn
          FROM clinical_assessments
          {_ASSESSMENT_WHERE}
      ) t
     WHERE rn <= %(cap)s
     ORDER BY patient_id, rn
"""
_ASSESSMENTS_COUNT = f"""
    SELECT patient_id, count(*) AS total
      FROM clinical_assessments
      {_ASSESSMENT_WHERE}
     GROUP BY patient_id
"""

# 플래그가 전부 false인 날(= 확인했고 사건 없음)은 적격이 아니다 (팀 확정 정책).
_SAFETY_WHERE = """
     WHERE patient_id = ANY(%(patient_ids)s)
       AND event_date <= %(reference_date)s
       AND event_date >= %(reference_date)s - make_interval(days => %(window_days)s)
       AND (has_fall OR has_wandering OR has_missing)
"""
_SAFETY = f"""
    SELECT patient_id, event_date, has_fall, has_wandering, has_missing, note
      FROM (
        SELECT patient_id, event_date, has_fall, has_wandering, has_missing, note,
               row_number() OVER (PARTITION BY patient_id
                                  ORDER BY event_date DESC, id) AS rn
          FROM safety_events
          {_SAFETY_WHERE}
      ) t
     WHERE rn <= %(cap)s
     ORDER BY patient_id, rn
"""
_SAFETY_COUNT = f"""
    SELECT patient_id, count(*) AS total
      FROM safety_events
      {_SAFETY_WHERE}
     GROUP BY patient_id
"""

# 복용 중이거나, 최근에 끊은 약 (최근 중단은 임상적으로 의미가 있다).
# 중단인데 end_date가 미래이거나 NULL인 약은 적격이 아니다 (AI adapter가 스냅샷 전체를 거부한다).
_MEDICATION_WHERE = """
     WHERE patient_id = ANY(%(patient_ids)s)
       AND (is_taking = true
            OR (end_date >= %(reference_date)s - make_interval(days => %(window_days)s)
                AND end_date <= %(reference_date)s))
"""
_MEDICATIONS = f"""
    SELECT patient_id, drug_name, is_taking, start_date, end_date, note, dosage, frequency
      FROM (
        SELECT patient_id, drug_name, is_taking, start_date, end_date, note, dosage, frequency,
               row_number() OVER (PARTITION BY patient_id
                                  ORDER BY is_taking DESC, start_date DESC NULLS LAST,
                                           created_at DESC, id) AS rn
          FROM medications
          {_MEDICATION_WHERE}
      ) t
     WHERE rn <= %(cap)s
     ORDER BY patient_id, rn
"""
_MEDICATIONS_COUNT = f"""
    SELECT patient_id, count(*) AS total
      FROM medications
      {_MEDICATION_WHERE}
     GROUP BY patient_id
"""

# 지난 진료 = 방문 완료 + [reference_date - past_days, reference_date]
# 예정 진료 = 미방문   + [reference_date, reference_date + future_days]
# 그 외 조합(미래 + 방문 완료, 과거 + 미방문)은 적격이 아니다. 조회와 건수가 이 조건을 같이 쓴다
# (AI adapter가 그런 기록이 하나라도 있으면 스냅샷 전체를 거부한다).
_VISIT_WHERE = """
     WHERE patient_id = ANY(%(patient_ids)s)
       AND ((is_visited
             AND visit_date >= %(reference_date)s - make_interval(days => %(past_days)s)
             AND visit_date <= %(reference_date)s)
            OR (NOT is_visited
                AND visit_date >= %(reference_date)s
                AND visit_date <= %(reference_date)s + make_interval(days => %(future_days)s)))
"""
# 방문 선정은 두 갈래다: 예정 진료(미방문)를 가까운 순으로 최대 upcoming_cap건 먼저 잡고,
# 남은 자리를 지난 진료로 최근 순으로 채운다. 한쪽이 모자라면 다른 쪽이 그 자리를 쓴다.
#   upcoming_quota = min(n_upcoming, upcoming_cap)
#   past_take      = min(n_past,     cap - upcoming_quota)
#   upcoming_take  = min(n_upcoming, cap - past_take)      ← 남은 자리를 예약으로 되채움
# rn은 is_upcoming으로 나눠 센다. 예약은 visit_date ASC(가까운 순), 지난 진료는 DESC(최근 순).
_VISITS = f"""
    WITH eligible AS (
        SELECT patient_id, visit_date, is_visited, department, visit_content,
               visit_reason, diagnosis, treatment_content, test_summary,
               medication_change, doctor_note, follow_up_plan,
               created_at, id,
               (NOT is_visited) AS is_upcoming
          FROM medical_visits
          {_VISIT_WHERE}
    ), ranked AS (
        SELECT *,
               row_number() OVER (
                   PARTITION BY patient_id, is_upcoming
                   ORDER BY CASE WHEN is_upcoming THEN visit_date END ASC,
                            visit_date DESC, created_at DESC, id
               ) AS rn,
               count(*) FILTER (WHERE is_upcoming) OVER (PARTITION BY patient_id) AS n_upcoming,
               count(*) FILTER (WHERE NOT is_upcoming) OVER (PARTITION BY patient_id) AS n_past
          FROM eligible
    ), quota AS (
        SELECT *,
               least(n_past, %(cap)s - least(n_upcoming, %(upcoming_cap)s)) AS past_take
          FROM ranked
    )
    SELECT patient_id, visit_date, is_visited, department, visit_content,
           visit_reason, diagnosis, treatment_content, test_summary,
           medication_change, doctor_note, follow_up_plan
      FROM quota
     WHERE (is_upcoming AND rn <= least(n_upcoming, %(cap)s - past_take))
        OR (NOT is_upcoming AND rn <= past_take)
     ORDER BY patient_id, visit_date DESC, created_at DESC, id
"""
_VISITS_COUNT = f"""
    SELECT patient_id, count(*) AS total
      FROM medical_visits
      {_VISIT_WHERE}
     GROUP BY patient_id
"""

# 기간은 반개구간 [start, end). patient_id가 있으면 '현재 관리 환자'인 것만 적격이다
# (조건을 WHERE에 두어 total_count에도 반영된다).
_CARE_LOG_WHERE = """
     WHERE user_id = %(user_id)s
       AND logged_at >= %(period_start)s
       AND logged_at <  %(period_end)s
       AND (patient_id IS NULL OR patient_id = ANY(%(patient_ids)s))
"""
_CARE_LOGS = f"""
    SELECT user_id, patient_id, log_type, content, mood_tag, logged_at
      FROM care_logs
      {_CARE_LOG_WHERE}
     ORDER BY logged_at DESC, id
     LIMIT %(cap)s
"""
_CARE_LOGS_COUNT = f"""
    SELECT count(*) AS total
      FROM care_logs
      {_CARE_LOG_WHERE}
"""

# 이미 끝난 기간(period_end < reference_date)이고, 기준 시각까지 생성된 요약만 본다.
_LONG_TERM_SUMMARY = """
    SELECT user_id, period_start, period_end, summary,
           COALESCE(updated_tags, '{}') AS updated_tags, generated_at
      FROM profile_summaries
     WHERE user_id = %(user_id)s
       AND period_end < %(reference_date)s
       AND generated_at <= %(reference_time)s
     ORDER BY period_end DESC, generated_at DESC, id
     LIMIT 1
"""


# ============================================================
# 일괄 조회
# ============================================================


def _check_arguments(
    reference_time: datetime,
    recent_period_start: datetime,
    recent_period_end: datetime,
) -> None:
    """DB에 가기 전에 인자를 검사한다. 시간 기준이 어긋나면 조용히 넘기지 않는다."""
    for name, value in (
        ("reference_time", reference_time),
        ("recent_period_start", recent_period_start),
        ("recent_period_end", recent_period_end),
    ):
        if not isinstance(value, datetime):
            raise ValueError(f"{name}은 datetime이어야 합니다: {type(value).__name__}")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name}에 시간대가 없습니다 (tz-aware datetime이어야 합니다).")
    if recent_period_start >= recent_period_end:
        raise ValueError(
            "recent_period_start는 recent_period_end보다 앞이어야 합니다: "
            f"{recent_period_start.isoformat()} >= {recent_period_end.isoformat()}"
        )
    if recent_period_end > reference_time:
        raise ValueError(
            "recent_period_end는 reference_time보다 늦을 수 없습니다: "
            f"{recent_period_end.isoformat()} > {reference_time.isoformat()}"
        )


def _grouped(rows: list[dict[str, Any]]) -> dict[UUID, list[dict[str, Any]]]:
    """patient_id별로 묶는다 (쿼리가 이미 patient_id, rn 순으로 정렬돼 있다)."""
    out: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[row["patient_id"]].append(row)
    return out


def _counts(rows: list[dict[str, Any]]) -> dict[UUID, int]:
    return {row["patient_id"]: row["total"] for row in rows}


def get_feed_context_snapshot(
    *,
    user_id: UUID,
    reference_time: datetime,
    reference_date: date,
    recent_period_start: datetime,
    recent_period_end: datetime,
) -> FeedReadSnapshot:
    """피드 개인화에 필요한 기록을 **한 스냅샷**으로 모두 읽는다.

    사용자 확인부터 모든 조회·건수까지를 **읽기 전용 REPEATABLE READ 트랜잭션 하나**에서
    처리한다. 그래서 모든 값이 같은 DB 상태를 본다 (조회 중간에 다른 트랜잭션이 커밋해도
    결과가 섞이지 않는다). 트랜잭션은 DB 레벨에서 읽기 전용이라 실수로 쓸 수도 없다.

    ★ 함수 안에서 now() / today()를 쓰지 않는다 ★
    시간 기준은 전부 인자로 받는다. 호출하는 쪽(AI Loader)의 시계와 어긋나지 않게 하기 위해서다.

    Args:
        user_id: 조회 대상 간병인 (users.id).
        reference_time: 기준 시각 (tz-aware). 월간 요약의 generated_at 상한, 인자 정합성 검사에 쓴다.
        reference_date: 위 시각을 서비스 시간대로 본 날짜. DATE 칸 비교(평가·안전·복약·방문)에 쓴다.
        recent_period_start: 돌봄 기록 기간 시작 (포함).
        recent_period_end: 돌봄 기록 기간 끝 (**제외**). 기간은 반개구간 [start, end).

    Returns:
        FeedReadSnapshot. None / 빈 목록 / 0은 **모든 조회가 성공했을 때만** 나온다.

    Raises:
        ValueError: 시각에 시간대가 없거나, start >= end, end > reference_time인 경우.
        FeedUserNotFound: user_id가 users에 없는 경우.
        FeedPatientLimitExceeded: 관리 환자가 MAX_MANAGED_PATIENTS(20)명을 넘는 경우.
        FeedReadError: 조회·건수·DTO 검증이 실패한 경우 (빈 결과로 바꾸지 않는다).
    """
    _check_arguments(reference_time, recent_period_start, recent_period_end)
    caps = DEFAULT_CAPS

    try:
        with get_connection() as conn:
            # 첫 쿼리 전에 설정해야 한다 (트랜잭션이 시작되면 바꿀 수 없다).
            conn.isolation_level = IsolationLevel.REPEATABLE_READ
            conn.read_only = True

            with conn.cursor(row_factory=dict_row) as cur:
                # 1) 사용자 확인 — 같은 스냅샷 안에서 본다.
                cur.execute(_USER_EXISTS, {"user_id": user_id})
                if cur.fetchone() is None:
                    raise FeedUserNotFound(user_id)

                # 2) 간병인 자가점검 (없으면 None)
                cur.execute(_CAREGIVER_PROFILE, {"user_id": user_id})
                caregiver_row = cur.fetchone()

                # 3) 관리 환자 — 자르지 않는다. 상한을 넘으면 실패시킨다.
                cur.execute(_PATIENTS, {"user_id": user_id})
                patient_rows = cur.fetchall()
                if len(patient_rows) > MAX_MANAGED_PATIENTS:
                    raise FeedPatientLimitExceeded(user_id, len(patient_rows))
                patient_ids = [row["patient_id"] for row in patient_rows]

                # 4~7) 자식 기록 4종 (환자 수와 무관하게 쿼리 8개)
                child = {"patient_ids": patient_ids, "reference_date": reference_date}

                cur.execute(_ASSESSMENTS, {**child, "window_days": ASSESSMENT_WINDOW_DAYS,
                                           "cap": caps["assessments"]})
                assessments = _grouped(cur.fetchall())
                cur.execute(_ASSESSMENTS_COUNT, {**child, "window_days": ASSESSMENT_WINDOW_DAYS})
                assessment_counts = _counts(cur.fetchall())

                cur.execute(_SAFETY, {**child, "window_days": SAFETY_WINDOW_DAYS,
                                      "cap": caps["safety"]})
                safety = _grouped(cur.fetchall())
                cur.execute(_SAFETY_COUNT, {**child, "window_days": SAFETY_WINDOW_DAYS})
                safety_counts = _counts(cur.fetchall())

                cur.execute(_MEDICATIONS, {**child, "window_days": MEDICATION_STOPPED_WITHIN_DAYS,
                                           "cap": caps["medications"]})
                medications = _grouped(cur.fetchall())
                cur.execute(_MEDICATIONS_COUNT,
                            {**child, "window_days": MEDICATION_STOPPED_WITHIN_DAYS})
                medication_counts = _counts(cur.fetchall())

                visit_params = {**child, "past_days": VISIT_PAST_WINDOW_DAYS,
                                "future_days": VISIT_FUTURE_WINDOW_DAYS}
                cur.execute(_VISITS, {**visit_params, "cap": caps["visits"],
                                      "upcoming_cap": caps["visits_upcoming"]})
                visits = _grouped(cur.fetchall())
                cur.execute(_VISITS_COUNT, visit_params)
                visit_counts = _counts(cur.fetchall())

                # 8) 돌봄 기록 — 기간 [start, end), 관리 환자이거나 환자 미연결인 것만
                log_params = {
                    "user_id": user_id,
                    "period_start": recent_period_start,
                    "period_end": recent_period_end,
                    "patient_ids": patient_ids,
                }
                cur.execute(_CARE_LOGS, {**log_params, "cap": caps["care_logs"]})
                care_log_rows = cur.fetchall()
                cur.execute(_CARE_LOGS_COUNT, log_params)
                care_log_total = cur.fetchone()["total"]

                # 9) 월간 요약 (없으면 None)
                cur.execute(_LONG_TERM_SUMMARY, {"user_id": user_id,
                                                 "reference_date": reference_date,
                                                 "reference_time": reference_time})
                summary_row = cur.fetchone()
    except (ValueError, FeedUserNotFound, FeedPatientLimitExceeded):
        raise
    except psycopg.Error as err:
        raise FeedReadError(f"피드 컨텍스트 조회에 실패했습니다 (user_id={user_id}): {err}") from err

    # --- DTO 조립. 검증 실패도 빈 결과로 바꾸지 않고 FeedReadError로 올린다. ---
    try:
        patients = []
        for row in patient_rows:
            pid = row["patient_id"]
            patients.append(FeedReadManagedPatient(
                **row,
                clinical_assessments=FeedReadCollection[FeedReadClinicalAssessment](
                    items=[FeedReadClinicalAssessment(**r) for r in assessments.get(pid, [])],
                    total_count=assessment_counts.get(pid, 0),
                ),
                safety_events=FeedReadCollection[FeedReadSafetyEvent](
                    items=[FeedReadSafetyEvent(**r) for r in safety.get(pid, [])],
                    total_count=safety_counts.get(pid, 0),
                ),
                medications=FeedReadCollection[FeedReadMedication](
                    items=[FeedReadMedication(**r) for r in medications.get(pid, [])],
                    total_count=medication_counts.get(pid, 0),
                ),
                medical_visits=FeedReadCollection[FeedReadMedicalVisit](
                    items=[FeedReadMedicalVisit(**r) for r in visits.get(pid, [])],
                    total_count=visit_counts.get(pid, 0),
                ),
            ))

        snapshot = FeedReadSnapshot(
            caregiver_profile=(
                None if caregiver_row is None else FeedReadCaregiverProfile(**caregiver_row)
            ),
            managed_patients=FeedReadCollection[FeedReadManagedPatient](
                items=patients, total_count=len(patients),
            ),
            recent_care_logs=FeedReadCollection[FeedReadCareLog](
                items=[FeedReadCareLog(**r) for r in care_log_rows],
                total_count=care_log_total,
            ),
            long_term_summary=(
                None if summary_row is None else FeedReadLongTermSummary(**summary_row)
            ),
        )
    except ValidationError as err:
        raise FeedReadError(
            f"피드 컨텍스트가 계약을 어겼습니다 (user_id={user_id}): {err}"
        ) from err

    logger.info(
        "get_feed_context_snapshot: user_id=%s 환자 %d명, 돌봄기록 %d/%d건, 요약 %s",
        user_id, len(patients), len(care_log_rows), care_log_total,
        "있음" if summary_row else "없음",
    )
    return snapshot


# ============================================================
# 쓰기 — 돌봄 기록 / 월간 요약
# ============================================================

_SAVE_CARE_LOG = """
    INSERT INTO care_logs (user_id, patient_id, log_type, content, mood_tag, logged_at)
    VALUES (%(user_id)s, %(patient_id)s, %(log_type)s, %(content)s, %(mood_tag)s,
            COALESCE(%(logged_at)s, now()))
    RETURNING id
"""

# 그 간병인의 환자인지 확인한다 (다른 간병인의 환자에 기록을 붙일 수 없게).
_PATIENT_BELONGS_TO = """
    SELECT 1 AS ok FROM patient_profiles WHERE id = %(patient_id)s AND caregiver_id = %(user_id)s
"""


def save_care_log(
    user_id: UUID,
    log_type: str,
    content: Optional[str] = None,
    mood_tag: Optional[str] = None,
    patient_id: Optional[UUID] = None,
    logged_at: Optional[datetime] = None,
) -> UUID:
    """돌봄·자기돌봄 기록 1건을 저장한다.

    Args:
        user_id: 기록한 간병인 (users.id).
        log_type: patient_care / caregiver_selfcare. 비울 수 없다.
        content: 기록 내용 (자유 서술).
        mood_tag: 기록 당시 기분 태그.
        patient_id: 관련 환자. None이면 '환자 미연결 기록'(간병인 본인)이다.
        logged_at: 기록 시각. None이면 DB의 now()를 쓴다.

    Returns:
        저장된 care_logs row의 id.

    Raises:
        ValueError: log_type이 비었거나, 없는 사용자이거나,
            patient_id가 이 간병인의 환자가 아닌 경우.
    """
    if not log_type or not log_type.strip():
        raise ValueError("log_type은 비울 수 없습니다 (care_logs.log_type이 NOT NULL).")

    params = {
        "user_id": user_id,
        "patient_id": patient_id,
        "log_type": log_type,
        "content": content,
        "mood_tag": mood_tag,
        "logged_at": logged_at,
    }
    with get_connection() as conn:
        with conn.cursor() as cur:
            if patient_id is not None:
                cur.execute(_PATIENT_BELONGS_TO, {"patient_id": patient_id, "user_id": user_id})
                if cur.fetchone() is None:
                    raise ValueError(
                        f"이 간병인의 환자가 아닙니다: patient_id={patient_id}, user_id={user_id}"
                    )
            try:
                cur.execute(_SAVE_CARE_LOG, params)
            except ForeignKeyViolation as err:
                raise ValueError(f"존재하지 않는 사용자입니다: user_id={user_id}") from err
            log_id = cur.fetchone()[0]

    logger.info("save_care_log: user_id=%s patient_id=%s 저장 (id=%s)", user_id, patient_id, log_id)
    return log_id


# (user_id, period_start, period_end) UNIQUE 인덱스가 ON CONFLICT 대상이다 → 006 마이그레이션.
_SAVE_PROFILE_SUMMARY = """
    INSERT INTO profile_summaries (user_id, period_start, period_end, summary, updated_tags)
    VALUES (%(user_id)s, %(period_start)s, %(period_end)s, %(summary)s, %(updated_tags)s)
    ON CONFLICT (user_id, period_start, period_end) DO UPDATE
       SET summary      = EXCLUDED.summary,
           updated_tags = EXCLUDED.updated_tags,
           generated_at = now()
    RETURNING id
"""


def save_profile_summary(
    user_id: UUID,
    period_start: date,
    period_end: date,
    summary: Optional[str] = None,
    updated_tags: Optional[list[str]] = None,
) -> UUID:
    """월간 요약을 저장한다. 같은 (user_id, period_start, period_end)면 갱신한다 (upsert).

    같은 기간을 다시 요약하면 행이 늘지 않고 기존 행을 덮어쓰며, generated_at을 다시 찍는다.

    Args:
        user_id: 대상 사용자 (users.id).
        period_start: 요약 기간 시작일.
        period_end: 요약 기간 종료일. period_start보다 앞일 수 없다.
        summary: 요약 본문.
        updated_tags: 갱신된 개인화 태그. None이면 빈 목록.

    Returns:
        저장/갱신된 profile_summaries row의 id (갱신이면 기존 id 그대로).

    Raises:
        ValueError: 기간이 거꾸로이거나, 없는 사용자인 경우.
    """
    if period_end < period_start:
        raise ValueError(
            f"period_end는 period_start보다 앞일 수 없습니다: {period_start} ~ {period_end}"
        )

    params = {
        "user_id": user_id,
        "period_start": period_start,
        "period_end": period_end,
        "summary": summary,
        "updated_tags": updated_tags or [],
    }
    with get_connection() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(_SAVE_PROFILE_SUMMARY, params)
            except ForeignKeyViolation as err:
                raise ValueError(f"존재하지 않는 사용자입니다: user_id={user_id}") from err
            summary_id = cur.fetchone()[0]

    logger.info(
        "save_profile_summary: user_id=%s %s~%s 저장 (id=%s)",
        user_id, period_start, period_end, summary_id,
    )
    return summary_id
