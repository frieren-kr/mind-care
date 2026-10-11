"""검증 → Handler → 출력 검증 → 안전한 JSON. 원문/예외 내용은 로그 금지."""
import json
import logging
from time import perf_counter
from uuid import UUID

from httpx import TimeoutException
from openai import APITimeoutError
from pydantic import BaseModel, ValidationError

from ._execution import RequestTimeout, bounded_handler, positive_seconds
from .registry import ToolName, ToolRegistry
from .schemas import AgentContext, AgentError, ToolCall, ToolOutcome, ToolTrace

logger = logging.getLogger(__name__)

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
        if spec is None:
            error_code = "unknown_tool"
        elif (spec.contract.name in (ToolName.PATIENT_PROFILE, ToolName.RECENT_CARE_LOGS)
              and not isinstance(context.patient_id, UUID)):
            error_code = "patient_context_mismatch"
        else:
            try:
                args = json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
                validated = spec.contract.input_model.model_validate(args)
            except (ValueError, TypeError, ValidationError):
                error_code = "invalid_arguments"
            if error_code is None:
                try:
                    raw = bounded_handler(lambda: spec.handler(context, validated), self.timeout_seconds)
                except RequestTimeout:
                    raise
                # 명시적인 타입만 판별한다. 예외 이름/메시지로 timeout을 추측하지 않는다.
                except (TimeoutError, TimeoutException, APITimeoutError):
                    error_code = "tool_timeout"
                except Exception:
                    error_code = "tool_exception"
                else:
                    if raw is None:
                        error_code = "empty_result"
                    else:
                        try:
                            # model_construct나 다른 모델의 무검증 반환도 재검증한다.
                            raw = raw.model_dump(warnings=False) if isinstance(raw, BaseModel) else raw
                            output = spec.contract.output_model.model_validate(raw)
                        except Exception:
                            error_code = "invalid_output"
                        else:
                            if "patient_id" in type(output).model_fields and output.patient_id != context.patient_id:
                                error_code = "patient_context_mismatch"
                            else:
                                try:
                                    data = output.model_dump(mode="json")
                                    json.dumps(data, ensure_ascii=False, allow_nan=False)
                                except Exception:
                                    error_code = "serialization_error"
                                    data = None
        error = AgentError(code=error_code, message=ERROR_MESSAGES[error_code]) if error_code else None
        outcome = ToolOutcome(call_id=call.id, tool_name=call.name,
                              success=error is None, data=data, error=error)
        count = None
        if data is not None:
            for key in ("logs", "evidence", "data"):
                if isinstance(data.get(key), list):
                    count = len(data[key])
                    break
        # 미등록 이름도 입력 유래이므로 로그에는 넣지 않는다.
        safe_name = spec.contract.name.value if spec else "<unknown>"
        trace = ToolTrace(tool_name=safe_name, success=outcome.success,
                          duration_ms=(perf_counter() - started) * 1000,
                          result_count=count, error_type=error_code)
        logger.info("agent_tool request_id=%s tool_name=%s success=%s duration_ms=%.2f result_count=%s error_type=%s",
                    context.request_id, safe_name, trace.success, trace.duration_ms, count, error_code)
        return outcome, trace
