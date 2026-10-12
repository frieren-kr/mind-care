"""개발용 돌봄 기록(care_logs) + 월간 요약(profile_summaries)을 샘플 간병인에게 적재한다.

`scripts/seed_dev_profiles.py`가 만든 샘플 간병인(dev-seed-*@example.com)에게
- 최근 30일 돌봄 기록 여러 건 (환자 연결 기록 + 환자 미연결 기록)
- 지난달 월간 요약 1건
을 넣는다. 개인화 피드(get_feed_context_snapshot)를 개발·검증할 때 쓴다.

- 실제 사람 정보가 아니다. 샘플 간병인에게만 넣는다.
- 날짜는 **실행 시각 기준 상대값**이다 (며칠 전). 고정 날짜를 쓰면 시간이 지나
  "최근 30일" 창에서 빠져나가 버린다.
- **여러 번 실행해도 중복으로 쌓이지 않는다.**
  - 돌봄 기록: 이 스크립트가 넣는 기록은 content가 '[dev-seed]'로 시작한다.
    적재 전에 해당 사용자의 '[dev-seed]' 기록을 지우고 다시 넣는다.
  - 월간 요약: save_profile_summary()가 (user_id, period_start, period_end) upsert라
    같은 기간이면 행이 늘지 않고 갱신된다.

★ 직접 SQL 예외 ★
feed_read.py에는 '기록 삭제'와 '이메일로 사용자 찾기' 함수가 없어서, 샘플 사용자 찾기와
재실행용 삭제에만 get_connection()으로 SQL을 직접 쓴다. 대상은 이메일이
'dev-seed-*@example.com'인 사용자 + content가 '[dev-seed]'로 시작하는 기록으로 한정한다.

실행 (backend/ 에서):
    python -m scripts.seed_dev_care_logs            # 적재 (재실행 안전)
    python -m scripts.seed_dev_care_logs --dry-run  # 넣을 내용만 보여주고 DB는 건드리지 않음
    python -m scripts.seed_dev_care_logs --reset    # [dev-seed] 기록·요약을 지우고 끝냄
"""
import argparse
import sys
from datetime import date, datetime, timedelta
from uuid import UUID

from app.db.connection import get_connection
from app.db.feed_read import save_care_log, save_profile_summary
from app.user_schemas import APP_TIMEZONE

# 이 스크립트가 넣은 기록을 알아보는 표시. 재실행 때 이 기록만 지운다.
MARK = "[dev-seed]"

# (며칠 전, log_type, 내용, 기분 태그, 환자에 연결할지)
CARE_LOGS: list[tuple[int, str, str, str | None, bool]] = [
    (1, "patient_care", "저녁에 같은 질문을 여러 번 반복하셨다. 차분히 대답해 드렸다.", "지침", True),
    (3, "patient_care", "함께 동네 산책 30분. 기분이 한결 좋아 보이셨다.", "뿌듯함", True),
    (4, "caregiver_selfcare", "오늘은 내가 너무 지쳤다. 일찍 누웠는데 잠이 잘 안 왔다.", "번아웃", False),
    (8, "patient_care", "약 드시는 걸 또 잊으셨다. 알람을 하나 더 맞춰 뒀다.", "걱정", True),
    (11, "caregiver_selfcare", "오랜만에 친구를 만나 두 시간 수다. 숨이 좀 쉬어졌다.", "안도", False),
    (15, "patient_care", "새벽에 집 안을 돌아다니셔서 같이 깼다. 수면이 계속 불규칙하다.", "불안", True),
    (19, "patient_care", "옛날 사진을 함께 보니 이야기를 많이 하셨다.", "뿌듯함", True),
    (23, "caregiver_selfcare", "병원 예약·서류 처리로 하루가 다 갔다. 내 시간이 없다.", "지침", False),
    (27, "patient_care", "식사량이 줄어 걱정이다. 좋아하시던 반찬도 조금만 드셨다.", "걱정", True),
]

SUMMARY_TAGS = ["수면장애", "반복질문", "간병인소진"]
SUMMARY_TEXT = (
    f"{MARK} 지난달에는 환자분의 수면 리듬이 흐트러진 날이 많았고(새벽 배회 2회), "
    "같은 질문을 반복하는 일이 늘었습니다. 복약을 잊는 날이 있어 알람을 추가했습니다. "
    "간병인은 서류·예약 업무 부담과 수면 부족으로 소진을 호소했고, "
    "친구를 만난 날에는 기분이 뚜렷하게 회복됐습니다."
)


