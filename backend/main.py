"""마인드 케어(Mind Care) 백엔드 진입점.

FastAPI 앱을 생성하고 라우터를 등록한다.
실행: uvicorn main:app --reload
"""
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.ai.agent.workflows.chat_history_context import chat_history_token_counter, utf8_token_upper_bound
from app.api import auth, chat, papers, recommendations, users
from app.config import settings

logger = logging.getLogger("uvicorn.error")


def warm_chat_tokenizer() -> None:
    """챗봇 이전 대화 토큰 계산용 tiktoken 인코딩을 미리 읽는다 (tiktoken이 프로세스 안에 캐시한다).

    챗봇 모델이 없으면 건너뛴다. 실패해도 예외를 내지 않고 경고만 남긴다 (요청 때 UTF-8 상한으로 센다).
    """
    model = chat.chat_model()
    if not model:
        return
    start = time.perf_counter()
    try:
        counter = chat_history_token_counter(model)
    except Exception:
        counter = utf8_token_upper_bound
    if counter is utf8_token_upper_bound:
        logger.warning("챗봇 토크나이저 예열 실패 (%.1f초) — 이전 대화는 UTF-8 상한으로 센다",
                       time.perf_counter() - start)
    else:
        logger.info("챗봇 토크나이저 예열 완료: %.1f초", time.perf_counter() - start)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버 시작 때 검색 모델(bge-m3)과 챗봇 토크나이저를 미리 불러온다. 첫 요청이 느려지지 않게.

    임베딩은 PRELOAD_EMBEDDING_MODEL=0이면 건너뛴다. 실패해도 서버는 뜨고, 첫 검색 때 다시 불러온다.
    """
    warm_chat_tokenizer()
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
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(recommendations.router)
app.include_router(chat.router)
