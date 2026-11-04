"""Explicit, call-scoped generation contracts. No ambient Provider options."""
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ..schemas import Message, ModelTurn


@dataclass(frozen=True)
class GenerationOptions:
    tool_choice: Literal["auto", "none", "required"] = "auto"
    required_tool: str | None = None
    parallel_tool_calls: bool | None = None
    # SDK 1.109.1 supports these values; actual model support is caller-owned.
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None


@dataclass(frozen=True)
class GenerationRequest:
    inputs: tuple[Message, ...]
    tools: list[dict[str, Any]]
    options: GenerationOptions = field(default_factory=GenerationOptions)


class LLMClient(Protocol):
    def generate(self, request: GenerationRequest) -> ModelTurn: ...
