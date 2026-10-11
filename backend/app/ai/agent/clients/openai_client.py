"""설치된 openai 1.51.0의 chat.completions API Adapter."""
import json
from time import monotonic
from typing import Any, Sequence

from openai import OpenAI

from .._execution import check_deadline, positive_seconds, request_deadline
from ..schemas import Message, ModelTurn, ToolCall


class OpenAIClient:
    def __init__(self, sdk: OpenAI, *, model: str, timeout_seconds: float = 20.0):
        if not model.strip():
            raise ValueError("Model name is required")
        self.sdk = sdk
        self.model = model
        self.timeout_seconds = positive_seconds(timeout_seconds, "timeout_seconds")

    @staticmethod
    def _message(message: Message) -> dict[str, Any]:
        wire: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            wire["tool_calls"] = [{
                "id": call.id, "type": "function", "function": {
                    "name": call.name,
                    "arguments": call.arguments if isinstance(call.arguments, str) else
                    json.dumps(call.arguments, ensure_ascii=False, allow_nan=False),
                },
            } for call in message.tool_calls]
        if message.role == "tool":
            wire["tool_call_id"] = message.tool_call_id
        return wire

    def generate(self, messages: Sequence[Message], tools: list[dict[str, Any]]) -> ModelTurn:
        kwargs: dict[str, Any] = {"model": self.model, "messages": [self._message(m) for m in messages]}
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        check_deadline()
        deadline = request_deadline.get()
        timeout = self.timeout_seconds
        if deadline is not None:
            timeout = min(timeout, deadline - monotonic())
            if timeout <= 0:
                check_deadline()
        # 주입된 SDK/HTTP client를 유지하고 이 호출의 timeout/retry만 덮어쓴다.
        completion = self.sdk.with_options(timeout=timeout, max_retries=0).chat.completions.create(**kwargs)
        if not completion.choices:
            raise ValueError("Provider returned no choices")
        choice = completion.choices[0]
        if choice.finish_reason not in ("stop", "tool_calls"):
            raise ValueError("Provider returned an incomplete response")
        message = choice.message
        return ModelTurn(text=message.content, tool_calls=[
            ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments)
            for call in (message.tool_calls or [])
        ])
