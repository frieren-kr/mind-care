"""인증 API의 공개 계약. 인증 내부 해시는 포함하지 않는다."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.user_schemas import normalize_email


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(max_length=320)
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def email_normalized(cls, value: str) -> str:
        return normalize_email(value)


class AuthUser(BaseModel):
    id: UUID
    name: str
    email: str
    feed_refresh_days: int


class LoginOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    expires_at: datetime
    user: AuthUser


class LogoutOut(BaseModel):
    ok: Literal[True] = True
