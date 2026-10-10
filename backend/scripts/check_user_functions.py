"""user_functions.py(사용자·환자 데이터 접근 함수)를 실 DB에서 점검한다.

테스트용 간병인 2명(A, B)과 그 환자들을 만들어 저장·조회·수정·입력 검증을 차례로 확인하고,
끝나면 테스트 사용자를 지워 흔적을 남기지 않는다. 사용자·환자 관련 테이블은 모두
ON DELETE CASCADE로 users에 매달려 있어서, 테스트 사용자만 지우면 아래 데이터가 함께 지워진다.

★ 직접 SQL 예외 ★
user_functions에는 삭제 함수가 없어서, 스키마 확인과 테스트 데이터 정리에만 get_connection()으로
SQL을 직접 쓴다. 점검 대상 동작은 모두 user_functions의 함수로만 호출한다.

테스트 사용자 이메일은 'ufcheck-<실행마다 다른 8자리>-x@example.com'이라 실제 데이터와 겹치지 않고,
정리도 이 실행에서 만든 사용자만 지운다.

실행 (backend/ 에서, 로컬 DB를 띄운 상태로):
    python -m scripts.check_user_functions
    python -m scripts.check_user_functions --keep     # 정리하지 않고 남겨서 DB에서 직접 확인
"""
import argparse
import random
import sys
from datetime import timedelta
from typing import Any, Callable, Optional
from uuid import UUID, uuid4

import psycopg
from pydantic import BaseModel

from app.db import user_functions as uf
from app.db.connection import get_connection
from app.user_schemas import PROFILE_RECENT_VISITS, PROFILE_SAFETY_DAYS, PatientIn, UserIn, today

# user_functions가 다루는 테이블 (004·005 마이그레이션까지 적용돼 있어야 한다).
REQUIRED_TABLES = (
    "users", "patient_profiles", "caregiver_profiles",
    "clinical_assessments", "safety_events", "medications", "medical_visits",
)
# 환자에 매달린 테이블 — 정리 후 남은 행이 없는지 확인할 때 쓴다.
PATIENT_CHILD_TABLES = ("clinical_assessments", "safety_events", "medications", "medical_visits")


def day(offset: int) -> str:
    """오늘(한국 시간) 기준 offset일 뒤 날짜를 'YYYY-MM-DD'로. 음수면 과거."""
    return (today() + timedelta(days=offset)).isoformat()


def same(left: Optional[BaseModel], right: BaseModel) -> bool:
    """두 결과 모델의 값이 같은지 비교한다.

    모델끼리 ==로 비교하면 pydantic 버전에 따라 "명시적으로 채운 필드 목록"까지 비교해서,
    값이 같아도 다르다고 나올 수 있다 (create_user는 patient_ids 없이, get_user는 채워서 만든다).
    """
    return left is not None and left.model_dump() == right.model_dump()


