"""Feed: prepare Context, execute once, hand validated results to Backend.

Production data sources and recommendations API adapters are separate work.
No DB writes, paper identity mapping, clinical filtering or fake persistence.
"""
import json
from typing import Callable, Literal, Protocol

from ..outputs import FeedAnswerV2, StructuredAgentResult
from ..schemas import AgentContext


FeedStage = Literal["context_prepare", "feed_execution", "backend_handoff"]


class FeedContextPrepare(Protocol):
    def __call__(self, context: AgentContext) -> str:
        """Trusted Stage 1: read/validate/select/minimize/serialize Provider input.

        Caller owns authentication and successful reads. Return prepared JSON,
        never a raw DTO or an empty substitute for a failed read.
        """
        ...


class FeedWorkflowError(RuntimeError):
    """Expose only the failed stage, never raw Context or exception text."""

    def __init__(self, stage: FeedStage):
        self.stage = stage
        super().__init__(f"Feed workflow stopped at {stage}")


class FeedWorkflow:
    """Three stages; Feed execution owns the shared Runner and its Tool loop.

    Handoff receives the V1 output plus server-resolved evidence metadata and
    trusted caregiver Context. Its return value is the Backend's own receipt.
    A real handoff is required explicitly; no unimplemented default claims success.
    """

    def __init__(self, *, context_prepare: FeedContextPrepare,
                 feed_execution: Callable[[AgentContext, str], StructuredAgentResult],
                 backend_handoff: Callable[[AgentContext, StructuredAgentResult], object]):
        self.context_prepare = context_prepare
        self.feed_execution = feed_execution
        self.backend_handoff = backend_handoff

    @staticmethod
    def _execute(stage: FeedStage, operation: Callable[[], object]):
        try:
            return operation()
        except Exception:
            raise FeedWorkflowError(stage) from None

    def run(self, context: AgentContext) -> object:
        provider_input = self._execute("context_prepare", lambda: self._prepare(context))
        result = self._execute("feed_execution", lambda: self.feed_execution(context, provider_input))
        return self._execute("backend_handoff", lambda: self._handoff(context, result))

    def _prepare(self, context: AgentContext) -> str:
        value = self.context_prepare(context)
        if not isinstance(value, str) or not value.strip():
            raise TypeError("Prepared Provider input required")
        # Format guard only. Disclosure/scope rules belong to the trusted Stage 1.
        if not isinstance(json.loads(value), dict):
            raise TypeError("Prepared Provider input must be a JSON object")
        return value

    def _handoff(self, context: AgentContext, result: StructuredAgentResult) -> object:
        # Reuse the existing wrapper validator at the handoff trust boundary.
        # This checks mutated nested values, not a second parser or source resolver.
        if not isinstance(result, StructuredAgentResult):
            raise TypeError("Structured Feed execution required")
        validated = StructuredAgentResult.model_validate(result)
        if (validated.execution.status != "completed" or not isinstance(validated.output, FeedAnswerV2)
                or validated.execution.request_id != context.request_id):
            raise ValueError("Completed Feed output for this request required")
        return self.backend_handoff(context, validated)
