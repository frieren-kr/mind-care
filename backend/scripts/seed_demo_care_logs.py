"""시연용 돌봄 기록(care_logs)을 dev-seed-1~4 계정의 환자에게 넣는다.

챗봇 돌봄 기록 Tool·개인화 피드 시연용. 실행한 날 기준 최근 14일 안에 환자마다 8~12건,
각 환자 프로필(app/db/seed/dev_profiles.json의 치매 단계·증상·복약)에 맞춘 patient_care 기록이다.
실제 사람 정보가 아니다. 이름·연락처는 넣지 않는다.
챗봇 Tool은 user_functions.get_recent_care_logs()(patient_care, 기본 14일·10건, 전체 건수 함께)로 읽는다.

- APP_ENV가 development 또는 test일 때만 돈다.
- 이 스크립트가 넣은 기록 표시: logged_at을 '정각 + MARK_MICROSECONDS 마이크로초'로 저장한다.
  내용(content)에 표시 문자열을 붙이면 챗봇·피드 화면에 그대로 보이므로, 화면에 드러나지 않는 시각 끝자리를 쓴다.
  재실행 때는 dev-seed-1~4 사용자의 patient_care 기록 중 이 표시가 있는 것만 지우고 다시 넣는다.
  (기존 scripts/seed_dev_care_logs.py의 '[dev-seed]' 기록과는 서로 건드리지 않는다.)
- 저장은 save_care_log()(feed_read.py), 환자 조회는 한슬 님 list_patients()(user_functions.py)를 쓴다.

★ 직접 SQL 예외 ★ (seed_dev_care_logs.py와 같은 이유)
이메일로 사용자 찾기·돌봄 기록 삭제 함수가 없어 이 두 곳만 get_connection()으로 SQL을 쓴다.
대상은 아래 4개 이메일 사용자 + 표시가 있는 patient_care 기록으로 한정한다.

실행 (backend/ 에서):
    python -m scripts.seed_demo_care_logs --dry-run   # 넣을 건수만 보여주고 DB는 바꾸지 않음
    python -m scripts.seed_demo_care_logs
"""
import argparse
from datetime import datetime, time, timedelta
from uuid import UUID

from app.config import settings
from app.db import user_functions as uf
from app.db.connection import get_connection
from app.db.feed_read import save_care_log
from app.user_schemas import APP_TIMEZONE

# ponytail: 시각 끝자리로 표시. 다른 경로가 같은 마이크로초로 저장할 확률(백만분의 1)만큼 오삭제 여지가 있다.
#           앱에 돌봄 기록 쓰기 화면이 생기면 출처 칸을 마이그레이션으로 추가해 바꾼다.
MARK_MICROSECONDS = 424242
LOG_TYPE = "patient_care"

