"""서명 검증 → 유효 세션 → 경로 본인 확인 순서의 FastAPI 의존성."""
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from psycopg import Error as DatabaseError

from app.auth.config import AuthConfigurationError
from app.auth.security import TokenIdentity, decode_access_token
from app.auth_schemas import AuthUser
from app.db import functions

bearer = HTTPBearer(auto_error=False, description="POST /auth/login에서 받은 access_token")


def unauthorized() -> HTTPException:
    return HTTPException(401, "로그인이 필요하거나 로그인 정보가 만료되었습니다.",
                         headers={"WWW-Authenticate": "Bearer"})


def unavailable() -> HTTPException:
    return HTTPException(503, "인증 서비스를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.")


def get_token_identity(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> TokenIdentity:
    if credentials is None:
        raise unauthorized()
    try:
        return decode_access_token(credentials.credentials)
    except InvalidTokenError:
        raise unauthorized() from None
    except AuthConfigurationError:
        raise unavailable() from None


def get_current_user(identity: Annotated[TokenIdentity, Depends(get_token_identity)]) -> AuthUser:
    try:
        user = functions.get_auth_session_user(
            identity.user_id, identity.session_id, reference_time=datetime.now(timezone.utc),
        )
    except DatabaseError:
        raise unavailable() from None
    if user is None:
        raise unauthorized()
    return user


def require_user_access(user_id: UUID, user: Annotated[AuthUser, Depends(get_current_user)]) -> AuthUser:
    if user_id != user.id:
        raise HTTPException(403, "본인의 정보만 접근할 수 있습니다.")
    return user
