"""Test-only assembly using real Chat stages/Runner and existing synthetic Tools.

Fake Context/handlers are not Backend authorization or production persistence.
"""
import json
from copy import deepcopy
from typing import Sequence

from ..registry import TOOL_CONTRACTS, ToolName, ToolRegistry, ToolSpec
from ..schemas import AgentContext, ModelTurn, ToolCall
from ..workflows.chat import ChatWorkflow
from ..workflows.chat_context_prepare import prepare_chat_input
from ..workflows.chat_execution import ChatExecutionV1
from .fake_llm import FakeLLMClient
from .fake_tools import fake_get_patient_profile, fake_get_recent_care_logs, fake_search_evidence

STAGES = ("context_prepare", "chat_execution", "backend_handoff")


def chat_call(name=ToolName.PATIENT_PROFILE, *, identifier="profile_1", args=None):
    return ToolCall(id=identifier, name=name.value if isinstance(name, ToolName) else name,
                    arguments={} if args is None else args)


def chat_final(answer="안녕하세요. 무엇을 도와드릴까요?", *, selected=()):
    return ModelTurn(text=json.dumps({"answer": answer, "selected": list(selected)}, ensure_ascii=False))


class FakeChatDependencies:
    def __init__(self, turns: Sequence[ModelTurn | Exception] = (), *, client=None,
                 handlers=None, fail_at=None, tool_timeout_seconds=30.0, request_timeout_seconds=120.0):
        self.client = FakeLLMClient(turns) if client is None else client
        self.handlers = {
            ToolName.PATIENT_PROFILE: fake_get_patient_profile,
            ToolName.RECENT_CARE_LOGS: fake_get_recent_care_logs,
            ToolName.SEARCH_EVIDENCE: fake_search_evidence,
        }
        if handlers:
            self.handlers.update(handlers)
        self.fail_at = fail_at
        self.events = []
        self.tool_events = []
        self.factory_contexts = []
        self.registries = []
        self.prepared = None
        self.agent_result = None
        self.saved = []
        self.saved_owners = []
        self.receipt = object()
        self.execution = ChatExecutionV1(self.client, self.registry_factory,
            tool_timeout_seconds=tool_timeout_seconds, request_timeout_seconds=request_timeout_seconds)

    def registry_factory(self, context: AgentContext):
        self.factory_contexts.append(context)
        specs = []
        for name, handler in self.handlers.items():
            if context.patient_id is None and TOOL_CONTRACTS[name].patient_scoped:
                continue
            def recorded(context, args, name=name, handler=handler):
                self.tool_events.append((name.value, context, deepcopy(args), len(self.client.requests)))
                return handler(context, args)
            specs.append(ToolSpec(TOOL_CONTRACTS[name], recorded, test_only=True))
        registry = ToolRegistry(tuple(specs), mode="test")
        self.registries.append(registry)
        return registry

    def _enter(self, stage):
        self.events.append(stage)
        if self.fail_at == stage:
            raise RuntimeError("PRIVATE synthetic stage failure")

    def prepare(self, context, user_message):
        self._enter("context_prepare")
        self.prepared = prepare_chat_input(context, user_message)
        return self.prepared

    def run_chat(self, context, prepared_input):
        self._enter("chat_execution")
        self.agent_result = self.execution(context, prepared_input)
        return self.agent_result

    def handoff(self, context, result):
        self._enter("backend_handoff")
        self.saved_owners.append(context.user_id)
        self.saved.append(result)
        return self.receipt

    def workflow(self):
        return ChatWorkflow(context_prepare=self.prepare, chat_execution=self.run_chat,
                            backend_handoff=self.handoff)
