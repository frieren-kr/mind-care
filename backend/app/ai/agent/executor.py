"""검증 → Handler → 출력 검증 → 안전한 JSON. 원문/예외 내용은 로그 금지."""
import hmac
import json
import logging
import secrets
from contextvars import ContextVar
from time import perf_counter
from uuid import UUID

from httpx import TimeoutException
from openai import APITimeoutError
from pydantic import BaseModel, ValidationError
from psycopg import Error as PsycopgError

from ._execution import RequestTimeout, bounded_handler, positive_seconds
from .error_diagnostics import diagnostic_message, summarize_json_error, summarize_validation_error
from .registry import ToolRegistry
from .schemas import AgentContext, AgentError, ToolCall, ToolOutcome, ToolTrace

logger = logging.getLogger(__name__)
# Request-local metadata only; propagated by existing bounded worker contexts.
tool_diagnostic_context: ContextVar[dict] = ContextVar("tool_diagnostic_context", default={})

PERMISSION_FAILURE_MESSAGE = "Tool 실행 권한이 없습니다. 재시도하지 마세요."
_LOG_ID_KEY = secrets.token_bytes(32)


def diagnostic_call_id(call_id: str) -> str:
    """Process-local correlation only; Provider IDs never enter diagnostic logs."""
    return "call-" + hmac.digest(_LOG_ID_KEY, call_id.encode("utf-8", errors="surrogatepass"), "sha256").hex()[:32]


class ToolOutputValidationError(ValueError):
    """An adapter validated Tool output before returning it to the Executor."""

ERROR_MESSAGES = {
    "unknown_tool": "등록되지 않은 Tool입니다.",
    "invalid_arguments": "Tool 입력이 계약과 일치하지 않습니다.",
    "tool_exception": "Tool 실행에 실패했습니다.",
    "empty_result": "Tool이 결과를 반환하지 않았습니다.",
    "invalid_output": "Tool 출력이 계약과 일치하지 않습니다.",
    "serialization_error": "Tool 결과를 JSON으로 전달할 수 없습니다.",
    "tool_timeout": "Tool 실행 대기 시간이 초과되었습니다. 자동으로 재실행하지 않습니다.",
    "patient_context_mismatch": "Tool 환자 Scope가 실행 Context와 일치하지 않습니다.",
}


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, *, timeout_seconds: float = 30.0):
        self.registry = registry
        self.timeout_seconds = positive_seconds(timeout_seconds, "timeout_seconds")

    def execute(self, call: ToolCall, context: AgentContext) -> tuple[ToolOutcome, ToolTrace]:
        started = perf_counter()
        spec = self.registry.get(call.name)
        error_code = None
        data = None
        diagnostic = {}
        stage, origin, exception_type = "tool_admission", tool_diagnostic_context.get().get("input_origin", "llm_arguments"), None
        if spec is None:
            error_code = "unknown_tool"
            diagnostic = {"reason": "unregistered_tool", "expected": "registered Tool only"}
        elif (spec.contract.patient_scoped
              and not isinstance(context.patient_id, UUID)):
            error_code = "patient_context_mismatch"
        else:
            try:
                stage = "tool_arguments_json"
                args = json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
                stage = "tool_input_validation"
                validated = spec.contract.input_model.model_validate(args)
            except (ValueError, TypeError) as exc:
                error_code = "invalid_arguments"
                exception_type = type(exc).__name__
                if isinstance(exc, ValidationError):
                    diagnostic = summarize_validation_error(exc, spec.contract.input_model)
                elif isinstance(exc, json.JSONDecodeError):
                    diagnostic = summarize_json_error(exc)
            if error_code is None:
                try:
                    stage, origin = "tool_handler_execution", "validated_tool_input"
                    raw = bounded_handler(lambda: spec.handler(context, validated), self.timeout_seconds)
                except RequestTimeout:
                    raise
                # 명시적인 타입만 판별한다. 예외 이름/메시지로 timeout을 추측하지 않는다.
                except (TimeoutError, TimeoutException, APITimeoutError) as exc:
                    error_code = "tool_timeout"
                    exception_type = type(exc).__name__
                except ToolOutputValidationError as exc:
                    error_code = "invalid_output"
                    stage, origin = "tool_output_validation", "adapter_output"
                    cause = exc.__cause__ or exc
                    exception_type = type(cause).__name__
                    if isinstance(cause, ValidationError):
                        diagnostic = summarize_validation_error(cause, spec.contract.output_model)
                except Exception as exc:
                    error_code = "tool_exception"
                    exception_type = type(exc).__name__
                    if isinstance(exc, PermissionError) or (
                            isinstance(exc, PsycopgError) and exc.sqlstate == "42501"):
                        diagnostic = {"reason": "permission_denied"}
                    elif isinstance(exc, ValidationError):
                        diagnostic = summarize_validation_error(exc)
                else:
                    if raw is None:
                        error_code = "empty_result"
                    else:
                        try:
                            stage, origin = "tool_output_validation", "handler_output"
                            # model_construct나 다른 모델의 무검증 반환도 재검증한다.
                            raw = raw.model_dump(warnings=False) if isinstance(raw, BaseModel) else raw
                            output = spec.contract.output_model.model_validate(raw)
                        except Exception as exc:
                            error_code = "invalid_output"
                            exception_type = type(exc).__name__
                            if isinstance(exc, ValidationError):
                                diagnostic = summarize_validation_error(exc, spec.contract.output_model)
                        else:
                            if "patient_id" in type(output).model_fields and output.patient_id != context.patient_id:
                                error_code = "patient_context_mismatch"
                            else:
                                try:
                                    stage = "tool_output_serialization"
                                    data = output.model_dump(mode="json")
                                    json.dumps(data, ensure_ascii=False, allow_nan=False)
                                except Exception as exc:
                                    error_code = "serialization_error"
                                    exception_type = type(exc).__name__
                                    data = None
        message = ERROR_MESSAGES.get(error_code)
        if diagnostic.get("reason") == "permission_denied":
            message = PERMISSION_FAILURE_MESSAGE
        if error_code == "invalid_arguments" and diagnostic:
            message += " " + diagnostic_message(diagnostic)
        error = AgentError(code=error_code, message=message) if error_code else None
        outcome = ToolOutcome(call_id=call.id, tool_name=call.name,
                              success=error is None, data=data, error=error)
        count = None
        if data is not None:
            for key in ("logs", "evidence", "data", "visits", "assessments", "events"):
                if isinstance(data.get(key), list):
                    count = len(data[key])
                    break
        # 미등록 이름도 입력 유래이므로 로그에는 넣지 않는다.
        safe_name = spec.contract.name.value if spec else "<unknown>"
        if error_code:
            logger.info("agent_error request_id=%r tool_call_id=%r tool_name=%s metadata=%s "
                        "stage=%s input_origin=%s error_code=%s exception_type=%s diagnostic=%s",
                        context.request_id, diagnostic_call_id(call.id), safe_name, tool_diagnostic_context.get(),
                        stage, origin, error_code, exception_type, diagnostic)
        trace = ToolTrace(tool_name=safe_name, success=outcome.success,
                          duration_ms=(perf_counter() - started) * 1000,
                          result_count=count, error_type=error_code)
        logger.info("agent_tool request_id=%s tool_name=%s success=%s duration_ms=%.2f result_count=%s error_type=%s",
                    context.request_id, safe_name, trace.success, trace.duration_ms, count, error_code)
        return outcome, trace
