"""챗봇 대화 저장·조회·하루 요약·오래된 원문 삭제 (박주현 담당, 012 마이그레이션).

★ 팀 규칙 ★
AI/백엔드 팀원은 DB에 직접 SQL을 쓰지 않는다. 대화 기록은 이 모듈의 함수로만 다룬다.

팀 결정: 대화 원문은 7일만 보관하고, 그 뒤에는 하루 단위 요약(chat_daily_summaries)만 남긴다.
"하루"는 한국 날짜다 (00:00~24:00 KST).

patient_id는 None = 환자를 고르지 않은 대화. 조회도 None끼리만 묶는다 (환자 대화와 섞지 않는다).
이 모듈은 `app/ai/`를 import 하지 않는다 (db → ai 방향 의존은 만들지 않는다).
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db.connection import get_connection

KST = timezone(timedelta(hours=9))  # 한국은 서머타임이 없어서 고정 +9로 충분하다
RETENTION_DAYS = 7


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str
    sources: list[dict[str, Any]]  # 답변의 출처 (ChatSource 모양). 질문은 빈 목록
    created_at: datetime
    patient_id: Optional[UUID]


# 같은 턴의 질문·답변 시각이 같게 찍혀도 질문(user)이 먼저 오게 role로 한 번 더 정렬한다.
_COLUMNS = "role, content, sources, created_at, patient_id"
_ORDER = "created_at, (role = 'assistant')"


def save_chat_turn(user_id: UUID, patient_id: Optional[UUID], question: str, answer: str,
                   sources: list[dict[str, Any]]) -> None:
    """질문과 답변(출처 포함) 두 행을 트랜잭션 하나로 저장한다. 하나라도 실패하면 둘 다 저장하지 않는다."""
    with get_connection() as conn:
        conn.execute(
            # clock_timestamp(): now()는 트랜잭션 시작 시각이라 같은 트랜잭션의 턴끼리 시각이 같아진다
            "INSERT INTO chat_messages (user_id, patient_id, role, content, sources, created_at)"
            " VALUES (%s, %s, 'user', %s, '[]', clock_timestamp()), (%s, %s, 'assistant', %s, %s, clock_timestamp())",
            (user_id, patient_id, question, user_id, patient_id, answer, Jsonb(sources)),
        )


def get_recent_chat_messages(user_id: UUID, patient_id: Optional[UUID], limit: int = 20) -> list[ChatMessage]:
    """최근 메시지 limit개를 오래된 것부터 돌려준다 ("이전 대화 기억"용). 7일 지난 원문은 이미 지워져 없다."""
    if limit < 1:
        raise ValueError("limit은 1 이상이어야 합니다")
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM chat_messages"
            " WHERE user_id = %s AND patient_id IS NOT DISTINCT FROM %s"
            f" ORDER BY created_at DESC, (role = 'user') LIMIT %s",
            (user_id, patient_id, limit),
        ).fetchall()
    return [ChatMessage(*row) for row in reversed(rows)]


def _day_range(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(), KST)
    return start, start + timedelta(days=1)


def list_chat_threads_for_day(day: date) -> list[tuple[UUID, Optional[UUID]]]:
    """그날(KST) 대화가 있었던 (user_id, patient_id) 목록. 요약 배치가 대상 고르기에 쓴다."""
    with get_connection() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT DISTINCT user_id, patient_id FROM chat_messages"
            " WHERE created_at >= %s AND created_at < %s ORDER BY user_id, patient_id",
            _day_range(day),
        ).fetchall()]


def get_messages_for_day(user_id: UUID, patient_id: Optional[UUID], day: date) -> list[ChatMessage]:
    """그날(KST) 이 사용자·환자의 대화 전부를 시간순으로 (요약 배치용)."""
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM chat_messages"
            " WHERE user_id = %s AND patient_id IS NOT DISTINCT FROM %s"
            f" AND created_at >= %s AND created_at < %s ORDER BY {_ORDER}",
            (user_id, patient_id, *_day_range(day)),
        ).fetchall()
    return [ChatMessage(*row) for row in rows]


def save_daily_summary(user_id: UUID, patient_id: Optional[UUID], day: date, summary: str, model: str) -> None:
    """하루 요약을 저장한다. 같은 사용자·환자·날짜가 이미 있으면 덮어쓴다 (배치를 다시 돌려도 한 행)."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO chat_daily_summaries (user_id, patient_id, summary_date, summary, model)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (user_id, patient_id, summary_date)"
            " DO UPDATE SET summary = EXCLUDED.summary, model = EXCLUDED.model, created_at = now()",
            (user_id, patient_id, day, summary, model),
        )


def delete_old_chat_messages(days: int = RETENTION_DAYS) -> int:
    """days일보다 오래된 대화 원문을 지우고 지운 행 수를 돌려준다. 요약(chat_daily_summaries)은 남는다."""
    if days < 1:
        raise ValueError("days는 1 이상이어야 합니다")
    with get_connection() as conn:
        return conn.execute(
            "DELETE FROM chat_messages WHERE created_at < now() - make_interval(days => %s)", (days,)
        ).rowcount
