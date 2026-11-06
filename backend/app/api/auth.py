"""기존 계정 로그인·내 정보·현재 세션 로그아웃 API."""
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from psycopg import Error as DatabaseError

from app.auth.dependencies import get_current_user, get_token_identity, unavailable
from app.auth.rate_limit import check_login_limit
from app.auth.security import TokenIdentity
from app.auth.service import login
from app.auth_schemas import AuthUser, LoginIn, LoginOut, LogoutOut
from app.db import functions


class AuthRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                response = await handler(request)
            except RequestValidationError:
                # FastAPI 기본 422의 input/ctx에는 원문 비밀번호가 들어갈 수 있다.
                response = JSONResponse(status_code=422, content={"detail": "로그인 입력 형식을 확인해 주세요."})
            except HTTPException as exc:
                response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
            return response

        return safe_handler


router = APIRouter(prefix="/auth", tags=["auth"], route_class=AuthRoute)


@router.post("/login", response_model=LoginOut, dependencies=[Depends(check_login_limit)])
def login_user(data: LoginIn) -> LoginOut:
    return login(data)


@router.get("/me", response_model=AuthUser)
def me(user: Annotated[AuthUser, Depends(get_current_user)]) -> AuthUser:
    return user


@router.post("/logout", response_model=LogoutOut)
def logout(identity: Annotated[TokenIdentity, Depends(get_token_identity)]) -> LogoutOut:
    """서명·만료를 확인한 토큰으로 현재 세션 폐기. 같은 토큰의 반복 호출도 성공."""
    try:
        functions.revoke_auth_session(identity.user_id, identity.session_id, reference_time=datetime.now(timezone.utc))
    except DatabaseError:
        raise unavailable() from None
    return LogoutOut()
