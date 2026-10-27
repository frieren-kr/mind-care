"""Feed policy and one shared structured Agent loop over prepared input."""
from ..agent_loop_runner import AgentLoopRunner
from ..clients.base import LLMClient
from ..outputs import FeedAnswerV2, StructuredAgentResult
from ..registry import ToolName, ToolRegistry
from ..schemas import AgentContext
from .feed_prompts import FeedPromptBuilder
from .feed_context_prepare import PreparedFeedInput


class FeedExecutionError(RuntimeError):
    """Fixed failure; never expose input or underlying exception data."""

    def __init__(self):
        super().__init__("Feed execution failed")


class FeedExecutionV1:
    """Own Feed runner configuration, not Context selection or a Tool loop.

    Input is prepared by Stage 1. Runner failure statuses remain normal results;
    the Workflow's handoff boundary admits only completed FeedAnswerV2 results.
    """

    def __init__(self, client: LLMClient, registry: ToolRegistry, *,
                 tool_timeout_seconds: float = 30.0, request_timeout_seconds: float = 120.0,
                 allow_parallel_tools: bool = True, max_parallel_calls: int = 3):
        try:
            self.runner = AgentLoopRunner(client, registry, prompt_builder=FeedPromptBuilder(),
                max_tool_rounds=3, max_total_tool_calls=9,
                allow_parallel_tools=allow_parallel_tools, max_parallel_calls=max_parallel_calls,
                tool_timeout_seconds=tool_timeout_seconds, request_timeout_seconds=request_timeout_seconds)
            self._validate_runner()
        except Exception:
            raise FeedExecutionError() from None

    def _validate_runner(self) -> None:
        runner = self.runner
        if not isinstance(runner.prompt_builder, FeedPromptBuilder):
            raise ValueError("Feed runner requires FeedPromptBuilder")
        if runner.executor.registry is not runner.registry:
            raise ValueError("Feed executor and runner must share the registry")
        names = [definition["function"]["name"] for definition in runner.registry.definitions()]
        if names != [ToolName.SEARCH_EVIDENCE.value]:
            raise ValueError("Feed runner requires search_evidence only")
        if runner.max_tool_rounds != 3 or runner.max_total_tool_calls != 9:
            raise ValueError("Feed runner requires 3 rounds and 9 total search calls")

    def __call__(self, context: AgentContext, provider_input: str) -> StructuredAgentResult:
        try:
            # Configuration is mutable after assembly; validate the actual execution path.
            self._validate_runner()
            if not isinstance(provider_input, str) or not provider_input.strip():
                raise TypeError("Prepared Feed input required")
            pmids = (provider_input.previously_recommended_pmids
                     if isinstance(provider_input, PreparedFeedInput) else frozenset())
            return self.runner.run_structured(str(provider_input), context, output_model=FeedAnswerV2,
                                               previously_recommended_pmids=pmids)
        except Exception:
            raise FeedExecutionError() from None
