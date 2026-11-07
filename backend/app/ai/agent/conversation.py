"""Request-owned conversation; opaque Provider continuation never enters results.

Prompt builders supply only policy/data messages. The shared Runner commits
validated Provider output and exactly one result per original function call.
Ephemeral Chat/Feed projections are appended only to generation snapshots.
"""
from copy import deepcopy

from .schemas import Message, ModelTurn, ToolOutcome
from .tools.contracts import ToolResultEnvelope


class Conversation:
    def __init__(self):
        self.items: list[Message] = []

    def snapshot(self) -> list[Message]:
        return deepcopy(self.items)

    def append_turn(self, turn: ModelTurn, outcomes: list[ToolOutcome], *,
                    project_search_results: bool = False,
                    evidence_call_ids: set[str] | frozenset[str] = frozenset()) -> None:
        # Provider boundary validates the payload; here validate call/result linkage
        # without interpreting SDK-specific response objects or executing output.
        calls = [item for item in turn.output_items if item.get('type') == 'function_call']
        if ([item.get('call_id') for item in calls] != [call.id for call in turn.tool_calls]
                or [item.get('name') for item in calls] != [call.name for call in turn.tool_calls]
                or [item.call_id for item in outcomes] != [call.id for call in turn.tool_calls]):
            raise ValueError('Invalid conversation call linkage')
        added = [Message(type='provider_output', kind='model', provider_output=deepcopy(item))
                 for item in turn.output_items]
        for outcome in outcomes:
            envelope = ToolResultEnvelope(success=outcome.success, data=outcome.data,
                                          error=outcome.error.model_dump() if outcome.error else None)
            if project_search_results and outcome.success and outcome.call_id in evidence_call_ids:
                envelope.data = {'candidate_list':'See the latest untrusted_chat_candidates message',
                                 'returned_evidence_count':len(outcome.data['evidence'])}
            added.append(Message(type='function_call_output', kind='tool_result', call_id=outcome.call_id,
                                 content=envelope.model_dump_json(exclude_unset=True)))
        self.items.extend(added)
