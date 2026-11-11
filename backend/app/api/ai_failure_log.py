"""챗봇·AI 피드 실행 실패의 실제 원인을 WARNING 로그로 남긴다 (박주현, api/chat.py·recommendations.py 공용).

ChatWorkflow/FeedWorkflow는 실패한 실행 결과를 backend_handoff에서 ValueError 하나로 바꾸고,
에이전트 루프는 OpenAI 예외를 provider_error로 삼킨다. 그래서 API 쪽에서 두 곳을 감싼다.
  - logged_execution(): 실행 결과의 status, errors의 code 목록, 실패한 Tool의 error_type을 남긴다.
  - ErrorRecordingClient: LLM 호출 예외에서 OpenAI 오류 종류·HTTP 상태·코드·메시지를 기록해 둔다.
    (Tool 호출은 다른 스레드에서 copy_context()로 돌므로 ContextVar에 담은 list를 함께 쓴다.)

남기지 않는 것: 키 값, 대화·질문 원문, 검색어, errors의 message. 오류 메시지는 OpenAI가 준 것만
한 줄로 바꾸고 'sk-…'를 가린 뒤 300자로 자른다.
"""
import logging
import re
from contextvars import ContextVar
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 300
_SECRET = re.compile(r"sk-[A-Za-z0-9_\-*.]*")
_provider_errors: ContextVar[Optional[list[str]]] = ContextVar("mindcare_provider_errors", default=None)


def clean_message(message: Any) -> str:
    """한 줄로 바꾸고 sk-로 시작하는 값을 가린 뒤 300자로 자른다."""
    text = _SECRET.sub("sk-***", " ".join(str(message).split()))
    return text[:MAX_MESSAGE_CHARS]


def describe_provider_error(exc: BaseException) -> str:
    """OpenAI 예외 → '종류 status=401 code=invalid_api_key message=…' (없는 칸은 뺀다)."""
    code = getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    if code is None and isinstance(body, dict):
        code = (body.get("error") if isinstance(body.get("error"), dict) else body).get("code")
    parts = [type(exc).__name__]
    status = getattr(exc, "status_code", None)
    if status is not None:
        parts.append(f"status={status}")
    if code is not None:
        parts.append(f"code={clean_message(code)}")
    message = getattr(exc, "message", None) or str(exc)
    if message:
        parts.append(f"message={clean_message(message)}")
    return " ".join(parts)


class ErrorRecordingClient:
    """LLM 클라이언트를 감싸 generate() 예외를 기록하고 그대로 다시 던진다. 동작은 바꾸지 않는다."""

    def __init__(self, client: Any):
        self._client = client

    def generate(self, request):
        try:
            return self._client.generate(request)
        except Exception as exc:
            recorded = _provider_errors.get()
            if recorded is not None:
                recorded.append(describe_provider_error(exc))
            raise

    def __getattr__(self, name: str):
        return getattr(self._client, name)


def _failure_detail(result: Any) -> str:
    execution = getattr(result, "execution", None)
    if execution is None:
        return f"result={type(result).__name__}"
    codes = [error.code for error in execution.errors]
    tools = [f"{trace.tool_name}:{trace.error_type}" for trace in execution.called_tools if not trace.success]
    return f"status={execution.status} errors={codes} tools={tools}"


def logged_execution(label: str, execution: Callable) -> Callable:
    """실행기(context, ...)를 감싸 실패하면 원인을 WARNING으로 남긴다. 결과·예외는 그대로 돌려준다."""

    def run(context, *args):
        recorded: list[str] = []
        token = _provider_errors.set(recorded)
        try:
            result = execution(context, *args)
        except Exception as exc:
            logger.warning("%s AI 실행 예외 (request_id=%s, %s, provider=%s)",
                           label, context.request_id, type(exc).__name__, recorded)
            raise
        finally:
            _provider_errors.reset(token)
        if getattr(getattr(result, "execution", None), "status", None) != "completed":
            logger.warning("%s AI 실행 실패 (request_id=%s, %s, provider=%s)",
                           label, context.request_id, _failure_detail(result), recorded)
        return result

    return run
