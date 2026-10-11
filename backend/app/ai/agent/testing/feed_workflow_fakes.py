"""Test-only Backend DTO -> Mapper -> Feed execution -> handoff fixtures."""
from typing import Callable
from uuid import UUID

from app.user_schemas import APP_TIMEZONE, PatientContext, UserProfileContext

from ..outputs import StructuredAgentResult
from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..schemas import AgentContext, ModelTurn
from ..tools import contracts as c
from ..workflows.feed import FeedWorkflow
from ..workflows.feed_execution import FeedExecutionV1
from ..workflows.feed_context_prepare import prepare_demo_feed_input
from .fake_llm import FakeLLMClient
from .fake_tools import FIXTURE_NOW, fake_search_evidence


STAGES = ("context_prepare", "feed_execution", "backend_handoff")


def build_feed_test_registry(*, query_validator: Callable[[AgentContext, c.EvidenceSearchInput], None],
                             handler=fake_search_evidence) -> ToolRegistry:
    """Test hook before each search; not a production Query Guard."""
    def guarded_search(context, args):
        query_validator(context, args)
        return handler(context, args)

    return ToolRegistry((ToolSpec(TOOL_CONTRACTS[ToolName.SEARCH_EVIDENCE],
                                 guarded_search, test_only=True),), mode="test")


class FakeFeedDependencies:
    """Wire real Mapper/Feed execution/Runner to synthetic reads and receipts."""

    def __init__(self, context: AgentContext, *, profile=None, reference_time=FIXTURE_NOW,
                 fail_at=None, turns=None, client=None, handler=fake_search_evidence):
        self.events = []
        self.received = {}
        self.fail_at = fail_at
        self.search_queries = []
        self.saved = []
        self.saved_owners = []
        self.receipt = object()
        self.reference_time = reference_time
        if profile is None:
            profile = UserProfileContext(
                user_id=context.user_id, name="PRIVATE synthetic caregiver",
                caregiver_profile=None, generated_at=reference_time,
                patients=[PatientContext.model_validate({
                    "patient": {"id": UUID(int=i), "caregiver_id": context.user_id,
                                "name": "PRIVATE synthetic patient",
                                "symptoms": [f"synthetic signal {i}"], "interests": [],
                                "created_at": reference_time, "updated_at": reference_time},
                    "latest_assessments": [], "current_medications": [], "recent_safety_events": [],
                    "recent_visits": [], "upcoming_visits": [],
                }) for i in (() if context.patient_id is None else (context.patient_id.int,))])
        self.profile = profile
        self.personalization = None
        self.agent_result = None
        self.client = client if client is not None else FakeLLMClient(turns if turns is not None else [
            ModelTurn(text='{"schema_version":"1","response_type":"feed","items":[]}')
        ])
        self.execution = FeedExecutionV1(self.client,
            build_feed_test_registry(query_validator=self.validate_query, handler=handler))
        self.runner = self.execution.runner

    def _enter(self, stage, *received):
        self.events.append(stage)
        self.received[stage] = received
        if self.fail_at == stage:
            raise RuntimeError("PRIVATE synthetic failure detail")

    def validate_query(self, context, args):
        if args.query == "[FAKE BLOCKED]":
            raise ValueError("PRIVATE rejected fixture query")
        self.search_queries.append(args.query)

    def load_context(self, context):
        self._enter("context_prepare", context)
        self.personalization = prepare_demo_feed_input(context, self.profile,
            reference_time=self.reference_time, service_timezone=APP_TIMEZONE)
        return self.personalization

    def run_feed(self, context, provider_input):
        self._enter("feed_execution", context, provider_input)
        self.agent_result = self.execution(context, provider_input)
        return self.agent_result

    def handoff(self, context, result: StructuredAgentResult):
        self._enter("backend_handoff", context, result)
        # Record exactly what the Backend boundary receives, not invented paper IDs.
        self.saved.append(result)
        self.saved_owners.append(context.user_id)
        return self.receipt

    def workflow(self):
        return FeedWorkflow(context_prepare=self.load_context, feed_execution=self.run_feed,
                            backend_handoff=self.handoff)
