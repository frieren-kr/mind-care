"""Request-local Chat assembly over the unchanged shared AgentLoopRunner."""
import re
import json
from typing import Callable

from .._execution import positive_seconds
from ..agent_loop_runner import AgentLoopRunner
from ..clients.base import LLMClient
from ..outputs import ChatAnswerV1, StructuredAgentResult
from ..registry import ToolRegistry
from ..schemas import AgentContext
from .chat_context_prepare import _validated_context
from .chat_prompts import ChatPromptBuilder


# Sentence-attached numeric suffixes are candidates, not sufficient evidence.
# Standalone/list/array/index notation and whitespace-separated markers stay.
_CITATION_SUFFIX = re.compile(r"(?<=[.!?。！？])(?:\[[1-9][0-9]*\])+(?=$|\s|[.!?。！？])")
_PROTECTED_TEXT = re.compile(
    r'```[\s\S]*?(?:```|$)|`[^`]*(?:`|$)|"(?:\\.|[^"\\])*(?:"|$)|'
    r"'(?:\\.|[^'\\])*(?:'|$)|"
    r'“[^”]*(?:”|$)|‘[^’]*(?:’|$)|\$\$[\s\S]*?(?:\$\$|$)|\$[^$\n]*(?:\$|$)|'
    r'\\\([\s\S]*?(?:\\\)|$)|\\\[[\s\S]*?(?:\\\]|$)')
# Keep separate context scans so protection does not depend on regex ordering.
_URL_TEXT = re.compile(r'https?://[^\s<>`"\']+', re.IGNORECASE)
_BLOCKQUOTE_TEXT = re.compile(r'^[ \t]*>[^\n]*(?:\n(?![ \t]*\r?$)[^\n]+)*', re.MULTILINE)
_IDENTIFIER_TEXT = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_./:+#@%~-]*\.(?:\[[1-9][0-9]*\])+')


def _finalize_chat_answer(result: StructuredAgentResult, prepared_input: str) -> StructuredAgentResult:
    """Presentation only, after Runner JSON/citation validation; never repair sources.

    Keep ambiguous text and user-mentioned literals. Numeric suffixes have no
    authority: only the validated citation_refs/sources identify actual evidence.
    """
    if (result.execution.status != 'completed' or not isinstance(result.output, ChatAnswerV1)
            or not result.output.citation_refs):
        return result
    answer = result.output.answer
    current_question = prepared_input
    try:
        payload = json.loads(prepared_input)
    except (ValueError, TypeError):
        pass
    else:
        # Recognize the prepared envelope by its question and server scope flag.
        # Other JSON is a direct question; envelope history never protects markers.
        if (isinstance(payload, dict) and isinstance(payload.get('user_message'), str)
                and type(payload.get('patient_scope_available')) is bool):
            current_question = payload['user_message']
    protected = [match.span() for pattern in (
        _PROTECTED_TEXT, _URL_TEXT, _BLOCKQUOTE_TEXT, _IDENTIFIER_TEXT)
        for match in pattern.finditer(answer)]
    valid_numbers = {str(number) for number in range(1, len(result.output.citation_refs) + 1)}
    def remove(match):
        markers = re.findall(r'\[([1-9][0-9]*)\]', match.group())
        if (any(start < match.end() and match.start() < end for start, end in protected)
                or any(f'[{number}]' in current_question for number in markers)
                or any(number not in valid_numbers for number in markers)):
            return match.group()
        return ''
    cleaned = _CITATION_SUFFIX.sub(remove, answer)
    if cleaned == answer or not cleaned.strip():
        return result
    payload = result.model_dump(warnings=False)
    payload['output']['answer'] = cleaned
    payload['execution']['final_answer'] = cleaned
    return StructuredAgentResult.model_validate(payload)


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
            specs = tuple(supplied.get(name) for name in supplied.names())
            registry = ToolRegistry(specs, mode=supplied.mode)
            if context.patient_id is None and any(spec.contract.patient_scoped for spec in specs):
                raise ValueError("Patient Tools require a selected patient")
            runner = AgentLoopRunner(self.client, registry, prompt_builder=ChatPromptBuilder(),
                max_tool_rounds=5, max_total_tool_calls=10,
                allow_parallel_tools=True, max_parallel_calls=3,
                tool_timeout_seconds=self.tool_timeout_seconds,
                request_timeout_seconds=self.request_timeout_seconds)
            result = runner.run_structured(prepared_input, context, output_model=ChatAnswerV1)
            return _finalize_chat_answer(result, prepared_input)
        except Exception:
            raise ChatExecutionError() from None
