"""개발용 예시 간병인·환자 프로필을 DB에 적재한다.

`app/db/seed/dev_profiles.json`의 예시 프로필(간병인 + 자가점검 + 환자 + 평가·안전 기록·복약·진료)을
user_functions의 함수로 저장한다. 점검 스크립트(check_user_functions)와 달리 데이터를 **남겨 두고**,
개인화 피드·챗봇·API를 개발할 때 "실제처럼 채워진 사용자"로 쓴다.

- 실제 사람 정보가 아니다. 이메일은 모두 'dev-seed-*@example.com'이고 전화번호는 비워 둔다.
- 날짜는 JSON에 실행일 기준 상대값으로 적는다 (days_ago = 며칠 전, in_days = 며칠 뒤, 음수면 과거).
  고정 날짜를 쓰면 시간이 지나 "최근 30일 기록"이 비거나 예약일이 과거가 되기 때문이다.
- 여러 번 실행해도 안전하다. 같은 이메일의 사용자가 이미 있으면 건너뛴다.
- 프로필 하나를 저장하다 실패하면 그 사용자를 지워서, 반쯤 저장된 프로필이 남지 않게 한다.

★ 직접 SQL 예외 ★
user_functions에는 이메일 조회·삭제 함수가 없어서, 시드 사용자 찾기와 삭제(--reset, 실패 시 되돌리기)에만
get_connection()으로 SQL을 직접 쓴다. 대상은 이메일이 'dev-seed-*@example.com'인 사용자로 한정한다.

실행 (backend/ 에서):
    python -m scripts.seed_dev_profiles --dry-run   # DB 없이 JSON 형식만 검사
    python -m scripts.seed_dev_profiles             # 적재 (이미 있는 프로필은 건너뜀)
    python -m scripts.seed_dev_profiles --reset     # 시드 사용자를 지우고 다시 적재
"""
import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

import psycopg
from pydantic import BaseModel

from app.db import user_functions as uf
from app.db.connection import get_connection
from app.user_schemas import (
    AssessmentIn,
    CaregiverProfileIn,
    MedicalVisitIn,
    MedicationIn,
    PatientIn,
    SafetyEventIn,
    UserIn,
    today,
)

SEED_FILE = Path(__file__).resolve().parents[1] / "app" / "db" / "seed" / "dev_profiles.json"
SEED_EMAIL_PREFIX = "dev-seed-"
SEED_EMAIL_DOMAIN = "@example.com"
SEED_EMAIL_LIKE = f"{SEED_EMAIL_PREFIX}%{SEED_EMAIL_DOMAIN}"

# 상대 날짜 키 → (저장할 필드, 방향). 방향 -1 = 오늘에서 빼기(며칠 전), +1 = 더하기(며칠 뒤).
PATIENT_DATES = {"diagnosis_days_ago": ("diagnosis_date", -1)}

# 환자에 딸린 기록: JSON 키 → (저장 함수, 입력 모델, 상대 날짜 규칙)
PATIENT_RECORDS: dict[str, tuple[Callable[[dict[str, Any]], BaseModel], type[BaseModel], dict[str, tuple[str, int]]]] = {
    "assessments": (uf.create_assessment, AssessmentIn, {"days_ago": ("assessed_at", -1)}),
    "safety_events": (uf.upsert_safety_event, SafetyEventIn, {"days_ago": ("event_date", -1)}),
    "medications": (
        uf.create_medication,
        MedicationIn,
        {"start_days_ago": ("start_date", -1), "end_days_ago": ("end_date", -1)},
    ),
    "medical_visits": (uf.create_medical_visit, MedicalVisitIn, {"in_days": ("visit_date", +1)}),
}


