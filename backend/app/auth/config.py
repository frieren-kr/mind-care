"""인증 설정. 비밀키가 없으면 인증 요청은 503으로 실패한다."""
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env", extra="ignore"
    )
    auth_jwt_secret: SecretStr = SecretStr("")
    auth_jwt_issuer: str = Field(default="mind-care", min_length=1)
    auth_jwt_audience: str = Field(default="mind-care-app", min_length=1)
    auth_access_token_minutes: int = Field(default=15, ge=1, le=60)


auth_settings = AuthSettings()


def signing_key() -> str:
    key = auth_settings.auth_jwt_secret.get_secret_value()
    if len(key.encode("utf-8")) < 32 or not key.strip():
        raise AuthConfigurationError("인증 비밀키 설정이 필요합니다.")
    return key


class AuthConfigurationError(RuntimeError):
    pass