# {이메일: {치매 단계: [(며칠 전 1~13, 시, 기분 태그, 내용)]}}
# 환자는 이름이 아니라 치매 단계로 찾는다 (서버마다 호칭을 바꾼 경우가 있다. 계정 안에서 단계는 겹치지 않는다).
SEED_LOGS: dict[str, dict[str, list[tuple[int, int, str, str]]]] = {
    # 경도인지장애 + 수면 문제 / 콜린알포세레이트 1일 2회 식후 — 낮잠이 길었던 다음 밤에 깸
    "dev-seed-1@example.com": {"경도인지장애": [
        (13, 8, "평온", "어젯밤 한 번도 안 깨고 주무셨다. 낮잠은 점심 뒤 30분 정도였다."),
        (12, 16, "걱정", "점심 드시고 소파에서 2시간 넘게 주무셨다. 깨워도 다시 눕고 싶어 하셨다."),
        (11, 7, "지침", "새벽 2시쯤 깨서 거실을 서성이셨다. 어제 낮잠이 길었던 날이다."),
        (10, 20, "안도", "저녁 식후 약을 잘 드셨다. 오늘은 낮잠을 30분에서 깨워 드렸다."),
        (9, 8, "평온", "밤새 푹 주무셨다. 낮잠이 짧았던 다음 날은 확실히 잘 주무신다."),
        (7, 15, "걱정", "비가 와서 산책을 못 했고 오후에 1시간 반 주무셨다."),
        (6, 7, "지침", "새벽 3시에 깨서 아침인 줄 아시고 옷을 갈아입으셨다. 다시 주무시기까지 40분 걸렸다."),
        (5, 21, "걱정", "아침 약을 드셨는지 기억 못 하셔서 약통을 같이 확인했다. 저녁 약은 식후에 드렸다."),
        (3, 17, "뿌듯함", "오후에 30분 같이 걸었다. 낮잠 없이 저녁까지 지내셨다."),
        (2, 8, "평온", "어젯밤 11시에 주무시고 아침 7시까지 안 깨셨다."),
        (1, 13, "걱정", "점심 직후 졸려하셔서 20분 뒤 깨워 드렸다. 낮잠이 30분을 넘지 않게 하고 있다."),
    ]},
    # 중등도 + 배회·낙상 / 도네페질 취침 전, 메만틴 1일 2회 — 해 질 녘마다 불안·출구 찾기
    "dev-seed-2@example.com": {"중등도": [
        (13, 18, "불안", "오후 5시 넘어서부터 '집에 가야 한다'며 현관을 여러 번 오가셨다."),
        (12, 9, "지침", "밤사이 두 번 깨셨다. 도네페질은 취침 전에 드렸다."),
        (11, 18, "불안", "해 질 무렵 다시 초조해하시며 신발을 찾으셨다. 거실 불을 일찍 켜 두니 조금 나아졌다."),
        (10, 14, "평온", "주간보호센터 오전 활동 뒤 오후에는 차분하셨다."),
        (9, 19, "걱정", "저녁 6시쯤 현관문을 열고 나가려 하셨다. 문 열림 알림이 울려 바로 따라갔다."),
        (8, 7, "불안", "새벽에 화장실 가시다 문턱에 걸려 휘청하셨다. 넘어지진 않았다. 복도 야간등을 켜 두기로 했다."),
        (7, 18, "불안", "저녁 식사 전 같은 질문을 계속하며 안절부절못하셨다. 옛날 노래를 틀어 드리니 가라앉았다."),
        (6, 20, "평온", "저녁 메만틴을 잘 드셨다. 오늘은 저녁 불안이 20분 정도로 짧았다."),
        (5, 18, "불안", "오후 5시 반부터 '회사 가야 한다'며 외투를 입으셨다."),
        (4, 10, "걱정", "아침에 화장실 위치를 헷갈려 하셨다. 문에 그림 표시를 붙였다."),
        (2, 18, "불안", "해 질 녘에 또 초조해지셨다. 커튼을 미리 치고 불을 밝게 하니 30분 만에 진정하셨다."),
        (1, 22, "지침", "취침 전 도네페질을 드리고 10시에 누우셨는데 자정에 깨서 현관 쪽으로 가셨다."),
    ]},
    # 경도 + 우울·무기력·반복 질문 / 에스시탈로프람 아침 — 식사량 감소 추세
    "dev-seed-3@example.com": {"경도": [
        (13, 13, "평온", "점심을 한 공기 다 드셨다. 오후에 TV를 보시며 이야기도 조금 하셨다."),
        (12, 9, "평온", "아침 약을 드셨다. 오늘 병원 가는 시간을 다섯 번쯤 다시 물으셨다."),
        (11, 19, "걱정", "저녁은 3분의 2 정도 드셨다. 입맛이 없다고 하셨다."),
        (9, 15, "지침", "하루 종일 누워 계시려 했다. 산책 가자고 해도 '귀찮다'고만 하셨다."),
        (8, 13, "걱정", "점심을 반 공기만 드셨다. 좋아하시던 생선조림에도 손이 잘 안 가셨다."),
        (6, 10, "지침", "아침 약은 드셨는데 오늘 날짜를 계속 물으셨다. 달력에 표시해 두었다."),
        (5, 19, "걱정", "저녁도 반 정도만 드셨다. 물도 평소보다 적게 드신다."),
        (3, 13, "걱정", "점심을 몇 숟가락 드시고 수저를 놓으셨다. 사흘째 식사량이 절반 아래다."),
        (1, 18, "불안", "체중을 재 보니 2주 전보다 1.5kg 줄었다. 다음 진료 때 말씀드리려고 적어 둔다."),
    ]},
    # 환자 2명 / 경도 + 약 복용 잊음, 리바스티그민 패치 1일 1회 교체
    #             중증 + 삼킴 곤란·공격성·낙상 위험, 메만틴 — 식사량 감소, 목욕·옷 갈아입기 때 거부
    "dev-seed-4@example.com": {
        "경도": [
            (13, 9, "평온", "아침 9시에 패치를 새로 붙였다. 붙이는 자리를 어깨 쪽으로 바꿨다."),
            (12, 20, "걱정", "패치를 직접 떼고 새로 붙였다고 하셨는데 언제 붙였는지는 기억 못 하셨다."),
            (10, 9, "걱정", "아침에 패치 교체를 잊고 나갈 뻔했다. 냉장고에 교체 체크표를 붙였다."),
            (9, 21, "안도", "체크표 덕분에 오늘은 제때 바꿨다. 등 쪽 피부는 괜찮다."),
            (7, 10, "걱정", "전날 붙인 자리에 붉은 기가 조금 있다. 오늘은 반대쪽 팔 위에 붙였다."),
            (5, 14, "지침", "아이 등원 준비로 바빠 패치 교체가 오후 2시로 밀렸다."),
            (3, 15, "평온", "냄비 불 끄는 걸 잊으셨지만 타이머가 울려 괜찮았다."),
            (1, 9, "안도", "최근 나흘은 체크표대로 제때 교체했다. 피부 붉은 기도 가라앉았다."),
        ],
        "중증": [
            (13, 12, "평온", "점심 죽을 거의 다 드셨다. 사레는 없었다."),
            (12, 19, "불안", "목욕을 도와드리는데 손을 뿌리치고 소리를 지르셨다. 중간에 쉬었다가 다시 했다."),
            (11, 8, "걱정", "아침 약을 물에 넘기다 사레가 들렸다. 점도 증진제를 탄 물로 바꿔 드렸다."),
            (10, 13, "걱정", "점심 죽을 3분의 2쯤 드셨다. 식사에 40분 넘게 걸렸다."),
            (8, 19, "불안", "옷 갈아입을 때 팔을 휘두르셨다. 미리 설명하고 천천히 하니 조금 나았다."),
            (7, 12, "걱정", "점심 중 두 번 사레가 들려 식사를 일찍 마쳤다. 절반 정도 드셨다."),
            (5, 16, "지침", "침대에서 휠체어로 옮겨 드리다 다리에 힘이 빠져 같이 주저앉을 뻔했다."),
            (4, 13, "걱정", "점심을 절반도 못 드셨다. 삼키는 데 시간이 오래 걸린다."),
            (2, 19, "불안", "저녁 목욕 때 거부가 또 심하셨다. 오늘은 수건으로 닦아 드리는 것으로 대신했다."),
            (1, 12, "걱정", "점심 몇 숟가락 뒤 고개를 돌리셨다. 일주일 사이 식사량이 눈에 띄게 줄었다."),
        ],
    },
}


