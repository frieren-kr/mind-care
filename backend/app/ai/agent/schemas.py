"""Agent 내부 계약. app.schemas의 DB 계약을 변경하지 않는다."""
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AgentContext(AgentModel):
    """인증/환자 접근 권한을 검증한 서버만 생성해야 한다."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str = Field(min_length=1, max_length=128)
    user_id: UUID
    # Explicit None is a user-scoped request, not a missing authenticated user.
    patient_id: UUID | None
    locale: str = "ko-KR"


class ToolCall(AgentModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    # Provider의 잘못된 JSON도 Executor에서 안전한 Tool Error로 처리한다.
    arguments: dict[str, Any] | str


class ModelTurn(AgentModel):
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


class Message(AgentModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    kind: Literal["policy", "agent_context", "user_input", "model", "tool_result", "evidence"]
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class AgentError(AgentModel):
    code: str
    message: str


class ToolOutcome(AgentModel):
    call_id: str
    tool_name: str
    success: bool
    data: dict[str, Any] | None = None
    error: AgentError | None = None


class ToolTrace(AgentModel):
    tool_name: str
    success: bool
    duration_ms: float
    result_count: int | None = None
    error_type: str | None = None


class AgentState(AgentModel):
    request_id: str
    messages: list[Message] = Field(default_factory=list)
    tool_round: int = 0
    total_tool_calls: int = 0
    called_tools: list[ToolTrace] = Field(default_factory=list)
    errors: list[AgentError] = Field(default_factory=list)
    final_answer: str | None = None


class AgentResult(AgentModel):
    status: Literal["completed", "limit_reached", "provider_error", "invalid_response", "failed"]
    request_id: str
    final_answer: str | None = None
    tool_rounds: int
    total_tool_calls: int
    called_tools: list[ToolTrace]
    errors: list[AgentError]
    # 전체 prompt/의료 원문/숨겨진 reasoning은 외부 결과에 포함하지 않는다.