def resolve_dates(record: dict[str, Any], rules: dict[str, tuple[str, int]]) -> dict[str, Any]:
    """상대 날짜 키(days_ago 등)를 실제 날짜 필드('YYYY-MM-DD')로 바꾼 사본을 돌려준다."""
    resolved = dict(record)
    for key, (field, direction) in rules.items():
        if key in resolved:
            days = resolved.pop(key)
            resolved[field] = None if days is None else (today() + timedelta(days=direction * days)).isoformat()
    return resolved


def load_profiles(path: Path) -> list[dict[str, Any]]:
    """JSON에서 프로필 목록을 읽는다. 시드가 아닌 이메일은 거부한다 (--reset이 실제 사용자를 지우지 않게)."""
    profiles = json.loads(path.read_text(encoding="utf-8"))["profiles"]
    for profile in profiles:
        email = profile["user"]["email"].strip().lower()
        if not (email.startswith(SEED_EMAIL_PREFIX) and email.endswith(SEED_EMAIL_DOMAIN)):
            raise SystemExit(f"시드 이메일은 '{SEED_EMAIL_PREFIX}*{SEED_EMAIL_DOMAIN}' 형식이어야 합니다: {email}")
    return profiles


def save_profile(profile: dict[str, Any], *, dry_run: bool) -> UUID:
    """프로필 하나를 저장하고 user_id를 돌려준다. dry_run이면 DB 없이 입력 모델 검증만 한다.

    실패하면 어느 기록에서 틀렸는지 담은 ValueError를 던진다.
    """
    email = profile["user"]["email"]

    def save(where: str, func: Callable[[dict[str, Any]], BaseModel], model: type[BaseModel], data: dict[str, Any]) -> UUID:
        try:
            if dry_run:
                model.model_validate(data)
                return uuid4()  # 자식 기록 검증에 쓸 임시 id
            return func(data).id
        except ValueError as error:
            raise ValueError(f"{email} / {where}: {error}") from error

    user_id = save("user", uf.create_user, UserIn, profile["user"])

    caregiver = profile.get("caregiver_profile")
    if caregiver:
        # upsert_caregiver_profile은 user_id를 따로 받고 .id가 없는 모델을 돌려줘서 save()를 쓰지 않는다.
        try:
            if dry_run:
                CaregiverProfileIn.model_validate(caregiver)
            else:
                uf.upsert_caregiver_profile(user_id, caregiver)
        except ValueError as error:
            raise ValueError(f"{email} / caregiver_profile: {error}") from error

    for patient in profile.get("patients", []):
        fields = {k: v for k, v in patient.items() if k not in PATIENT_RECORDS}
        label = f"환자 '{fields.get('name')}'"
        patient_id = save(label, uf.create_patient, PatientIn,
                          {**resolve_dates(fields, PATIENT_DATES), "caregiver_id": str(user_id)})
        for key, (func, model, rules) in PATIENT_RECORDS.items():
            for index, record in enumerate(patient.get(key, [])):
                save(f"{label} / {key}[{index}]", func, model,
                     {**resolve_dates(record, rules), "patient_id": str(patient_id)})
    return user_id


# ============================================================
# 시드 사용자 찾기·삭제 (직접 SQL — 이 스크립트에서만, dev-seed 이메일로 한정)
# ============================================================


def existing_seed_users() -> dict[str, UUID]:
    """이미 DB에 있는 시드 사용자 {이메일: user_id}."""
    with get_connection() as conn:
        rows = conn.execute("SELECT email, id FROM users WHERE email LIKE %s", (SEED_EMAIL_LIKE,)).fetchall()
    return {email: user_id for email, user_id in rows}


def delete_seed_users(user_ids: list[UUID] | None = None) -> int:
    """시드 사용자를 지운다 (환자·기록은 CASCADE로 함께 삭제). user_ids를 주면 그 사용자만."""
    with get_connection() as conn:
        if user_ids is None:
            cur = conn.execute("DELETE FROM users WHERE email LIKE %s", (SEED_EMAIL_LIKE,))
        else:
            cur = conn.execute(
                "DELETE FROM users WHERE id = ANY(%s) AND email LIKE %s", (user_ids, SEED_EMAIL_LIKE)
            )
        return cur.rowcount


