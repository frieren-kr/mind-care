"""챗봇 대화 하루 요약 + 7일 지난 원문 삭제. 하루 1번 cron으로 돌린다. (담당: 박주현)

팀 결정: 대화 원문은 7일만 보관하고, 그 뒤에는 하루 단위 요약만 남긴다.

  ① 그날(기본: 어제, KST) 대화가 있었던 사용자·환자마다 대화를 모아 요약 → chat_daily_summaries
     (같은 날을 다시 돌리면 덮어쓴다)
  ② 7일 지난 원문 삭제 — 요약이 실패하거나 요약 함수가 없어도 지운다 (보관 기간이 우선)

요약 함수는 웅 님 담당 app/ai의 create_daily_chat_summary를 쓸 예정이다. 아직 develop에 없어서
summarize_day()가 None을 돌려주고 요약 단계는 건너뛴다 (TODO). 그동안은 원문 삭제만 한다.

종료 코드: 0 성공(이미 실행 중이라 건너뛴 경우 포함) / 1 일부 실패 / 2 실행 불가(DB 연결 실패)

실행 (backend/ 에서):
    python -m scripts.summarize_chats --log-file logs/summarize_chats.log
    python -m scripts.summarize_chats --date 2026-11-05
    python -m scripts.summarize_chats --dry-run      # 대상만 세고 저장·삭제하지 않는다
"""
import argparse
import logging
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from app.db import chat_store
from app.db.chat_store import ChatMessage
from scripts.update_papers import acquire_lock, setup_logging

logger = logging.getLogger("summarize_chats")

DEFAULT_LOCK = Path(tempfile.gettempdir()) / "mindcare_summarize_chats.lock"


def summarize_day(messages: list[ChatMessage]) -> Optional[tuple[str, str]]:
    """하루 대화 → (요약, 모델 이름). None이면 요약을 저장하지 않는다.

    TODO(웅 님): app/ai의 create_daily_chat_summary가 develop에 들어오면 여기서 import해 부른다.
    """
    return None


def run(day: date, dry_run: bool) -> int:
    try:
        threads = chat_store.list_chat_threads_for_day(day)
    except Exception as exc:
        logger.error("실행할 수 없습니다: DB 연결 실패 (%s)", type(exc).__name__)
        return 2
    logger.info("시작 — %s 대화 %d건(사용자·환자별)%s", day, len(threads), " (dry-run)" if dry_run else "")

    saved = skipped = failed = 0
    for user_id, patient_id in threads:
        try:
            messages = chat_store.get_messages_for_day(user_id, patient_id, day)
            result = summarize_day(messages)
            if result is None:
                skipped += 1
                continue
            if not dry_run:
                chat_store.save_daily_summary(user_id, patient_id, day, *result)
            saved += 1
        except Exception as exc:  # 한 사람이 실패해도 나머지는 계속한다 (대화 내용은 로그에 남기지 않는다)
            logger.warning("요약 실패 user=%s patient=%s (%s)", user_id, patient_id, type(exc).__name__)
            failed += 1
    if skipped:
        logger.warning("요약 함수가 아직 없어 %d건 건너뜀 (create_daily_chat_summary 연결 전)", skipped)
    logger.info("[요약] 저장 %d건, 건너뜀 %d건, 실패 %d건%s", saved, skipped, failed,
                " (dry-run: 저장 안 함)" if dry_run else "")

    if dry_run:
        logger.info("[삭제] dry-run: %d일 지난 원문을 지우지 않음", chat_store.RETENTION_DAYS)
    else:
        try:
            deleted = chat_store.delete_old_chat_messages(chat_store.RETENTION_DAYS)
            logger.info("[삭제] %d일 지난 원문 %d행", chat_store.RETENTION_DAYS, deleted)
        except Exception as exc:
            logger.error("[삭제] 실패 (%s)", type(exc).__name__)
            failed += 1
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="챗봇 대화 하루 요약 + 7일 지난 원문 삭제")
    ap.add_argument("--date", type=date.fromisoformat, help="요약할 날짜 YYYY-MM-DD (기본: 어제, 한국 날짜)")
    ap.add_argument("--dry-run", action="store_true", help="DB에 쓰지도 지우지도 않는다")
    ap.add_argument("--log-file", type=Path, help="로그를 화면과 함께 남길 파일")
    ap.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK, help=f"겹쳐 실행 방지용 잠금 파일 (기본 {DEFAULT_LOCK})")
    args = ap.parse_args(argv)
    day = args.date or datetime.now(chat_store.KST).date() - timedelta(days=1)
    setup_logging(args.log_file)

    lock = acquire_lock(args.lock_file)
    if lock is None:
        logger.warning("이미 실행 중입니다 (잠금 파일 %s). 이번 실행은 건너뜁니다.", args.lock_file)
        return 0
    try:
        code = run(day, args.dry_run)
        logger.info("끝 — 종료 코드 %d", code)
        return code
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
