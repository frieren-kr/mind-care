from typing import Any, Protocol, Sequence

from ..schemas import Message, ModelTurn


class LLMClient(Protocol):
    def generate(self, messages: Sequence[Message], tools: list[dict[str, Any]]) -> ModelTurn:
        """공개 답변과 Tool Call만 반환한다. 숨겨진 reasoning은 받지 않는다."""
        ...