def seed_users() -> list[tuple[UUID, str]]:
    """샘플 간병인 목록 (이메일 순)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email FROM users WHERE email LIKE 'dev-seed-%%@example.com'"
                " ORDER BY email"
            )
            return cur.fetchall()


def patients_of(user_id: UUID) -> list[UUID]:
    """그 간병인의 환자 id 목록 (등록 순)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM patient_profiles WHERE caregiver_id = %s ORDER BY created_at, id",
                (user_id,),
            )
            return [row[0] for row in cur.fetchall()]


def delete_seeded(user_ids: list[UUID]) -> tuple[int, int]:
    """이 스크립트가 넣은 기록·요약을 지운다. 표시(MARK)가 붙은 것만 지운다."""
    if not user_ids:
        return (0, 0)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM care_logs WHERE user_id = ANY(%s) AND content LIKE %s",
                (user_ids, MARK + "%"),
            )
            logs = cur.rowcount
            cur.execute(
                "DELETE FROM profile_summaries WHERE user_id = ANY(%s) AND summary LIKE %s",
                (user_ids, MARK + "%"),
            )
            summaries = cur.rowcount
    return (logs, summaries)


def last_month_period(today: date) -> tuple[date, date]:
    """지난달 1일 ~ 말일. (reference_date보다 period_end가 앞이어야 피드 조회에 잡힌다.)"""
    first_this_month = today.replace(day=1)
    period_end = first_this_month - timedelta(days=1)
    return (period_end.replace(day=1), period_end)


def main() -> None:
    parser = argparse.ArgumentParser(description="개발용 돌봄 기록·월간 요약 적재")
    parser.add_argument("--dry-run", action="store_true", help="DB를 건드리지 않고 넣을 내용만 보여준다")
    parser.add_argument("--reset", action="store_true", help="[dev-seed] 기록·요약을 지우고 끝낸다")
    args = parser.parse_args()

    now = datetime.now(APP_TIMEZONE)
    today = now.date()
    period_start, period_end = last_month_period(today)

    users = seed_users()
    if not users:
        print("샘플 간병인이 없습니다. 먼저 실행하세요: python -m scripts.seed_dev_profiles")
        sys.exit(1)

    print(f"샘플 간병인 {len(users)}명 / 기준 {today} / 월간 요약 기간 {period_start} ~ {period_end}")

    if args.reset:
        logs, summaries = delete_seeded([uid for uid, _ in users])
        print(f"--reset: 돌봄 기록 {logs}건, 월간 요약 {summaries}건 삭제했습니다.")
        return

    if args.dry_run:
        for uid, email in users:
            pids = patients_of(uid)
            linked = sum(1 for *_, to_patient in CARE_LOGS if to_patient and pids)
            print(f"  [dry-run] {email}: 돌봄 기록 {len(CARE_LOGS)}건"
                  f"(환자 연결 {linked} / 미연결 {len(CARE_LOGS) - linked})"
                  f" + 월간 요약 1건, 환자 {len(pids)}명")
        print("--dry-run: 저장하지 않았습니다.")
        return

    # 재실행 안전: 이 스크립트가 넣은 것만 지우고 다시 넣는다.
    removed_logs, removed_summaries = delete_seeded([uid for uid, _ in users])
    if removed_logs or removed_summaries:
        print(f"기존 [dev-seed] 기록 {removed_logs}건, 요약 {removed_summaries}건을 지우고 다시 넣습니다.")

    total_logs = 0
    for uid, email in users:
        pids = patients_of(uid)
        linked = unlinked = 0
        for days_ago, log_type, content, mood_tag, to_patient in CARE_LOGS:
            # 환자가 없는 간병인은 환자 연결 기록을 미연결로 넣는다.
            patient_id = pids[0] if (to_patient and pids) else None
            save_care_log(
                user_id=uid,
                log_type=log_type,
                content=f"{MARK} {content}",
                mood_tag=mood_tag,
                patient_id=patient_id,
                logged_at=now - timedelta(days=days_ago),
            )
            if patient_id is None:
                unlinked += 1
            else:
                linked += 1
            total_logs += 1

        save_profile_summary(
            user_id=uid,
            period_start=period_start,
            period_end=period_end,
            summary=SUMMARY_TEXT,
            updated_tags=SUMMARY_TAGS,
        )
        print(f"  [저장] {email}: 돌봄 기록 {linked + unlinked}건"
              f"(환자 연결 {linked} / 미연결 {unlinked}) + 월간 요약 1건")

    print(f"\n완료: 돌봄 기록 {total_logs}건, 월간 요약 {len(users)}건.")
    print("다시 실행해도 중복으로 쌓이지 않습니다 ([dev-seed] 기록을 지우고 다시 넣습니다).")


if __name__ == "__main__":
    main()
