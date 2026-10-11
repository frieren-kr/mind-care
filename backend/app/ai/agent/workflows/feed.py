"""Demo Feed: prepare Context, execute once, hand validated results to Backend.

Production Context V1.1 and recommendations API integration are separate work.
No DB writes, paper identity mapping, clinical filtering or fake persistence.
"""
from typing import Callable, Literal, Protocol

from ..outputs import FeedAnswerV1, StructuredAgentResult
from ..schemas import AgentContext
from .feed_profile_planning import FeedProfilePlanningInputV1, build_feed_provider_input


FeedStage = Literal["context_prepare", "feed_execution", "backend_handoff"]


class FeedContextLoader(Protocol):
    def __call__(self, context: AgentContext) -> FeedProfilePlanningInputV1:
        """Trusted Backend UserProfile read + Mapper; caller supplies auth/time.

        Return the Demo input after scope/time validation, never a Production
        FeedPersonalizationContextV1 or empty data substituted for a failed read.
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

    def __init__(self, *, context_loader: FeedContextLoader,
                 feed_execution: Callable[[AgentContext, str], StructuredAgentResult],
                 backend_handoff: Callable[[AgentContext, StructuredAgentResult], object]):
        self.context_loader = context_loader
        self.feed_execution = feed_execution
        self.backend_handoff = backend_handoff

    @staticmethod
    def _execute(stage: FeedStage, operation: Callable[[], object]):
        try:
            return operation()
        except Exception:
            raise FeedWorkflowError(stage) from None

    def run(self, context: AgentContext) -> object:
        provider_input = self._execute("context_prepare", lambda:
            build_feed_provider_input(self.context_loader(context)))
        result = self._execute("feed_execution", lambda: self.feed_execution(context, provider_input))
        return self._execute("backend_handoff", lambda: self._handoff(context, result))

    def _handoff(self, context: AgentContext, result: StructuredAgentResult) -> object:
        # Reuse the existing wrapper validator at the handoff trust boundary.
        # This checks mutated nested values, not a second parser or source resolver.
        if not isinstance(result, StructuredAgentResult):
            raise TypeError("Structured Feed execution required")
        validated = StructuredAgentResult.model_validate(result)
        if (validated.execution.status != "completed" or not isinstance(validated.output, FeedAnswerV1)
                or validated.execution.request_id != context.request_id):
            raise ValueError("Completed Feed output for this request required")
        return self.backend_handoff(context, validated)
