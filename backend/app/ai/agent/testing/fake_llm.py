"""Scripted client for the explicit generation protocol; no Provider or I/O."""
from collections import deque
from copy import deepcopy
import json
from typing import Sequence

from ..clients.base import GenerationRequest
from ..schemas import ModelTurn


def fake_output(turn):
    """Build native continuation fixtures for domain-only scripted turns."""
    if not isinstance(turn, ModelTurn) or turn.output_items:
        return turn
    items = []
    if turn.text is not None:
        items.append({'type': 'message', 'id': 'msg_fake', 'role': 'assistant', 'status': 'completed',
                      'content': [{'type': 'output_text', 'text': turn.text, 'annotations': []}]})
    for call in turn.tool_calls:
        items.append({'type': 'function_call', 'id': 'item_'+call.id, 'call_id': call.id,
                      'name': call.name, 'arguments': call.arguments if isinstance(call.arguments, str)
                      else json.dumps(call.arguments, ensure_ascii=False), 'status': 'completed'})
    return turn.model_copy(update={'output_items': items})


class FakeLLMClient:
    def __init__(self, turns: Sequence[ModelTurn | Exception]):
        self.turns = deque(deepcopy(turns))
        self.options = []
        self.requests = []

    def generate(self, request: GenerationRequest) -> ModelTurn:
        self.options.append(request.options)
        self.requests.append((deepcopy(list(request.inputs)), deepcopy(request.tools)))
        if not self.turns:
            raise RuntimeError('FakeLLM script exhausted')
        turn = self.turns.popleft()
        if isinstance(turn, Exception):
            raise turn
        return fake_output(turn)
