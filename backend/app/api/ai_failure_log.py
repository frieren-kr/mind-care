"""챗봇·AI 피드 실행 실패의 실제 원인을 WARNING 로그로 남긴다 (박주현, api/chat.py·recommendations.py 공용).

ChatWorkflow/FeedWorkflow는 실패한 실행 결과를 backend_handoff에서 ValueError 하나로 바꾸고,
에이전트 루프는 OpenAI 예외를 provider_error로 삼킨다. 그래서 API 쪽에서 두 곳을 감싼다.
  - logged_execution(): 실행 결과의 status, errors의 code 목록, 실패한 Tool의 error_type을 남긴다.
  - ErrorRecordingClient: LLM 호출 예외에서 OpenAI 오류 종류·HTTP 상태·코드를 정규화하여 기록해 둔다.
    (Tool 호출은 다른 스레드에서 copy_context()로 돌므로 ContextVar에 담은 list를 함께 쓴다.)

남기지 않는 것: 키 값, 대화·질문 원문, 검색어, errors의 message. Provider 오류 원문도 기록하지 않는다.
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
    """레거시 문자열 헬퍼. 개인정보 제거가 아니므로 Provider 로그에는 사용하지 않는다."""
    text = _SECRET.sub("sk-***", " ".join(str(message).split()))
    return text[:MAX_MESSAGE_CHARS]


_PROVIDER_ERROR_TYPES = frozenset((
    "APIError", "APIConnectionError", "APITimeoutError", "APIStatusError", "APIResponseValidationError",
    "BadRequestError", "AuthenticationError", "PermissionDeniedError", "NotFoundError",
    "ConflictError", "UnprocessableEntityError", "RateLimitError", "InternalServerError",
    "ProviderResponseError", "TimeoutError", "RuntimeError", "ValueError",
))
_PROVIDER_CODES = frozenset((
    "invalid_api_key", "expired_secret_key", "insufficient_quota", "rate_limit_exceeded",
    "model_not_found", "context_length_exceeded", "invalid_request_error", "invalid_value",
    "unsupported_value", "server_error", "authentication_error", "permission_denied",
))


def describe_provider_error(exc: BaseException) -> str:
    """Log only allowlisted labels and a numeric HTTP status, never message/body text."""
    name = type(exc).__name__
    parts = [name if name in _PROVIDER_ERROR_TYPES else "ProviderError"]
    status = getattr(exc, "status_code", None)
    if type(status) is int and 100 <= status <= 599:
        parts.append(f"status={status}")
    code = getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    if code is None and isinstance(body, dict):
        code = (body.get("error") if isinstance(body.get("error"), dict) else body).get("code")
    if code is not None:
        parts.append("code=" + (code if type(code) is str and code in _PROVIDER_CODES else "unknown"))
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
