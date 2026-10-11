from collections import deque
from copy import deepcopy
from typing import Any, Sequence

from ..schemas import Message, ModelTurn


class FakeLLMClient:
    """스크립트 순서대로 응답하며, 테스트용 호출 snapshot을 남긴다."""

    def __init__(self, turns: Sequence[ModelTurn | Exception]):
        self.turns = deque(deepcopy(turns))
        self.requests: list[tuple[list[Message], list[dict[str, Any]]]] = []

    def generate(self, messages: Sequence[Message], tools: list[dict[str, Any]]) -> ModelTurn:
        self.requests.append((deepcopy(list(messages)), deepcopy(tools)))
        if not self.turns:
            raise RuntimeError("FakeLLM script exhausted")
        turn = self.turns.popleft()
        if isinstance(turn, Exception):
            raise turn
        return turn
