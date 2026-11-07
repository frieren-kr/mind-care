"""Validate the current question, then optionally enrich with read-only history."""
import logging
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Annotated

from pydantic import Field, TypeAdapter

from ..outputs import ContentText
from ..schemas import AgentContext
from .._execution import bounded_call, positive_seconds
from .chat_history_context import (ChatHistoryReader, MAX_RECENT_TURNS, build_chat_history,
                                   serialize_chat_data)

logger = logging.getLogger(__name__)

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


def prepare_chat_input(context: AgentContext, user_message: str, *,
                       history_reader: ChatHistoryReader | None = None,
                       reference_time: datetime | None = None,
                       count_tokens: Callable[[str], int] | None = None,
                       model: str | None = None,
                       history_timeout_seconds: float = 2.0) -> str:
    """Preserve the question and scope flag; optionally supply bounded history.

    Backend owns auth and history retrieval. Optional history failures degrade
    to the current question; only input validation may produce a Stage 1 error.
    """
    try:
        validated = _validated_context(context)
        message = _USER_MESSAGE.validate_python(user_message)
    except Exception:
        raise ChatContextPrepareError() from None
    value = {"user_message": message, "patient_scope_available": validated.patient_id is not None}
    if history_reader is not None:
        try:
            now = reference_time if reference_time is not None else datetime.now(timezone.utc)
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError('History reference must be timezone-aware')
            def load():
                snapshot = history_reader.read_history(user_id=validated.user_id, patient_id=validated.patient_id,
                    reference_time=now, max_recent_turns=MAX_RECENT_TURNS)
                return build_chat_history(snapshot, user_id=validated.user_id, patient_id=validated.patient_id,
                    reference_time=now, count_tokens=count_tokens, model=model)
            value['previous_chat_context'] = bounded_call(load,
                timeout_seconds=positive_seconds(history_timeout_seconds, 'history_timeout_seconds'))
        except Exception:
            # Deliberately no exception text, raw content, patient IDs or API 422.
            logger.warning('Chat history unavailable; proceeding with the current question only')
    return serialize_chat_data(value)
