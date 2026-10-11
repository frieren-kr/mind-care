"""Request-local Chat assembly over the unchanged shared AgentLoopRunner."""
from typing import Callable

from .._execution import positive_seconds
from ..agent_loop_runner import AgentLoopRunner
from ..clients.base import LLMClient
from ..outputs import ChatAnswerV1, StructuredAgentResult
from ..registry import ToolName, ToolRegistry
from ..schemas import AgentContext
from .chat_context_prepare import _validated_context
from .chat_prompts import ChatPromptBuilder


class ChatExecutionError(RuntimeError):
    def __init__(self):
        super().__init__("Chat execution failed")


class ChatExecutionV1:
    """Server factory owns allowed Tools/auth; Runner owns all loop/validation.

    Factory must omit patient Tools for user-scoped requests. A bad assembly
    fails before Provider execution. Add future patient Tools to that policy
    and the corresponding Executor guard, not a new registry framework.
    No Runner or mutable Registry is retained between requests.
    """

    def __init__(self, client: LLMClient, registry_factory: Callable[[AgentContext], ToolRegistry], *,
                 tool_timeout_seconds: float = 30.0, request_timeout_seconds: float = 120.0):
        try:
            if not callable(registry_factory):
                raise TypeError("Registry factory required")
            self.client = client
            self.registry_factory = registry_factory
            self.tool_timeout_seconds = positive_seconds(tool_timeout_seconds, "tool_timeout_seconds")
            self.request_timeout_seconds = positive_seconds(request_timeout_seconds, "request_timeout_seconds")
        except Exception:
            raise ChatExecutionError() from None

    def __call__(self, context: AgentContext, prepared_input: str) -> StructuredAgentResult:
        try:
            context = _validated_context(context)
            if not isinstance(prepared_input, str) or not prepared_input.strip():
                raise TypeError("Prepared Chat input required")
            supplied = self.registry_factory(context)
            if not isinstance(supplied, ToolRegistry):
                raise TypeError("ToolRegistry required")
            # Snapshot through the existing canonical Registry validator. Even a
            # reused factory registry cannot share its mutable mapping with a run.
            specs = tuple(supplied.get(d["function"]["name"]) for d in supplied.definitions())
            registry = ToolRegistry(specs, mode=supplied.mode)
            if context.patient_id is None and any(registry.get(name.value) is not None for name in (
                    ToolName.PATIENT_PROFILE, ToolName.RECENT_CARE_LOGS)):
                raise ValueError("Patient Tools require a selected patient")
            runner = AgentLoopRunner(self.client, registry, prompt_builder=ChatPromptBuilder(),
                max_tool_rounds=5, max_total_tool_calls=10,
                tool_timeout_seconds=self.tool_timeout_seconds,
                request_timeout_seconds=self.request_timeout_seconds)
            return runner.run_structured(prepared_input, context, output_model=ChatAnswerV1)
        except Exception:
            raise ChatExecutionError() from None
