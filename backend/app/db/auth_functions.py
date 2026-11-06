"""인증 전용 DB 구현. 외부 호출자는 functions.py 관문으로 접근한다."""
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from psycopg.rows import dict_row

from app.auth_schemas import AuthUser
from app.db.connection import get_connection
from app.user_schemas import normalize_email


@dataclass(frozen=True)
class AuthCredential:
    user: AuthUser
    password_hash: str = field(repr=False)
    is_active: bool


def _aware(*values: datetime) -> None:
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        raise ValueError("인증 시각에는 시간대가 필요합니다.")


def get_auth_credentials(email: str) -> AuthCredential | None:
    email = normalize_email(email)
    with get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT u.id, u.name, u.email, u.feed_refresh_days, c.password_hash, c.is_active
              FROM users u JOIN user_credentials c ON c.user_id = u.id
             WHERE lower(btrim(u.email)) = %s
        """, (email,))
        row = cur.fetchone()
    if row is None:
        return None
    return AuthCredential(AuthUser.model_validate(row), row["password_hash"], row["is_active"])


def create_auth_session(
    user_id: UUID, session_id: UUID, *, password_hash: str,
    created_at: datetime, expires_at: datetime,
) -> bool:
    """검증한 해시·활성 상태를 잠금 아래 재확인하고 세션 생성. 변경됐으면 False."""
    _aware(created_at, expires_at)
    if expires_at <= created_at:
        raise ValueError("세션 만료는 생성 시각 이후여야 합니다.")
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT password_hash, is_active, password_changed_at
              FROM user_credentials WHERE user_id = %s FOR UPDATE
        """, (user_id,))
        row = cur.fetchone()
        if row is None or row[0] != password_hash or not row[1] or row[2] > created_at:
            return False
        cur.execute("""
            INSERT INTO auth_sessions (id, user_id, created_at, expires_at)
            VALUES (%s, %s, %s, %s)
        """, (session_id, user_id, created_at, expires_at))
    return True


def get_auth_session_user(user_id: UUID, session_id: UUID, *, reference_time: datetime) -> AuthUser | None:
    """세션 소유·만료·폐기·비밀번호 변경·계정 활성 상태를 함께 검사한다."""
    _aware(reference_time)
    with get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT u.id, u.name, u.email, u.feed_refresh_days
              FROM auth_sessions s JOIN users u ON u.id = s.user_id
              JOIN user_credentials c ON c.user_id = u.id
             WHERE s.id = %s AND s.user_id = %s AND s.revoked_at IS NULL
               AND s.created_at <= %s AND s.expires_at > %s AND c.is_active
               AND c.password_changed_at <= s.created_at
        """, (session_id, user_id, reference_time, reference_time))
        row = cur.fetchone()
    return AuthUser.model_validate(row) if row else None


def revoke_auth_session(user_id: UUID, session_id: UUID, *, reference_time: datetime) -> None:
    """해당 사용자의 세션만 폐기. 반복·없는 세션도 성공한다."""
    _aware(reference_time)
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""
            UPDATE auth_sessions SET revoked_at = GREATEST(created_at, %s)
             WHERE id = %s AND user_id = %s AND revoked_at IS NULL
        """, (reference_time, session_id, user_id))
