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
    # 임베딩 모델은 bge-m3 / 1024차원으로 확정 (2026-10-01, AI 담당 김현서와 합의).
    # embedding_dim은 DB 스키마(003_embedding_dim_1024.sql의 vector(1024))와 반드시 같아야 한다.
    # 차원을 바꿀 때는 새 마이그레이션과 이 값을 함께 고친다.
    embedding_model: str = "bge-m3"
    embedding_dim: int = 1024

    # Celery
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # App
    app_env: str = "development"


settings = Settings()
