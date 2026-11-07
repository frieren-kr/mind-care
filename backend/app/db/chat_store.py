"""챗봇 대화 저장·조회·하루 요약·오래된 원문 삭제 (박주현 담당, 012 마이그레이션).

★ 팀 규칙 ★
AI/백엔드 팀원은 DB에 직접 SQL을 쓰지 않는다. 대화 기록은 이 모듈의 함수로만 다룬다.

팀 결정: 대화 원문은 7일만 보관하고, 그 뒤에는 하루 단위 요약(chat_daily_summaries)만 남긴다.
"하루"는 한국 날짜다 (00:00~24:00 KST).

patient_id는 None = 환자를 고르지 않은 대화. 조회도 None끼리만 묶는다 (환자 대화와 섞지 않는다).
환자를 지우면 그 환자의 대화 원문·요약도 지워진다 (013, CASCADE) — 그래서 NULL은 "원래 환자 미선택"뿐이다.
질문·답변 두 행은 turn_id로 묶는다 (013). turn_id가 NULL인 행은 012 시절 옛 행이라 이전 대화 조회에서 뺀다.
이 모듈은 `app/ai/`를 import 하지 않는다 (db → ai 방향 의존은 만들지 않는다).
"""
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

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


@dataclass(frozen=True)
class ChatTurn:
    """완료된 질문·답변 한 쌍. started_at = 질문 행 시각, completed_at = 답변 행 시각."""
    question: str
    answer: str
    started_at: datetime
    completed_at: datetime


@dataclass(frozen=True)
class ChatTurnRecord:
    """화면용 질문·답변 한 쌍 (GET /chat/{user_id}/history). sources는 답변 행의 출처 목록."""
    turn_id: UUID
    question: str
    answer: str
    sources: list[dict[str, Any]]
    asked_at: datetime
    answered_at: datetime


@dataclass(frozen=True)
class ChatDailySummary:
    summary_date: date  # 한국 날짜
    summary: str


# 같은 턴의 질문·답변 시각이 같게 찍혀도 질문(user)이 먼저 오게 role로 한 번 더 정렬한다.
_COLUMNS = "role, content, sources, created_at, patient_id"
_ORDER = "created_at, (role = 'assistant')"


def save_chat_turn(user_id: UUID, patient_id: Optional[UUID], question: str, answer: str,
                   sources: list[dict[str, Any]]) -> None:
    """질문과 답변(출처 포함) 두 행을 트랜잭션 하나로 저장한다. 하나라도 실패하면 둘 다 저장하지 않는다.

    두 행에 같은 turn_id(uuid4)를 넣어 한 쌍으로 묶는다 (013).
    """
    turn_id = uuid4()
    with get_connection() as conn:
        conn.execute(
            # clock_timestamp(): now()는 트랜잭션 시작 시각이라 같은 트랜잭션의 턴끼리 시각이 같아진다.
            # VALUES는 앞 행부터 계산하므로 질문 시각 <= 답변 시각이다.
            "INSERT INTO chat_messages (turn_id, user_id, patient_id, role, content, sources, created_at)"
            " VALUES (%s, %s, %s, 'user', %s, '[]', clock_timestamp()),"
            " (%s, %s, %s, 'assistant', %s, %s, clock_timestamp())",
            (turn_id, user_id, patient_id, question, turn_id, user_id, patient_id, answer, Jsonb(sources)),
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


def _use(conn):
    """conn을 받으면 그 커넥션(같은 트랜잭션)을 그대로 쓰고, 없으면 새로 연다."""
    return nullcontext(conn) if conn is not None else get_connection()


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name}에는 시간대가 있어야 합니다")


def get_recent_chat_turns(user_id: UUID, patient_id: Optional[UUID], reference_time: datetime,
                          limit: int = 5, *, conn=None) -> list[ChatTurn]:
    """reference_time 전에 완료된 질문·답변 쌍을 최신순으로 최대 limit개 ("이전 대화 기억"용).

    turn_id로 질문·답변 행을 묶는다. turn_id가 NULL인 옛 행(012)과 짝이 없는 행은 빠진다.
    patient_id 범위는 정확히 같은 것만 (None이면 환자 미선택 대화만). 같은 완료 시각은 turn_id 순.
    conn을 넘기면 그 트랜잭션 안에서 조회한다 (read_chat_history 참고).
    """
    _aware(reference_time, "reference_time")
    rows = _select_turns(user_id, patient_id, reference_time, limit, conn)
    return [ChatTurn(question, answer, asked, answered) for _, question, answer, _, asked, answered in rows]


