"""챗봇 API (박주현 담당, 10/28 회의).

POST /chat/{user_id}   본문 {"message": "...", "patient_id": null}

흐름 (웅 님 ChatWorkflow 3단계, app/ai/agent/README.md의 조립 인터페이스 그대로):
  ① get_user_profile_context()로 사용자·환자 확인 (404 판정)   [한슬님 함수]
  ② context_prepare = prepare_chat_input  (질문 검증 → 최소 JSON + 이전 대화)
       이전 대화는 chat_history.ChatStoreHistoryReader가 읽는다 (최근 완료 턴 5개 + 오늘 이전 하루 요약).
       조회가 실패하거나 2초를 넘으면 이전 대화 없이 진행한다 (경고 로그만).
     chat_execution  = ChatExecutionV1(OpenAIClient, 요청별 Tool 목록)  (LLM + Tool 반복)
       Tool 목록은 요청마다 build_chat_registry(context)로 만든다.
       항상: search_evidence·search_guidelines·get_caregiver_profile
       환자를 고른 요청만: 환자 프로필·돌봄 기록·복약·진료·평가·안전 사건
     backend_handoff = 답변·출처를 화면 형식으로 옮김
  ③ 실행기가 없거나 실패하면 503 (검색 대체 응답은 없다)

아직 DB 함수가 없는 Tool 경로(돌봄 기록 get_recent_care_logs, 다가오는 진료 get_patient_medical_visits
status=upcoming)는 부르면 그 Tool만 실패하고 답변은 계속 만든다.
답변에 성공하면 질문·답변을 chat_store.save_chat_turn()으로 저장한다 (7일 보관 → 하루 요약, scripts/summarize_chats.py).
저장이 실패해도 답변은 그대로 돌려주고 로그만 남긴다.
라우터 공통 의존성(require_user_access)이 로그인·본인 확인을 실행기 조립과 데이터 조회보다 먼저 한다
(api/users.py·recommendations.py와 같은 방식). 토큰 없음·만료 401, 남의 user_id 403, 남의 환자 404.
"""
import logging
from contextlib import suppress
from functools import lru_cache
from typing import Callable, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from openai import OpenAI

from app.ai.agent.clients.openai_client import OpenAIClient
from app.ai.agent.composition import build_chat_registry
from app.ai.agent.outputs import StructuredAgentResult
from app.ai.agent.schemas import AgentContext
from app.ai.agent.workflows.chat import ChatWorkflow, ChatWorkflowError
from app.ai.agent.workflows.chat_context_prepare import prepare_chat_input as _prepare_chat_input
from app.api.chat_history import ChatStoreHistoryReader
from app.ai.agent.workflows.chat_execution import ChatExecutionV1
from app.ai.agent.workflows.chat_history_context import chat_history_token_counter
from app.auth.dependencies import require_user_access
from app.config import settings
from app.db import chat_store, user_functions
from app.schemas import ChatRequest, ChatResponse, ChatSource

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[Depends(require_user_access)])

_USER_NOT_FOUND = "사용자를 찾을 수 없습니다."
_PATIENT_NOT_FOUND = "환자를 찾을 수 없습니다."
_UNAVAILABLE = "지금은 답변을 만들 수 없어요. 잠시 후 다시 시도해 주세요."

ChatExecution = Callable[[AgentContext, str], StructuredAgentResult]

# 화면(api/index.ts)의 대기 시간이 90초라서 그 안에 끝나게 한다.
REQUEST_TIMEOUT_SECONDS = 80.0
LLM_TIMEOUT_SECONDS = 30.0


_HISTORY_READER = ChatStoreHistoryReader()


def chat_model() -> str:
    """실행기와 이전 대화 토큰 계산이 함께 쓰는 모델 이름 (get_feed_execution()과 같은 순서)."""
    return settings.openai_agent_model.strip() or settings.openai_summary_model.strip()


