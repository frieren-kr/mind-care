"""챗봇 API (박주현 담당, 10/28 회의).

POST /chat/{user_id}   본문 {"message": "...", "patient_id": null}

흐름 (웅 님 ChatWorkflow 3단계, app/ai/agent/README.md의 조립 인터페이스 그대로):
  ① get_user_profile_context()로 사용자·환자 확인 (404 판정)   [한슬님 함수]
  ② context_prepare = prepare_chat_input  (질문 검증 → 최소 JSON)
     chat_execution  = ChatExecutionV1(OpenAIClient, 도구 목록)  (LLM + search_evidence 반복)
     backend_handoff = 답변·출처를 화면 형식으로 옮김
  ③ 실행기가 없거나 실패하면 503 (검색 대체 응답은 없다)

도구는 지금 search_evidence 하나다. 환자 도구(프로필·돌봄 기록)는 웅 님 작업(11/8) 후 붙인다.
대화 저장은 아직 하지 않는다 (요약 저장 형식은 웅 님과 정한 뒤 따로).
로그인이 아직 없어서 user_id를 경로로 받는다 (api/users.py와 같은 방식).
"""
import logging
from contextlib import suppress
from functools import lru_cache
from typing import Callable, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from openai import OpenAI

from app.ai.agent.clients.openai_client import OpenAIClient
from app.ai.agent.composition import build_evidence_registry
from app.ai.agent.outputs import StructuredAgentResult
from app.ai.agent.schemas import AgentContext
from app.ai.agent.workflows.chat import ChatWorkflow, ChatWorkflowError
from app.ai.agent.workflows.chat_context_prepare import prepare_chat_input
from app.ai.agent.workflows.chat_execution import ChatExecutionV1
from app.config import settings
from app.db import user_functions
from app.schemas import ChatRequest, ChatResponse, ChatSource

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

_USER_NOT_FOUND = "사용자를 찾을 수 없습니다."
_PATIENT_NOT_FOUND = "환자를 찾을 수 없습니다."
_UNAVAILABLE = "지금은 답변을 만들 수 없어요. 잠시 후 다시 시도해 주세요."

ChatExecution = Callable[[AgentContext, str], StructuredAgentResult]

# 화면(api/index.ts)의 대기 시간이 90초라서 그 안에 끝나게 한다.
REQUEST_TIMEOUT_SECONDS = 80.0
LLM_TIMEOUT_SECONDS = 30.0


def _example(detail: str) -> dict:
    """OpenAPI 문서(/docs)에 보여줄 오류 응답 예시."""
    return {"description": detail, "content": {"application/json": {"example": {"detail": detail}}}}


@lru_cache(maxsize=1)
def get_chat_execution() -> Optional[ChatExecution]:
    """ChatWorkflow의 2단계(chat_execution)를 돌려주는 FastAPI 의존성. get_feed_execution()과 같은 설정.

    키/모델이 없거나 조립이 실패하면 None (→ 503). 설정 변경 후 서버 재시작 필요.
    조립만으로 외부 API를 호출하지 않는다.
    테스트에서는 app.dependency_overrides[get_chat_execution]으로 바꿔 끼운다.
    """
    api_key = settings.openai_api_key.strip()
    model = settings.openai_agent_model.strip() or settings.openai_summary_model.strip()
    if not api_key or not model:
        logger.info("챗봇 AI 설정 없음 → 503으로 응답")
        return None

    sdk = None
    try:
        # 도구가 search_evidence뿐이라 환자 선택 여부와 상관없이 같은 목록을 준다.
        # 환자 도구가 붙으면 context.patient_id가 None일 때 빼야 한다 (ChatExecutionV1이 검사).
        registry = build_evidence_registry()
        sdk = OpenAI(api_key=api_key, timeout=LLM_TIMEOUT_SECONDS, max_retries=0)
        client = OpenAIClient(sdk, model=model, timeout_seconds=LLM_TIMEOUT_SECONDS)
        return ChatExecutionV1(client, lambda _context: registry,
                               request_timeout_seconds=REQUEST_TIMEOUT_SECONDS)
    except Exception as exc:
        if sdk is not None:
            with suppress(Exception):
                sdk.close()
        # 키나 Provider 오류 원문은 로그에 남기지 않는다.
        logger.warning("챗봇 AI 초기화 실패 → 503으로 응답 (%s)", type(exc).__name__)
        return None


def backend_handoff(_context: AgentContext, result: StructuredAgentResult) -> ChatResponse:
    """ChatWorkflow 3단계. 답변과 출처(citation_refs 순서 그대로)를 화면 형식으로 옮긴다."""
    return ChatResponse(
        answer=result.output.answer,
        sources=[ChatSource(**source.model_dump(exclude={"source_ref"})) for source in result.sources],
    )


@router.post(
    "/{user_id}",
    response_model=ChatResponse,
    summary="챗봇에게 질문",
    responses={404: _example(_USER_NOT_FOUND), 503: _example(_UNAVAILABLE)},
)
def post_chat(
    user_id: UUID,
    body: ChatRequest,
    chat_execution: Optional[ChatExecution] = Depends(get_chat_execution),
) -> ChatResponse:
    """간병인의 질문에 답하고, 답에 쓴 근거 자료를 `sources`로 돌려준다.

    - `sources`는 답변이 인용한 순서 그대로다. 근거 없이 답하면 빈 목록이다.
    - `patient_id`는 선택이다. 이 사용자의 환자가 아니면(없는 환자 포함) 404.
    - 없는 사용자는 404. 질문이 비었거나 4000자를 넘으면 422.
    - AI를 쓸 수 없거나 실패하면 503. 최대 약 80초 걸릴 수 있다.
    - 대화는 아직 저장하지 않는다 (이전 대화를 기억하지 않는다).
    """
    profile = user_functions.get_user_profile_context(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=_USER_NOT_FOUND)
    if body.patient_id is not None and body.patient_id not in {row.patient.id for row in profile.patients}:
        raise HTTPException(status_code=404, detail=_PATIENT_NOT_FOUND)
    if chat_execution is None:
        raise HTTPException(status_code=503, detail=_UNAVAILABLE)

    context = AgentContext(request_id=str(uuid4()), user_id=user_id, patient_id=body.patient_id)
    try:
        return ChatWorkflow(
            context_prepare=prepare_chat_input,
            chat_execution=chat_execution,
            backend_handoff=backend_handoff,
        ).run(context, body.message)
    except ChatWorkflowError as exc:
        # 1단계는 질문 검증만 하므로 여기서 막히면 입력 문제다 (예: 제어 문자뿐인 질문).
        if exc.stage == "context_prepare":
            raise HTTPException(status_code=422, detail="질문을 확인해 주세요.") from None
        # 원래 예외 종류는 __context__에서 읽는다 (메시지는 남기지 않는다).
        logger.warning("챗봇 AI 실패 → 503 (stage=%s, %s)", exc.stage, type(exc.__context__).__name__)
        raise HTTPException(status_code=503, detail=_UNAVAILABLE) from None
