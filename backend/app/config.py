"""환경 변수 설정. .env 파일에서 읽어온다 (.env.example 참고)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql://mindcare:password@localhost:5432/mindcare"

    # PubMed
    pubmed_api_key: str = ""
    pubmed_email: str = ""

    # LLM / Embedding
    openai_api_key: str = ""
    gemini_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    # Celery
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # App
    app_env: str = "development"


settings = Settings()