def prepare_chat_input(context: AgentContext, user_message: str) -> str:
    """ChatWorkflow 1단계. 웅 님 prepare_chat_input에 이전 대화 Reader와 실행 모델을 붙인다.

    모듈 이름으로 부르므로 테스트에서 patch.object(chat, "prepare_chat_input", ...)로 바꿔 끼울 수 있다.
    """
    return _prepare_chat_input(context, user_message, history_reader=_HISTORY_READER, model=chat_model())


def _example(detail: str) -> dict:
    """OpenAPI 문서(/docs)에 보여줄 오류 응답 예시."""
    return {"description": detail, "content": {"application/json": {"example": {"detail": detail}}}}


@lru_cache(maxsize=1)
def get_chat_execution() -> Optional[ChatExecution]:
    """ChatWorkflow의 2단계(chat_execution)를 돌려주는 FastAPI 의존성. get_feed_execution()과 같은 설정.

    키/모델이 없거나 조립이 실패하면 None (→ 503). 설정 변경 후 서버 재시작 필요.
    조립만으로 OpenAI API를 호출하지 않는다. 다만 이전 대화 토큰 계산용 tiktoken 인코딩을 여기서 한 번 읽는다
    (디스크 캐시가 없으면 처음 한 번 내려받아 약 3초). 요청 안의 이력 조회 제한(2초)에 걸려 첫 요청의
    이전 대화가 빠지지 않게 하려는 것이다. 실패해도 UTF-8 상한으로 대신 세므로 오류는 나지 않는다.
    테스트에서는 app.dependency_overrides[get_chat_execution]으로 바꿔 끼운다.
    """
    api_key = settings.openai_api_key.strip()
    model = chat_model()
    if not api_key or not model:
        logger.info("챗봇 AI 설정 없음 → 503으로 응답")
        return None

    sdk = None
    try:
        chat_history_token_counter(model)
        sdk = OpenAI(api_key=api_key, timeout=LLM_TIMEOUT_SECONDS, max_retries=0)
        client = OpenAIClient(sdk, model=model, timeout_seconds=LLM_TIMEOUT_SECONDS)
        # 요청마다 새 Tool 목록. 환자를 고르지 않으면 환자 Tool을 빼는 것은 build_chat_registry가 하고
        # ChatExecutionV1이 한 번 더 검사한다. 어댑터는 상태가 없어 요청마다 만들어도 DB에 가지 않는다.
        return ChatExecutionV1(client, build_chat_registry,
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

    - `Authorization: Bearer <access_token>` 필요. 없거나 만료면 401, 경로가 본인 user_id가 아니면 403.
    - `sources`는 답변이 인용한 순서 그대로다. 근거 없이 답하면 빈 목록이다.
    - `patient_id`는 선택이다. 이 사용자의 환자가 아니면(없는 환자 포함) 404.
    - 없는 사용자는 404. 질문이 비었거나 4000자를 넘으면 422.
    - AI를 쓸 수 없거나 실패하면 503. 최대 약 80초 걸릴 수 있다.
    - 답한 대화는 7일 동안 저장되고, 같은 사용자·같은 환자 범위의 최근 대화 5턴과 지난 날짜 하루 요약을
      참고해 답한다 (`patient_id` 없음은 환자 없는 대화끼리만). 이전 대화를 못 읽어도 답변은 나간다.
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
        response = ChatWorkflow(
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
    _save_turn(user_id, body.patient_id, body.message, response)
    return response


def _save_turn(user_id: UUID, patient_id: Optional[UUID], question: str, response: ChatResponse) -> None:
    """대화 저장. 실패해도 답변은 이미 만들어졌으므로 사용자에게 오류를 내지 않는다 (질문 원문은 로그에 남기지 않는다)."""
    try:
        chat_store.save_chat_turn(user_id, patient_id, question, response.answer,
                                  [source.model_dump() for source in response.sources])
    except Exception as exc:
        logger.warning("챗봇 대화 저장 실패 — 답변은 그대로 보냄 (%s)", type(exc).__name__)
