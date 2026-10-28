"""마인드 케어(Mind Care) 백엔드 진입점.

FastAPI 앱을 생성하고 라우터를 등록한다.
실행: uvicorn main:app --reload
"""
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import chat, papers, recommendations, users
from app.config import settings

logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버 시작 때 검색 모델(bge-m3)을 미리 불러온다. 첫 /recommendations 요청이 느려지지 않게.

    PRELOAD_EMBEDDING_MODEL=0이면 건너뛴다. 실패해도 서버는 뜨고, 첫 검색 때 다시 불러온다.
    """
    if settings.preload_embedding_model:
        start = time.perf_counter()
        try:
            from app.ai.retrieval.embedding import get_model

            get_model()
            logger.info("임베딩 모델 예열 완료: %.1f초", time.perf_counter() - start)
        except Exception:
            logger.warning("임베딩 모델 예열 실패 (%.1f초) — 첫 검색 때 다시 불러온다",
                           time.perf_counter() - start, exc_info=True)
    yield

app = FastAPI(
    title="Mind Care API",
    description="치매·경도인지장애 가족 간병인을 위한 연구 요약·개인화 앱 백엔드",
    version="0.1.0",
    lifespan=lifespan,
)


def _add_dev_cors(app: FastAPI) -> None:
    """개발 환경에서만 CORS 허용 (웹에서 화면 테스트용, Expo web 기본 포트 8081)."""
    if settings.app_env != "development":
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:8081", "http://127.0.0.1:8081"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


_add_dev_cors(app)


@app.get("/health")
def health() -> dict:
    """헬스 체크. 배포/개발 환경 확인용."""
    return {"status": "ok", "env": settings.app_env}


# 팀원 라우터는 아래에 등록한다. app/api/ 에 파일을 만들고 한 줄씩 추가.
# (예: from app.api import users → app.include_router(users.router))
app.include_router(papers.router)
app.include_router(users.router)
app.include_router(recommendations.router)
app.include_router(chat.router)
