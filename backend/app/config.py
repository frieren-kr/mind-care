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
    # 논문 요약 배치(scripts/summarize_papers.py)가 쓰는 OpenAI 모델. 일부러 기본값을 두지 않는다
    # (비어 있으면 배치가 안내 메시지를 내고 멈춘다).
    openai_summary_model: str = ""
    # 요약 배치 전용 키(선택). 있으면 요약 배치는 이 키를 쓰고, 비어 있으면 openai_api_key를 쓴다.
    openai_summary_api_key: str = ""
    # 임베딩 모델은 bge-m3 / 1024차원으로 확정 (2026-10-01, AI 담당 김현서와 합의).
    # embedding_dim은 DB 스키마(003_embedding_dim_1024.sql의 vector(1024))와 반드시 같아야 한다.
    # 차원을 바꿀 때는 새 마이그레이션과 이 값을 함께 고친다.
    embedding_model: str = "bge-m3"
    embedding_dim: int = 1024
    # 서버 시작 때 bge-m3를 미리 불러올지. 0이면 첫 검색 요청 때 불러온다 (테스트에서는 끈다).
    preload_embedding_model: bool = True

    # Celery
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # App
    app_env: str = "development"


settings = Settings()
