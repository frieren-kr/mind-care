"""Minimal Chat input preparation; no reads, history, auth or Tool selection."""
import json
from typing import Annotated

from pydantic import Field, TypeAdapter

from ..outputs import ContentText
from ..schemas import AgentContext

MAX_CHAT_MESSAGE_LENGTH = 4000
_USER_MESSAGE = TypeAdapter(Annotated[ContentText, Field(max_length=MAX_CHAT_MESSAGE_LENGTH)])


class ChatContextPrepareError(RuntimeError):
    def __init__(self):
        super().__init__("Chat context preparation failed")


def _validated_context(context: AgentContext) -> AgentContext:
    # Revalidate model_construct instances too. UUID consistency is not auth.
    if not isinstance(context, AgentContext):
        raise TypeError("Trusted AgentContext required")
    # model_copy(update=...) can insert undeclared fields that model_dump omits.
    if set(vars(context)) - set(AgentContext.model_fields):
        raise ValueError("Unexpected Context fields")
    return AgentContext.model_validate(context.model_dump(warnings=False))


def prepare_chat_input(context: AgentContext, user_message: str) -> str:
    """Preserve the question; disclose only a server-computed scope presence flag.

    Backend owns authentication and patient ownership. No context/profile reads
    or classification occur here. User-authored PII is not sanitized.
    """
    try:
        validated = _validated_context(context)
        message = _USER_MESSAGE.validate_python(user_message)
        return json.dumps({"user_message": message,
                           "patient_scope_available": validated.patient_id is not None},
                          ensure_ascii=False, allow_nan=False)
    except Exception:
        raise ChatContextPrepareError() from None
