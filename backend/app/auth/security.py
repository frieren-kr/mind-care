"""Argon2id 검증과 HS256 액세스 토큰. 알고리즘은 서버에서 고정한다."""
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
import jwt

from app.auth.config import auth_settings, signing_key

PASSWORD_HASHER = PasswordHasher()
# 없는 계정에도 같은 비용의 검증을 수행한다. 실제 계정으로 사용하지 않는 해시다.
_DUMMY_HASH = PASSWORD_HASHER.hash("mind-care-dummy-password-not-an-account")


@dataclass(frozen=True)
class TokenIdentity:
    user_id: UUID
    session_id: UUID


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        valid = PASSWORD_HASHER.verify(password_hash or _DUMMY_HASH, password)
        return bool(valid and password_hash)
    except (InvalidHashError, VerificationError, UnicodeError):
        return False


def create_access_token(user_id: UUID, session_id: UUID, *, issued_at: datetime, expires_at: datetime) -> str:
    return jwt.encode({
        "sub": str(user_id), "sid": str(session_id), "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()), "iss": auth_settings.auth_jwt_issuer,
        "aud": auth_settings.auth_jwt_audience,
    }, signing_key(), algorithm="HS256")


def decode_access_token(token: str) -> TokenIdentity:
    if len(token) > 4096:
        raise jwt.InvalidTokenError("Invalid token")
    payload = jwt.decode(
        token, signing_key(), algorithms=["HS256"],
        issuer=auth_settings.auth_jwt_issuer, audience=auth_settings.auth_jwt_audience,
        options={"require": ["sub", "sid", "iat", "exp", "iss", "aud"], "strict_aud": True},
    )
    try:
        if type(payload["iat"]) is not int or type(payload["exp"]) is not int or payload["exp"] <= payload["iat"]:
            raise ValueError("Invalid timestamps")
        return TokenIdentity(UUID(payload["sub"]), UUID(payload["sid"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise jwt.InvalidTokenError("Invalid claims") from exc
