"""Three-stage Chat skeleton; no DB persistence, Feed inheritance or Tool loop."""
import json
from typing import Callable, Literal, Protocol

from ..outputs import ChatAnswerV1, StructuredAgentResult
from ..schemas import AgentContext

ChatStage = Literal["context_prepare", "chat_execution", "backend_handoff"]


class ChatContextPrepare(Protocol):
    def __call__(self, context: AgentContext, user_message: str) -> str:
        """Trusted input validation/minimization; Backend owns auth and future history."""
        ...


class ChatWorkflowError(RuntimeError):
    def __init__(self, stage: ChatStage):
        self.stage = stage
        super().__init__(f"Chat workflow stopped at {stage}")


class ChatWorkflow:
    def __init__(self, *, context_prepare: ChatContextPrepare,
                 chat_execution: Callable[[AgentContext, str], StructuredAgentResult],
                 backend_handoff: Callable[[AgentContext, StructuredAgentResult], object]):
        self.context_prepare = context_prepare
        self.chat_execution = chat_execution
        self.backend_handoff = backend_handoff

    @staticmethod
    def _execute(stage: ChatStage, operation: Callable[[], object]):
        try:
            return operation()
        except Exception:
            raise ChatWorkflowError(stage) from None

    def run(self, context: AgentContext, user_message: str) -> object:
        prepared = self._execute("context_prepare", lambda: self._prepare(context, user_message))
        result = self._execute("chat_execution", lambda: self.chat_execution(context, prepared))
        return self._execute("backend_handoff", lambda: self._handoff(context, result))

    def _prepare(self, context: AgentContext, user_message: str) -> str:
        value = self.context_prepare(context, user_message)
        # Format guard only; the injected trusted Stage 1 owns the input contract.
        if not isinstance(value, str) or not value.strip() or not isinstance(json.loads(value), dict):
            raise TypeError("Prepared Chat JSON object required")
        return value

    def _handoff(self, context: AgentContext, result: StructuredAgentResult) -> object:
        if not isinstance(result, StructuredAgentResult):
            raise TypeError("Structured Chat result required")
        validated = StructuredAgentResult.model_validate(result)
        if (validated.execution.status != "completed" or not isinstance(validated.output, ChatAnswerV1)
                or validated.execution.request_id != context.request_id):
            raise ValueError("Completed Chat result for this request required")
        return self.backend_handoff(context, validated)
