"""로그인 순서 조립. DB는 공용 함수 관문으로만 접근한다."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from psycopg import Error as DatabaseError

from app.auth.config import AuthConfigurationError, auth_settings, signing_key
from app.auth.dependencies import unavailable
from app.auth.security import create_access_token, verify_password
from app.auth_schemas import LoginIn, LoginOut
from app.db import functions


def login(data: LoginIn) -> LoginOut:
    try:
        signing_key()
        credential = functions.get_auth_credentials(data.email)
        valid = verify_password(data.password.get_secret_value(), credential.password_hash if credential else None)
        if not valid or credential is None or not credential.is_active:
            raise HTTPException(401, "이메일 또는 비밀번호를 확인해 주세요.")
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(minutes=auth_settings.auth_access_token_minutes)).replace(microsecond=0)
        sid = uuid4()
        token = create_access_token(credential.user.id, sid, issued_at=now, expires_at=expires_at)
        if not functions.create_auth_session(
            credential.user.id, sid, password_hash=credential.password_hash, created_at=now, expires_at=expires_at,
        ):
            raise HTTPException(401, "이메일 또는 비밀번호를 확인해 주세요.")
        return LoginOut(access_token=token, expires_in=int((expires_at - now).total_seconds()),
                        expires_at=expires_at, user=credential.user)
    except (DatabaseError, AuthConfigurationError):
        raise unavailable() from None