class Checker:
    """점검 결과를 한 줄씩 출력하고 집계한다."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def ok(self, name: str, condition: bool, detail: Any = "") -> bool:
        if condition:
            self.passed += 1
            print(f"  [OK]   {name}")
        else:
            self.failed.append(name)
            print(f"  [FAIL] {name}" + (f"\n         → {detail}" if detail != "" else ""))
        return condition

    def raises(self, name: str, func: Callable[..., Any], *args: Any, contains: str) -> bool:
        """func(*args)가 메시지에 contains가 든 ValueError를 던지는지 확인한다."""
        try:
            func(*args)
        except ValueError as error:
            return self.ok(name, contains in str(error), f"메시지에 '{contains}'가 없습니다: {error}")
        except Exception as error:
            return self.ok(name, False, f"ValueError가 아닌 {type(error).__name__}: {error}")
        return self.ok(name, False, "예외가 나지 않았습니다 (저장됐다면 정리 단계에서 지워진다)")


# ============================================================
# 점검 항목 — 앞 단계에서 만든 id를 ctx에 담아 다음 단계가 쓴다.
# ============================================================


def check_users(c: Checker, ctx: dict[str, Any]) -> None:
    run, phone = ctx["run"], ctx["phone"]

    a = uf.create_user({
        "name": "  점검용 간병인A  ",
        "email": f"  UFCheck-{run}-A@Example.com ",
        "phone_number": f"{phone[:3]}-{phone[3:7]}-{phone[7:]}",
    })
    ctx["user_a"] = a.id
    c.ok("create_user(dict): 이름 앞뒤 공백 제거", a.name == "점검용 간병인A", repr(a.name))
    c.ok("create_user(dict): 이메일 소문자·공백 정리", a.email == f"ufcheck-{run}-a@example.com", a.email)
    c.ok("create_user(dict): 휴대폰 번호는 숫자만 저장", a.phone_number == phone, repr(a.phone_number))
    c.ok("create_user: 막 가입한 사용자의 patient_ids는 빈 목록", a.patient_ids == [], a.patient_ids)

    b = uf.create_user(UserIn(name="점검용 간병인B", email=f"ufcheck-{run}-b@example.com"))
    ctx["user_b"] = b.id
    c.ok("create_user(UserIn 모델 입력)", b.email == f"ufcheck-{run}-b@example.com" and b.phone_number is None)

    c.ok("get_user(UUID 문자열)", same(uf.get_user(str(a.id)), a))
    c.ok("get_user(없는 id) → None", uf.get_user(uuid4()) is None)
    c.ok("get_user(형식이 틀린 id) → None", uf.get_user("not-a-uuid") is None)

    c.raises("create_user: 이메일 중복(대소문자만 다름) 거부", uf.create_user,
             {"name": "중복", "email": f"UFCHECK-{run}-A@example.com"}, contains="이미 사용 중인 이메일")
    c.raises("create_user: 휴대폰 번호 중복 거부", uf.create_user,
             {"name": "중복", "email": f"ufcheck-{run}-c@example.com", "phone_number": phone},
             contains="이미 사용 중인 휴대폰")
    c.raises("create_user: 이메일 형식 오류", uf.create_user,
             {"name": "x", "email": "not-an-email"}, contains="이메일 형식")
    c.raises("create_user: 휴대폰 번호 형식 오류", uf.create_user,
             {"name": "x", "email": f"ufcheck-{run}-d@example.com", "phone_number": "02-123-4567"},
             contains="휴대폰 번호 형식")
    c.raises("create_user: 필수 필드(name) 누락", uf.create_user,
             {"email": f"ufcheck-{run}-e@example.com"}, contains="필수 필드가 없습니다: name")
    c.raises("create_user: 모르는 필드(phone) 거부", uf.create_user,
             {"name": "x", "email": f"ufcheck-{run}-f@example.com", "phone": "01012345678"},
             contains="알 수 없는 필드")

    renamed = uf.update_user(a.id, {"name": "새 이름"})
    c.ok("update_user: 보낸 필드만 변경",
         renamed.name == "새 이름" and renamed.email == a.email and renamed.phone_number == a.phone_number)
    c.ok("update_user: updated_at 갱신", renamed.updated_at > a.updated_at)
    cleared = uf.update_user(str(a.id), {"phone_number": None})
    c.ok("update_user: phone_number null → 번호 삭제", cleared.phone_number is None)
    c.ok("update_user({}) → 현재 값 그대로", same(uf.update_user(a.id, {}), cleared))
    c.ok("update_user(없는 id) → None", uf.update_user(uuid4(), {"name": "x"}) is None)
    c.raises("update_user: name null 거부", uf.update_user, a.id, {"name": None}, contains="비울 수 없습니다")


def check_patients(c: Checker, ctx: dict[str, Any]) -> None:
    a, b = ctx["user_a"], ctx["user_b"]

    p1 = uf.create_patient({
        "caregiver_id": str(a),
        "name": " 어머니 ",
        "dementia_stage": "경도",
        "diagnosis_date": day(-365),
        "symptoms": [" 수면장애 ", "배회", "수면장애", "  "],
        "interests": None,
    })
    c.ok("create_patient: caregiver_id 문자열 → UUID", p1.caregiver_id == a)
    c.ok("create_patient: 목록의 공백·중복·빈 값 정리", p1.symptoms == ["수면장애", "배회"], p1.symptoms)
    c.ok("create_patient: interests null → 빈 목록", p1.interests == [], p1.interests)

    p2 = uf.create_patient(PatientIn(caregiver_id=a, name="아버지"))
    pb = uf.create_patient({"caregiver_id": b, "name": "B의 환자"})
    ctx.update(patient_1=p1.id, patient_2=p2.id, patient_b=pb.id)

    c.ok("get_patient", same(uf.get_patient(p1.id), p1))
    c.ok("get_patient(없는 id) → None", uf.get_patient(uuid4()) is None)
    c.ok("list_patients: 등록 순서", [p.id for p in uf.list_patients(a)] == [p1.id, p2.id])
    c.ok("get_user: patient_ids가 list_patients와 같은 순서", uf.get_user(a).patient_ids == [p1.id, p2.id])
    c.ok("list_patients(형식이 틀린 id) → []", uf.list_patients("bad") == [])

    c.ok("is_caregiver_of: 본인 환자 → True", uf.is_caregiver_of(a, p1.id) is True)
    c.ok("is_caregiver_of: 다른 간병인의 환자 → False", uf.is_caregiver_of(b, p1.id) is False)
    c.ok("is_caregiver_of: 형식이 틀린 id → False", uf.is_caregiver_of("bad", p1.id) is False)

    updated = uf.update_patient(p1.id, {"dementia_stage": "중등도", "symptoms": None})
    c.ok("update_patient: 보낸 필드만 변경, symptoms null → 빈 목록",
         updated.dementia_stage == "중등도" and updated.symptoms == [] and updated.name == "어머니")
    c.ok("update_patient(없는 id) → None", uf.update_patient(uuid4(), {"name": "x"}) is None)
    c.raises("update_patient: caregiver_id 변경 거부", uf.update_patient, p1.id,
             {"caregiver_id": str(b)}, contains="알 수 없는 필드")

    c.raises("create_patient: 목록에 없는 진단 단계", uf.create_patient,
             {"caregiver_id": a, "dementia_stage": "초기"}, contains="dementia_stage")
    c.raises("create_patient: 미래 진단일", uf.create_patient,
             {"caregiver_id": a, "diagnosis_date": day(1)}, contains="미래 날짜")
    c.raises("create_patient: 없는 간병인", uf.create_patient,
             {"caregiver_id": str(uuid4())}, contains="존재하지 않는 사용자")


def check_caregiver_profile(c: Checker, ctx: dict[str, Any]) -> None:
    a, b = ctx["user_a"], ctx["user_b"]

    c.ok("get_caregiver_profile: 저장 전 → None", uf.get_caregiver_profile(a) is None)
    first = uf.upsert_caregiver_profile(a, {"relationship": "자녀", "burden_score": 32, "lifestyle_tags": ["직장병행"]})
    c.ok("upsert_caregiver_profile: 처음 저장",
         first.relationship == "자녀" and first.burden_score == 32 and first.mood_score is None)
    second = uf.upsert_caregiver_profile(str(a), {"mood_score": 8})
    c.ok("upsert_caregiver_profile: 다시 저장하면 보낸 필드만 변경, 나머지 유지",
         second.relationship == "자녀" and second.burden_score == 32
         and second.mood_score == 8 and second.lifestyle_tags == ["직장병행"])
    c.ok("upsert_caregiver_profile: updated_at 갱신", second.updated_at > first.updated_at)
    c.ok("get_caregiver_profile: 마지막 저장 값과 같음", same(uf.get_caregiver_profile(a), second))
    c.ok("upsert_caregiver_profile({}), 저장 이력 없음 → None", uf.upsert_caregiver_profile(b, {}) is None)

    c.raises("upsert_caregiver_profile: burden_score 범위 초과(89)", uf.upsert_caregiver_profile, a,
             {"burden_score": 89}, contains="burden_score 범위")
    c.raises("upsert_caregiver_profile: mood_score 정수 아님(1.5)", uf.upsert_caregiver_profile, a,
             {"mood_score": 1.5}, contains="정수")
    c.raises("upsert_caregiver_profile: 없는 사용자", uf.upsert_caregiver_profile, uuid4(),
             {"relationship": "자녀"}, contains="존재하지 않는 사용자")


def check_assessments(c: Checker, ctx: dict[str, Any]) -> None:
    p1 = ctx["patient_1"]

    names = {t.assessment_type for t in uf.list_assessment_types()}
    c.ok("list_assessment_types: K-MMSE·CDR 포함", {"K-MMSE", "CDR"} <= names, sorted(names))

    old = uf.create_assessment({
        "patient_id": str(p1), "assessment_type": "k-mmse", "score": "24",
        "assessed_at": day(-60), "assessed_by": "OO병원",
    })
    c.ok("create_assessment: 검사명 별칭 → 표준 이름, 문자열 점수 → 숫자",
         old.assessment_type == "K-MMSE" and old.score == 24, (old.assessment_type, old.score))
    cdr = uf.create_assessment({"patient_id": p1, "assessment_type": "CDR", "score": 0.5, "assessed_at": day(-30)})
    c.ok("create_assessment: CDR 0.5 저장", cdr.score == 0.5, cdr.score)
    new = uf.create_assessment({"patient_id": p1, "assessment_type": "K MMSE", "score": 22, "assessed_at": day(-1)})

    c.ok("get_assessment", same(uf.get_assessment(old.id), old))
    c.ok("get_assessment(없는 id) → None", uf.get_assessment(uuid4()) is None)
    c.ok("list_assessments: 최근 평가일부터",
         [x.id for x in uf.list_assessments(p1)] == [new.id, cdr.id, old.id])
    c.ok("list_assessments: 검사명 필터(별칭 'KMMSE')",
         [x.id for x in uf.list_assessments(p1, "KMMSE")] == [new.id, old.id])
    c.ok("list_assessments: limit", len(uf.list_assessments(p1, limit=1)) == 1)
    c.raises("list_assessments: 지원하지 않는 검사명 필터", uf.list_assessments, p1, "XYZ",
             contains="지원하지 않는 검사명")
    latest = uf.get_latest_assessments(p1)
    c.ok("get_latest_assessments: 검사별 최신 1건",
         {(x.assessment_type, x.id) for x in latest} == {("CDR", cdr.id), ("K-MMSE", new.id)},
         [(x.assessment_type, x.score) for x in latest])

    base = {"patient_id": p1, "assessed_at": day(-1)}
    c.raises("create_assessment: CDR 허용 값이 아님(0.7)", uf.create_assessment,
             {**base, "assessment_type": "CDR", "score": 0.7}, contains="중 하나여야")
    c.raises("create_assessment: K-MMSE 범위 초과(31)", uf.create_assessment,
             {**base, "assessment_type": "K-MMSE", "score": 31}, contains="범위는")
    c.raises("create_assessment: K-MMSE 정수 아님(24.5)", uf.create_assessment,
             {**base, "assessment_type": "K-MMSE", "score": 24.5}, contains="정수로만")
    c.raises("create_assessment: 지원하지 않는 검사명", uf.create_assessment,
             {**base, "assessment_type": "ABC", "score": 1}, contains="지원하지 않는 검사명")
    c.raises("create_assessment: 미래 평가일", uf.create_assessment,
             {**base, "assessment_type": "K-MMSE", "assessed_at": day(1)}, contains="미래 날짜")
    c.raises("create_assessment: 없는 환자", uf.create_assessment,
             {**base, "patient_id": str(uuid4()), "assessment_type": "K-MMSE"}, contains="존재하지 않는 환자")

    edited = uf.update_assessment(old.id, {"score": 23})
    c.ok("update_assessment: 점수만 수정, 나머지 유지",
         edited.score == 23 and edited.assessment_type == "K-MMSE" and edited.assessed_by == "OO병원")
    c.raises("update_assessment: 검사명만 바꿔 기존 점수가 새 범위 밖 → 거부", uf.update_assessment, old.id,
             {"assessment_type": "CDR"}, contains="중 하나여야")
    c.raises("update_assessment: patient_id 변경 거부", uf.update_assessment, old.id,
             {"patient_id": str(uuid4())}, contains="알 수 없는 필드")
    cleared = uf.update_assessment(old.id, {"score": None})
    c.ok("update_assessment: score null → 점수 삭제", cleared.score is None)
    c.ok("update_assessment(없는 id) → None", uf.update_assessment(uuid4(), {"score": 1}) is None)


def check_safety_events(c: Checker, ctx: dict[str, Any]) -> None:
    p1 = ctx["patient_1"]
    d1, d2 = day(-2), day(-1)

    first = uf.upsert_safety_event({"patient_id": p1, "event_date": d1, "has_fall": True})
    c.ok("upsert_safety_event: 처음 기록 — 보낸 값만 true, 나머지 false",
         first.has_fall and not first.has_wandering and not first.has_missing)
    second = uf.upsert_safety_event({"patient_id": str(p1), "event_date": d1, "has_wandering": True, "note": "새벽 현관"})
    c.ok("upsert_safety_event: 같은 날 다시 기록하면 같은 행(id)", second.id == first.id)
    c.ok("upsert_safety_event: 이전 체크 유지 + 새 체크 반영",
         second.has_fall and second.has_wandering and second.note == "새벽 현관")
    quiet = uf.upsert_safety_event({"patient_id": p1, "event_date": d2})
    c.ok("upsert_safety_event: 날짜만 보내면 '이상 없음' 기록",
         not (quiet.has_fall or quiet.has_wandering or quiet.has_missing))

    c.ok("get_safety_event", same(uf.get_safety_event(first.id), second))
    c.ok("list_safety_events: 최근 날짜부터", [e.id for e in uf.list_safety_events(p1)] == [quiet.id, first.id])
    c.ok("list_safety_events: 하루 범위(start = end)",
         [e.id for e in uf.list_safety_events(p1, start_date=d1, end_date=d1)] == [first.id])
    c.raises("list_safety_events: end_date가 start_date보다 빠름", uf.list_safety_events, p1, d2, d1,
             contains="빠를 수 없습니다")

    c.raises("upsert_safety_event: 미래 날짜", uf.upsert_safety_event,
             {"patient_id": p1, "event_date": day(1)}, contains="미래 날짜")
    c.raises("upsert_safety_event: has_fall에 문자열 'true'", uf.upsert_safety_event,
             {"patient_id": p1, "event_date": d1, "has_fall": "true"}, contains="true/false")
    c.raises("upsert_safety_event: 없는 환자", uf.upsert_safety_event,
             {"patient_id": str(uuid4()), "event_date": d1}, contains="존재하지 않는 환자")


def check_medications(c: Checker, ctx: dict[str, Any]) -> None:
    p1 = ctx["patient_1"]

    m1 = uf.create_medication({
        "patient_id": p1, "drug_name": " 도네페질 5mg ", "dosage": "1정",
        "frequency": "1일 1회 취침 전", "start_date": day(-90),
    })
    c.ok("create_medication: 기본 is_taking=true, 약품명 공백 정리",
         m1.is_taking is True and m1.drug_name == "도네페질 5mg", (m1.is_taking, m1.drug_name))
    m2 = uf.create_medication({
        "patient_id": str(p1), "drug_name": "메만틴", "is_taking": False,
        "start_date": day(-200), "end_date": day(-100),
    })

    c.ok("get_medication", same(uf.get_medication(m1.id), m1))
    c.ok("list_medications: 복용 중인 약 먼저", [m.id for m in uf.list_medications(p1)] == [m1.id, m2.id])
    c.ok("list_medications(is_taking=True)", [m.id for m in uf.list_medications(p1, is_taking=True)] == [m1.id])
    c.raises("list_medications: is_taking에 문자열 'true'", uf.list_medications, p1, "true", contains="true/false")

    stopped = uf.update_medication(m1.id, {"is_taking": False, "end_date": day(0)})
    c.ok("update_medication: 복용 중단 (다른 필드는 유지)",
         stopped.is_taking is False and stopped.end_date == today() and stopped.dosage == "1정")
    c.ok("update_medication: updated_at 갱신", stopped.updated_at > m1.updated_at)
    c.raises("update_medication: 종료일이 기존 시작일보다 빠름", uf.update_medication, m1.id,
             {"end_date": day(-91)}, contains="빠를 수 없습니다")

    c.raises("create_medication: 종료일이 시작일보다 빠름", uf.create_medication,
             {"patient_id": p1, "drug_name": "x", "start_date": day(0), "end_date": day(-1)},
             contains="빠를 수 없습니다")
    c.raises("create_medication: 약품명이 공백뿐", uf.create_medication,
             {"patient_id": p1, "drug_name": "   "}, contains="비울 수 없습니다")
    c.raises("create_medication: is_taking에 1", uf.create_medication,
             {"patient_id": p1, "drug_name": "x", "is_taking": 1}, contains="true/false")
    c.raises("create_medication: 없는 환자", uf.create_medication,
             {"patient_id": str(uuid4()), "drug_name": "x"}, contains="존재하지 않는 환자")


def check_medical_visits(c: Checker, ctx: dict[str, Any]) -> None:
    p1 = ctx["patient_1"]

    plan = uf.create_medical_visit({
        "patient_id": p1, "visit_date": day(14), "hospital_name": "OO병원",
        "department": "신경과", "visit_reason": "정기 진료",
    })
    c.ok("create_medical_visit: 예약(미래 날짜), 기본 is_visited=false", plan.is_visited is False)
    done = uf.create_medical_visit({
        "patient_id": str(p1), "visit_date": day(-7), "is_visited": True, "diagnosis": "경도 진행",
    })
    c.raises("create_medical_visit: 미래 날짜를 방문 완료로 저장 거부", uf.create_medical_visit,
             {"patient_id": p1, "visit_date": day(14), "is_visited": True}, contains="방문 완료")
    c.raises("create_medical_visit: 없는 환자", uf.create_medical_visit,
             {"patient_id": str(uuid4()), "visit_date": day(0)}, contains="존재하지 않는 환자")

    c.ok("get_medical_visit", same(uf.get_medical_visit(plan.id), plan))
    c.ok("list_medical_visits: 최근 날짜부터", [v.id for v in uf.list_medical_visits(p1)] == [plan.id, done.id])
    c.ok("list_medical_visits(is_visited=False): 예약만",
         [v.id for v in uf.list_medical_visits(p1, is_visited=False)] == [plan.id])
    c.ok("list_medical_visits(is_visited=True): 다녀온 진료만",
         [v.id for v in uf.list_medical_visits(p1, is_visited=True)] == [done.id])
    c.ok("list_medical_visits(start_date=오늘): 다가오는 예약",
         [v.id for v in uf.list_medical_visits(p1, start_date=day(0))] == [plan.id])

    c.raises("update_medical_visit: 미래 예약을 방문 완료로 → 거부", uf.update_medical_visit, plan.id,
             {"is_visited": True}, contains="방문 완료")
    c.raises("update_medical_visit: 방문 완료 기록의 날짜를 미래로 → 거부", uf.update_medical_visit, done.id,
             {"visit_date": day(3)}, contains="방문 완료")
    noted = uf.update_medical_visit(done.id, {"doctor_note": "낙상 주의", "diagnosis": None})
    c.ok("update_medical_visit: 보낸 필드만 변경, null → 값 삭제",
         noted.doctor_note == "낙상 주의" and noted.diagnosis is None and noted.is_visited is True)
    c.ok("update_medical_visit: updated_at 갱신", noted.updated_at > done.updated_at)
    ctx["visit_done"] = done.id
    ctx["visit_plan"] = plan.id


def check_list_users(c: Checker, ctx: dict[str, Any]) -> None:
    a, b = ctx["user_a"], ctx["user_b"]

    # 실 DB에는 다른 사용자(시드 등)도 있으므로 전체를 받아 테스트 사용자만 골라 본다.
    everyone = uf.list_users(limit=1_000_000)
    ids = [u.id for u in everyone]
    c.ok("list_users: 테스트 사용자 A·B 포함, 가입 순(A → B)", a in ids and b in ids and ids.index(a) < ids.index(b))
    by_id = {u.id: u for u in everyone}
    c.ok("list_users: patient_count (A 2명, B 1명)",
         by_id[a].patient_count == 2 and by_id[b].patient_count == 1,
         (by_id[a].patient_count, by_id[b].patient_count))
    c.ok("list_users: email 포함", by_id[a].email == f"ufcheck-{ctx['run']}-a@example.com", by_id[a].email)
    c.ok("list_users: limit / offset으로 다음 페이지",
         [u.id for u in uf.list_users(limit=1, offset=ids.index(b))] == [b])
    c.ok("list_users(limit=0) → []", uf.list_users(limit=0) == [])
    c.raises("list_users: 음수 offset 거부", uf.list_users, 10, -1, contains="offset")


def _keys(value: Any) -> set[str]:
    """dict / list 안의 모든 키 이름 (중첩 포함)."""
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def check_profile_context(c: Checker, ctx: dict[str, Any]) -> None:
    a, b, p1, p2 = ctx["user_a"], ctx["user_b"], ctx["patient_1"], ctx["patient_2"]
    days = PROFILE_SAFETY_DAYS

    # 앞 단계 기록에 경계값을 더한다:
    # 안전 기록 — 기간 첫날(포함)·기간 밖(제외), 복약 — 복용 중 1건(앞 단계 약은 모두 중단),
    # 진료 — 다녀온 진료를 3건 넘게, 예약을 하나 더(가까운 날짜).
    edge_in = uf.upsert_safety_event({"patient_id": p1, "event_date": day(-(days - 1)), "has_missing": True})
    uf.upsert_safety_event({"patient_id": p1, "event_date": day(-days), "has_fall": True})
    taking = uf.create_medication({"patient_id": p1, "drug_name": "갈란타민", "start_date": day(-10)})
    old_visits = [
        uf.create_medical_visit({"patient_id": p1, "visit_date": day(-n), "is_visited": True})
        for n in (30, 60, 90)
    ]
    soon = uf.create_medical_visit({"patient_id": p1, "visit_date": day(3)})

    profile = uf.get_user_profile_context(str(a))
    c.ok("get_user_profile_context: 사용자·환자 순서",
         profile.user_id == a and [p.patient.id for p in profile.patients] == [p1, p2])
    c.ok("get_user_profile_context: 간병인 자가점검 포함",
         profile.caregiver_profile is not None and profile.caregiver_profile.mood_score == 8)

    first = profile.patients[0]
    c.ok("검사별 최신 평가 = get_latest_assessments()",
         [x.id for x in first.latest_assessments] == [x.id for x in uf.get_latest_assessments(p1)])
    c.ok("복용 중인 약만", [m.id for m in first.current_medications] == [taking.id],
         [m.drug_name for m in first.current_medications])

    fall_day = uf.list_safety_events(p1, start_date=day(-2), end_date=day(-2))[0]
    c.ok(f"안전 기록: 최근 {days}일 + 이벤트 있는 날만 (이상 없는 날·기간 밖 제외, 첫날 포함)",
         [e.id for e in first.recent_safety_events] == [fall_day.id, edge_in.id],
         [str(e.event_date) for e in first.recent_safety_events])
    c.ok(f"다녀온 진료 최근 {PROFILE_RECENT_VISITS}건, 최근 날짜부터",
         [v.id for v in first.recent_visits] == [ctx["visit_done"], old_visits[0].id, old_visits[1].id],
         [str(v.visit_date) for v in first.recent_visits])
    c.ok("다가오는 예약: 가까운 날짜부터",
         [v.id for v in first.upcoming_visits] == [soon.id, ctx["visit_plan"]],
         [str(v.visit_date) for v in first.upcoming_visits])

    empty = profile.patients[1]
    c.ok("기록 없는 환자 → 목록이 모두 비어 있음",
         not (empty.latest_assessments or empty.current_medications or empty.recent_safety_events
              or empty.recent_visits or empty.upcoming_visits))

    keys = _keys(profile.model_dump(mode="json"))
    c.ok("개인정보(email / phone_number) 미포함", not keys & {"email", "phone_number"}, keys & {"email", "phone_number"})
    c.ok("model_dump_json(): JSON 텍스트로 변환", '"user_id"' in profile.model_dump_json())

    other = uf.get_user_profile_context(b)
    c.ok("자가점검 없는 사용자 → caregiver_profile null", other.caregiver_profile is None)
    c.ok("get_user_profile_context(없는 id) → None", uf.get_user_profile_context(uuid4()) is None)
    c.ok("get_user_profile_context(형식이 틀린 id) → None", uf.get_user_profile_context("bad") is None)

    caregiver = uf.get_caregiver_context(str(a))
    c.ok("get_caregiver_context: 이름·자가점검·환자 id 목록 = 프로필 묶음과 같음",
         caregiver.name == profile.name and caregiver.patient_ids == [p1, p2]
         and caregiver.caregiver_profile == profile.caregiver_profile)
    c.ok("get_caregiver_context: 개인정보 미포함", not _keys(caregiver.model_dump(mode="json")) & {"email", "phone_number"})
    c.ok("get_caregiver_context(없는 id) → None", uf.get_caregiver_context(uuid4()) is None)

    c.ok("get_patient_context = 프로필 묶음의 같은 환자 항목",
         uf.get_patient_context(str(p1)).model_dump() == first.model_dump())
    c.ok("get_patient_context(없는 id) → None", uf.get_patient_context(uuid4()) is None)
    c.ok("get_patient_context(형식이 틀린 id) → None", uf.get_patient_context("bad") is None)


def check_api(c: Checker, ctx: dict[str, Any]) -> None:
    from fastapi.testclient import TestClient  # 점검할 때만 앱을 띄운다

    from main import app

    client = TestClient(app)
    a, b, p1 = ctx["user_a"], ctx["user_b"], ctx["patient_1"]

    caregiver = client.get(f"/feed_user/{a}")
    c.ok("GET /feed_user/{user_id} → 200, 환자 id 목록 포함",
         caregiver.status_code == 200 and caregiver.json()["patient_ids"] == [str(p1), str(ctx["patient_2"])],
         caregiver.status_code)
    c.ok("GET /feed_user/{user_id}: 없는 id → 404", client.get(f"/feed_user/{uuid4()}").status_code == 404)

    patient = client.get(f"/feed_patient/{a}/{p1}")
    c.ok("GET /feed_patient/{user_id}/{patient_id} → 200",
         patient.status_code == 200 and patient.json()["patient"]["id"] == str(p1), patient.status_code)
    c.ok("환자 API: 다른 간병인의 환자 → 404", client.get(f"/feed_patient/{b}/{p1}").status_code == 404)
    c.ok("환자 API: 없는 환자 → 404", client.get(f"/feed_patient/{a}/{uuid4()}").status_code == 404)
    c.ok("환자 API: UUID 형식이 아님 → 422", client.get(f"/feed_patient/{a}/bad").status_code == 422)

    ok = client.get(f"/feed/{a}")
    c.ok("GET /feed/{user_id} → 200", ok.status_code == 200, ok.status_code)
    c.ok("응답 = get_user_profile_context()와 같은 환자 목록",
         [p["patient"]["id"] for p in ok.json()["patients"]] == [str(ctx["patient_1"]), str(ctx["patient_2"])])
    missing = client.get(f"/feed/{uuid4()}")
    c.ok("없는 user_id → 404", missing.status_code == 404, missing.status_code)
    bad = client.get("/feed/not-a-uuid")
    c.ok("UUID 형식이 아님 → 422", bad.status_code == 422, bad.status_code)


SECTIONS: list[tuple[str, Callable[[Checker, dict[str, Any]], None]]] = [
    ("users (간병인 계정)", check_users),
    ("patient_profiles (환자)", check_patients),
    ("caregiver_profiles (간병인 자가점검)", check_caregiver_profile),
    ("clinical_assessments (치매 평가)", check_assessments),
    ("safety_events (안전·행동 기록)", check_safety_events),
    ("medications (복약)", check_medications),
    ("medical_visits (병원 방문)", check_medical_visits),
    ("list_users (사용자 목록)", check_list_users),
    ("get_user_profile_context (프로필 묶음)", check_profile_context),
    ("API: /feed_user · /feed_patient · /feed", check_api),
]


# ============================================================
# 스키마 확인 / 테스트 데이터 정리 (직접 SQL — 이 스크립트에서만)
# ============================================================


def check_schema() -> None:
    """user_functions가 쓰는 테이블·컬럼이 DB에 있는지 확인한다 (004·005 마이그레이션 적용 여부)."""
    with get_connection() as conn:
        missing = [
            table for table in REQUIRED_TABLES
            if conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0] is None
        ]
        has_005 = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'medical_visits' AND column_name = 'follow_up_plan'"
        ).fetchone()
    if missing:
        raise SystemExit(
            f"DB에 테이블이 없습니다: {', '.join(missing)}\n"
            "  → 004_patient_management.sql 이후 마이그레이션이 적용되지 않은 DB입니다."
        )
    if not has_005:
        raise SystemExit("medical_visits에 005 컬럼(follow_up_plan 등)이 없습니다 → 005_medical_visits_detail.sql 미적용.")


def cleanup(run: str) -> dict[str, int]:
    """이 실행에서 만든 테스트 사용자를 지우고, 남은 행 수를 테이블별로 돌려준다 (모두 0이어야 정상)."""
    pattern = f"ufcheck-{run}-%@example.com"
    with get_connection() as conn:
        user_ids: list[UUID] = [
            row[0] for row in conn.execute("SELECT id FROM users WHERE email LIKE %s", (pattern,)).fetchall()
        ]
        patient_ids: list[UUID] = [
            row[0] for row in conn.execute(
                "SELECT id FROM patient_profiles WHERE caregiver_id = ANY(%s)", (user_ids,)
            ).fetchall()
        ]
        conn.execute("DELETE FROM users WHERE id = ANY(%s)", (user_ids,))

    with get_connection() as conn:
        left = {
            "users": conn.execute("SELECT count(*) FROM users WHERE id = ANY(%s)", (user_ids,)).fetchone()[0],
            "patient_profiles": conn.execute(
                "SELECT count(*) FROM patient_profiles WHERE id = ANY(%s)", (patient_ids,)
            ).fetchone()[0],
            "caregiver_profiles": conn.execute(
                "SELECT count(*) FROM caregiver_profiles WHERE user_id = ANY(%s)", (user_ids,)
            ).fetchone()[0],
        }
        for table in PATIENT_CHILD_TABLES:
            left[table] = conn.execute(
                f"SELECT count(*) FROM {table} WHERE patient_id = ANY(%s)", (patient_ids,)
            ).fetchone()[0]
    print(f"  테스트 사용자 {len(user_ids)}명(환자 {len(patient_ids)}명 포함)을 삭제했습니다.")
    return left


def main() -> None:
    parser = argparse.ArgumentParser(description="user_functions 실 DB 점검")
    parser.add_argument("--keep", action="store_true", help="테스트 데이터를 지우지 않고 남긴다")
    args = parser.parse_args()

    try:
        check_schema()
    except psycopg.OperationalError as error:
        raise SystemExit(
            "DB에 연결할 수 없습니다. backend/에서 `docker compose up -d`로 DB를 띄웠는지, "
            f".env의 DATABASE_URL이 맞는지 확인하세요.\n  {error}"
        )

    run = uuid4().hex[:8]
    ctx: dict[str, Any] = {"run": run, "phone": f"010{random.randrange(10**8):08d}"}
    print(f"점검 실행 id: {run} (테스트 이메일: ufcheck-{run}-*@example.com)")

    c = Checker()
    try:
        for title, section in SECTIONS:
            print(f"\n[{title}]")
            try:
                section(c, ctx)
            except Exception as error:  # 한 단계가 깨져도 나머지 단계와 정리는 계속한다
                c.ok(f"{title}: 예상하지 못한 예외로 이 단계 중단", False, f"{type(error).__name__}: {error}")
    finally:
        print("\n[정리]")
        if args.keep:
            print(f"  --keep: 테스트 데이터를 남겼습니다. user_a={ctx.get('user_a')}, user_b={ctx.get('user_b')}")
            print(f"  나중에 지우려면: DELETE FROM users WHERE email LIKE 'ufcheck-{run}-%@example.com';")
        else:
            left = cleanup(run)
            c.ok("정리 후 테스트 데이터가 남지 않음 (CASCADE)", not any(left.values()),
                 {table: n for table, n in left.items() if n})

    print(f"\n결과: 통과 {c.passed}건 / 실패 {len(c.failed)}건")
    if c.failed:
        for name in c.failed:
            print(f"  - {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
