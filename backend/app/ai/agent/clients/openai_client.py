"""OpenAI SDK 1.109.1 Responses transport, with no Chat Completions fallback."""
from copy import deepcopy
from time import monotonic

from openai import OpenAI
from pydantic import ValidationError

from .base import GenerationRequest
from .._execution import check_deadline, positive_seconds, request_deadline
from ..error_diagnostics import summarize_validation_error
from ..schemas import Message, ModelTurn, ToolCall

class ProviderResponseError(ValueError):
    def __init__(self, *, stage='provider_response_validation', diagnostic=None):
        self.stage = stage
        self.diagnostic = diagnostic or {}
        super().__init__('Provider response does not satisfy the generation contract')


class OpenAIClient:
    def __init__(self, sdk: OpenAI, *, model: str, timeout_seconds: float = 20.0):
        if not model.strip():
            raise ValueError('Model name is required')
        self.sdk = sdk
        self.model = model
        self.timeout_seconds = positive_seconds(timeout_seconds, 'timeout_seconds')

    @staticmethod
    def _input(item: Message) -> dict:
        if item.type == 'provider_output':
            if not isinstance(item.provider_output, dict):
                raise ProviderResponseError(stage='provider_request_validation')
            return deepcopy(item.provider_output)
        if item.type == 'function_call_output':
            if not item.call_id or not isinstance(item.content, str):
                raise ProviderResponseError(stage='provider_request_validation')
            return {'type': 'function_call_output', 'call_id': item.call_id, 'output': item.content}
        if item.role not in ('system', 'user', 'assistant') or not isinstance(item.content, str):
            raise ProviderResponseError(stage='provider_request_validation')
        return {'role': item.role, 'content': item.content}

    @staticmethod
    def _turn(response) -> ModelTurn:
        reason, field = 'response_not_completed', 'response.status/error/incomplete_details'
        output_index = None
        try:
            if response.status != 'completed' or response.error is not None or response.incomplete_details is not None:
                raise ValueError()
            texts, calls, items = [], [], []
            reason, field = 'invalid_output_items', 'output'
            for output_index, item in enumerate(response.output):
                reason, field = 'invalid_output_item', 'output'
                raw = item.model_dump(exclude_none=True)
                kind = raw.get('type')
                if kind == 'function_call':
                    reason, field = 'invalid_tool_status', 'output.status'
                    if raw.get('status') not in (None, 'completed'):
                        raise ValueError()
                    reason, field = 'invalid_tool_arguments_format', 'output.arguments'
                    if not isinstance(raw.get('arguments'), str):
                        raise ValueError()
                    reason, field = 'empty_tool_call_id', 'output.call_id'
                    if not isinstance(raw.get('call_id'), str) or not raw['call_id'].strip():
                        raise ValueError()
                    reason, field = 'invalid_tool_call', 'output.name'
                    # Output item id is NOT the function-result/source-reference key.
                    calls.append(ToolCall(id=raw['call_id'], name=raw['name'], arguments=raw['arguments']))
                elif kind == 'message':
                    reason, field = 'invalid_assistant_message', 'output.role/status'
                    if raw.get('role') != 'assistant' or raw.get('status') != 'completed':
                        raise ValueError()
                    reason, field = 'invalid_message_content', 'output.content'
                    for part in raw['content']:
                        if part.get('type') != 'output_text' or not isinstance(part.get('text'), str):
                            raise ValueError()  # Includes refusals: never silently complete.
                        texts.append(part['text'])
                elif kind == 'reasoning':
                    reason, field = 'invalid_reasoning_item', 'output.reasoning'
                    if raw.get('status') not in (None, 'completed') or not isinstance(raw.get('summary'), list):
                        raise ValueError()
                else:
                    reason, field = 'unsupported_output_item', 'output.type'
                    raise ValueError()  # Built-in/unknown tools are not authorized here.
                reason, field = 'invalid_output_item_id', 'output.id'
                if (kind != 'function_call' or raw.get('id') is not None) and (
                        not isinstance(raw.get('id'), str) or not raw['id']):
                    raise ValueError()
                items.append(raw)
            reason, field = 'invalid_model_turn', '<root>'
            return ModelTurn(text='\n'.join(texts) if texts else None, tool_calls=calls, output_items=items)
        except Exception as exc:
            diagnostic = dict(reason=reason, field=field, expected='Responses generation contract')
            if isinstance(exc, ValidationError):
                diagnostic = summarize_validation_error(exc, ToolCall if reason == 'invalid_tool_call' else ModelTurn)
            if output_index is not None:
                diagnostic['output_index'] = output_index
            raise ProviderResponseError(diagnostic=diagnostic) from exc

    def generate(self, request: GenerationRequest) -> ModelTurn:
        options = request.options
        kwargs = dict(model=self.model, input=[self._input(item) for item in request.inputs],
                      store=False, include=['reasoning.encrypted_content'])
        if options.tool_choice not in ('auto', 'none', 'required') or (
                options.parallel_tool_calls is not None and type(options.parallel_tool_calls) is not bool):
            raise ValueError('Invalid generation options')
        if options.reasoning_effort is not None:
            if options.reasoning_effort not in ('minimal', 'low', 'medium', 'high'):
                raise ValueError('Invalid reasoning effort')
            kwargs['reasoning'] = {'effort': options.reasoning_effort}
        if request.tools:
            kwargs.update(tools=deepcopy(request.tools),
                          tool_choice=({'type': 'function', 'name': options.required_tool}
                                       if options.required_tool else options.tool_choice))
            if options.parallel_tool_calls is not None:
                kwargs['parallel_tool_calls'] = options.parallel_tool_calls
        elif options.required_tool or options.tool_choice != 'auto':
            raise ValueError('Tool selection requires tools')
        check_deadline()
        deadline = request_deadline.get()
        timeout = self.timeout_seconds if deadline is None else min(self.timeout_seconds, deadline-monotonic())
        if timeout <= 0:
            check_deadline()
        response = self.sdk.with_options(timeout=timeout, max_retries=0).responses.create(**kwargs)
        return self._turn(response)
