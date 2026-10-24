from typing import Any, Literal, Protocol, Sequence
from contextvars import ContextVar
from dataclasses import dataclass

from ..schemas import Message, ModelTurn


class LLMClient(Protocol):
    def generate(self, messages: Sequence[Message], tools: list[dict[str, Any]]) -> ModelTurn:
        """공개 답변과 Tool Call만 반환한다. 숨겨진 reasoning은 받지 않는다."""
        ...


# Call-scoped options preserve the generate(messages, tools) client interface.
@dataclass(frozen=True)
class GenerationOptions:
    tool_choice: Literal["auto", "none"] = "auto"
    required_tool: str | None = None
    parallel_tool_calls: bool = False


generation_options: ContextVar[GenerationOptions | None] = ContextVar("agent_generation_options", default=None)


def generate_with_options(client: LLMClient, messages: Sequence[Message],
                          tools: list[dict[str, Any]], options: GenerationOptions) -> ModelTurn:
    token = generation_options.set(options)
    try:
        return client.generate(messages, tools)
    finally:
        generation_options.reset(token)
