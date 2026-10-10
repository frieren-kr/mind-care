"""마인드 케어(Mind Care) 백엔드 진입점.

FastAPI 앱을 생성하고 라우터를 등록한다.
실행: uvicorn main:app --reload
"""
from fastapi import FastAPI

from app.api import papers, users
from app.config import settings

app = FastAPI(
    title="Mind Care API",
    description="치매·경도인지장애 가족 간병인을 위한 연구 요약·개인화 앱 백엔드",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict:
    """헬스 체크. 배포/개발 환경 확인용."""
    return {"status": "ok", "env": settings.app_env}


# 팀원 라우터는 아래에 등록한다. app/api/ 에 파일을 만들고 한 줄씩 추가.
# (예: from app.api import users → app.include_router(users.router))
app.include_router(papers.router)
app.include_router(users.router)