def list_chat_turns(user_id: UUID, patient_id: Optional[UUID], *, before: Optional[datetime] = None,
                    limit: int = 20) -> tuple[list[ChatTurnRecord], bool]:
    """화면에 그릴 대화 목록 (GET /chat/{user_id}/history). (턴 목록, has_more)를 돌려준다.

    before(답변 시각 기준, 이 시각 미만)보다 이전에 완료된 쌍 중 최신 limit개를 골라
    오래된 것부터 정렬해 준다. 더 불러오기는 받은 목록 첫 턴의 answered_at을 다음 before로 넘긴다.
    has_more = 그보다 이전 쌍이 더 있는지 (limit+1개를 읽어 판단). 범위·제외 규칙은 get_recent_chat_turns와 같다.
    7일 지난 원문은 이미 지워져 없다.
    """
    if before is not None:
        _aware(before, "before")
    rows = _select_turns(user_id, patient_id, before, limit + 1, None)
    return [ChatTurnRecord(*row) for row in reversed(rows[:limit])], len(rows) > limit


def _select_turns(user_id, patient_id, before, limit, conn, since=None) -> list[tuple]:
    """완료된 질문·답변 쌍을 답변 시각 최신순으로. (turn_id, 질문, 답변, 출처, 질문 시각, 답변 시각)

    turn_id로 질문·답변 행을 self-join 한다. turn_id가 NULL인 옛 행(012)과 짝이 없는 행은 빠진다.
    답변 시각이 since 이상 before 미만인 것만 (None이면 그쪽 제한 없음). limit None = 전부.
    """
    if limit is not None and limit < 1:
        raise ValueError("limit은 1 이상이어야 합니다")
    with _use(conn) as c:
        return c.execute(
            "SELECT a.turn_id, q.content, a.content, a.sources, q.created_at, a.created_at"
            "  FROM chat_messages a"
            "  JOIN chat_messages q ON q.turn_id = a.turn_id AND q.role = 'user'"
            "   AND q.user_id = a.user_id AND q.patient_id IS NOT DISTINCT FROM a.patient_id"
            " WHERE a.role = 'assistant' AND a.user_id = %(user_id)s"
            "   AND a.patient_id IS NOT DISTINCT FROM %(patient_id)s"
            "   AND (%(before)s::timestamptz IS NULL OR a.created_at < %(before)s::timestamptz)"
            "   AND (%(since)s::timestamptz IS NULL OR a.created_at >= %(since)s::timestamptz)"
            " ORDER BY a.created_at DESC, a.turn_id LIMIT %(limit)s",
            {"user_id": user_id, "patient_id": patient_id, "before": before, "since": since, "limit": limit},
        ).fetchall()


def get_daily_summaries_before(user_id: UUID, patient_id: Optional[UUID], before_day: date,
                               limit: int = 7, *, conn=None) -> list[ChatDailySummary]:
    """before_day(한국 날짜)보다 이전의 하루 요약을 최신 날짜부터 최대 limit개.

    오늘(KST) 이전만 쓰려면 before_day = 오늘. patient_id 범위는 get_recent_chat_turns와 같다.
    """
    if limit < 1:
        raise ValueError("limit은 1 이상이어야 합니다")
    with _use(conn) as c:
        rows = c.execute(
            "SELECT summary_date, summary FROM chat_daily_summaries"
            " WHERE user_id = %s AND patient_id IS NOT DISTINCT FROM %s AND summary_date < %s"
            " ORDER BY summary_date DESC LIMIT %s",
            (user_id, patient_id, before_day, limit),
        ).fetchall()
    return [ChatDailySummary(*row) for row in rows]


def read_chat_history(user_id: UUID, patient_id: Optional[UUID], reference_time: datetime,
                      max_turns: int = 5) -> tuple[list[ChatTurn], list[ChatDailySummary]]:
    """챗봇 이전 대화 조회 한 번 = 위 두 조회를 커넥션·트랜잭션 하나로.

    요약은 reference_time의 한국 날짜(오늘)보다 이전 것만. 오류는 그대로 올린다 (빈 목록으로 숨기지 않는다).
    ponytail: 기본 READ COMMITTED라 두 SELECT 사이에 요약 배치가 쓰면 서로 다른 시점을 볼 수 있다.
    겹침 판정은 Agent가 날짜 단위로 보수적으로 하므로 지금은 충분하다. 필요해지면 REPEATABLE READ로.
    """
    _aware(reference_time, "reference_time")
    with get_connection() as conn:
        turns = get_recent_chat_turns(user_id, patient_id, reference_time, max_turns, conn=conn)
        summaries = get_daily_summaries_before(user_id, patient_id, reference_time.astimezone(KST).date(),
                                               conn=conn)
    return turns, summaries


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


def get_turns_for_day(user_id: UUID, patient_id: Optional[UUID], day: date) -> list[ChatTurn]:
    """그날(KST, 답변 시각 기준) 완료된 질문·답변 쌍 전부를 시간순으로 (하루 요약 배치용).

    범위·제외 규칙은 get_recent_chat_turns와 같다 (turn_id 없는 옛 행, 답 없는 질문은 빠진다).
    """
    start, end = _day_range(day)
    rows = _select_turns(user_id, patient_id, end, None, None, since=start)
    return [ChatTurn(question, answer, asked, answered) for _, question, answer, _, asked, answered in reversed(rows)]


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