def print_summary(user_ids: list[UUID]) -> None:
    """저장된 내용을 user_functions로 다시 읽어 요약한다 (API 테스트에 쓸 user_id 확인용)."""
    print("\n[적재된 시드 프로필] — user_functions로 다시 읽은 결과")
    for user_id in user_ids:
        user = uf.get_user(user_id)
        if user is None:
            print(f"  {user_id}: 조회되지 않음")
            continue
        caregiver = uf.get_caregiver_profile(user_id)
        burden = f"부담 {caregiver.burden_score} / 우울 {caregiver.mood_score}" if caregiver else "자가점검 없음"
        print(f"\n  {user.name} <{user.email}>  ({burden})")
        print(f"    user_id = {user.id}")
        for patient_id in user.patient_ids:
            patient = uf.get_patient(patient_id)
            counts = (
                f"평가 {len(uf.list_assessments(patient_id))} · "
                f"안전 기록 {len(uf.list_safety_events(patient_id))} · "
                f"복약 {len(uf.list_medications(patient_id))} · "
                f"진료 {len(uf.list_medical_visits(patient_id))}"
            )
            print(f"    - 환자 {patient.name} ({patient.dementia_stage})  patient_id = {patient_id}")
            print(f"      {counts}")


def main() -> None:
    parser = argparse.ArgumentParser(description="개발용 예시 간병인·환자 프로필 적재")
    parser.add_argument("--file", default=str(SEED_FILE), help="프로필 JSON 경로 (기본: app/db/seed/dev_profiles.json)")
    parser.add_argument("--dry-run", action="store_true", help="DB에 가지 않고 JSON 형식만 검사")
    parser.add_argument("--reset", action="store_true", help="기존 시드 사용자를 지우고 다시 적재")
    args = parser.parse_args()

    profiles = load_profiles(Path(args.file))
    print(f"프로필 파일: {args.file} ({len(profiles)}명)")

    if args.dry_run:
        failed = 0
        for profile in profiles:
            try:
                save_profile(profile, dry_run=True)
                print(f"  [OK]   {profile['user']['email']} ({profile.get('type', '')})")
            except ValueError as error:
                failed += 1
                print(f"  [FAIL] {error}")
        print(f"\n--dry-run: 저장하지 않았습니다. 형식 오류 {failed}건")
        sys.exit(1 if failed else 0)

    try:
        if args.reset:
            print(f"--reset: 기존 시드 사용자 {delete_seed_users()}명을 지웠습니다.")
        existing = existing_seed_users()
    except psycopg.OperationalError as error:
        raise SystemExit(
            "DB에 연결할 수 없습니다. backend/에서 `docker compose up -d`로 DB를 띄웠는지, "
            f".env의 DATABASE_URL이 맞는지 확인하세요.\n  {error}"
        )

    user_ids: list[UUID] = []
    failed = 0
    for profile in profiles:
        email = profile["user"]["email"].strip().lower()
        if email in existing:
            print(f"  [건너뜀] {email} — 이미 있습니다 (다시 만들려면 --reset)")
            user_ids.append(existing[email])
            continue
        try:
            user_ids.append(save_profile(profile, dry_run=False))
            print(f"  [저장]   {email} ({profile.get('type', '')})")
        except ValueError as error:
            failed += 1
            print(f"  [FAIL]   {error}")
            # 반쯤 저장된 프로필이 남지 않게 이 이메일의 사용자를 지운다.
            partial = existing_seed_users().get(email)
            if partial is not None:
                delete_seed_users([partial])
                print("           → 저장 중이던 이 사용자를 지웠습니다.")

    print_summary(user_ids)
    print(f"\n결과: 프로필 {len(profiles)}명 중 실패 {failed}명")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