def logged_at(now: datetime, days_ago: int, hour: int) -> datetime:
    """실행일 기준 며칠 전 그 시각(정각) + 표시 마이크로초. 한국 시간."""
    day = now.astimezone(APP_TIMEZONE).date() - timedelta(days=days_ago)
    return datetime.combine(day, time(hour, 0, 0, MARK_MICROSECONDS), tzinfo=APP_TIMEZONE)


def seed_user_ids(conn) -> dict[str, UUID]:
    """대상 이메일 → user_id (DB에 있는 것만)."""
    rows = conn.execute("SELECT email, id FROM users WHERE email = ANY(%s)", (list(SEED_LOGS),)).fetchall()
    return {email: user_id for email, user_id in rows}


def delete_seeded(conn, user_ids: list[UUID]) -> int:
    """이 스크립트가 넣은 기록(표시 있는 patient_care)만 지운다."""
    if not user_ids:
        return 0
    return conn.execute(
        "DELETE FROM care_logs WHERE user_id = ANY(%s) AND log_type = %s"
        " AND EXTRACT(MICROSECONDS FROM logged_at) = %s",
        (user_ids, LOG_TYPE, MARK_MICROSECONDS),
    ).rowcount


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="시연용 돌봄 기록 적재 (dev-seed-1~4 환자)")
    parser.add_argument("--dry-run", action="store_true", help="넣을 건수만 보여주고 DB는 바꾸지 않는다")
    args = parser.parse_args(argv)
    if settings.app_env not in {"development", "test"}:
        parser.error("이 스크립트는 APP_ENV=development 또는 test에서만 실행할 수 있습니다.")

    now = datetime.now(APP_TIMEZONE)
    with get_connection() as conn:
        users = seed_user_ids(conn)
    if not users:
        raise SystemExit("dev-seed 계정이 없습니다. 먼저 실행하세요: python -m scripts.seed_dev_profiles")

    # (이메일, user_id, patient_id, 기록 목록). 계정·환자가 없으면 건너뛴다.
    targets = []
    for email, by_patient in SEED_LOGS.items():
        if email not in users:
            print(f"  [건너뜀] {email}: 계정 없음")
            continue
        patients = {p.dementia_stage: p.id for p in uf.list_patients(users[email])}
        for stage, logs in by_patient.items():
            if stage not in patients:
                print(f"  [건너뜀] {email}: '{stage}' 환자 없음")
                continue
            targets.append((email, users[email], patients[stage], stage, logs))

    if args.dry_run:
        for email, _, _, stage, logs in targets:
            print(f"  [dry-run] {email} / {stage}: {len(logs)}건")
        print(f"--dry-run: 총 {sum(len(t[4]) for t in targets)}건 예정, 저장하지 않았습니다.")
        return

    with get_connection() as conn:
        removed = delete_seeded(conn, list(users.values()))
    print(f"이전에 이 스크립트가 넣은 기록 {removed}건을 지웠습니다.")

    total = 0
    for email, user_id, patient_id, stage, logs in targets:
        for days_ago, hour, mood_tag, content in logs:
            save_care_log(user_id=user_id, log_type=LOG_TYPE, content=content, mood_tag=mood_tag,
                          patient_id=patient_id, logged_at=logged_at(now, days_ago, hour))
        total += len(logs)
        print(f"  [저장] {email} / {stage}: {len(logs)}건")
    print(f"완료: 돌봄 기록 {total}건 저장.")


if __name__ == "__main__":
    main()
