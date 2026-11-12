"""Request-scoped history selection. No SQL, persistence or LLM summarization."""
import json
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Protocol
from uuid import UUID

import tiktoken

from pydantic import ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

from ..schemas import AgentModel

MAX_RECENT_TURNS = 5
RECENT_TOKEN_LIMIT = 5500
SUMMARY_TOKEN_LIMIT = 1500
MAX_EXCERPT_CHARACTERS = 600
KST = timezone(timedelta(hours=9))
logger = logging.getLogger(__name__)


class ChatHistorySource(AgentModel):
    model_config = ConfigDict(revalidate_instances='always')
    citation_number: StrictInt = Field(ge=1)
    title: StrictStr = Field(min_length=1, max_length=1000)
    pmid: StrictStr | None = None
    evidence_type: Literal['new_research', 'guideline']

    @field_validator('title')
    @classmethod
    def nonblank_title(cls, value):
        if not value.strip():
            raise ValueError('Empty source title')
        return value

    @field_validator('pmid')
    @classmethod
    def valid_pmid(cls, value):
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError('Invalid PMID')
        return value

    @model_validator(mode='after')
    def source_type(self):
        if self.evidence_type == 'guideline' and self.pmid is not None:
            raise ValueError('Guideline must not have PMID')
        return self


class CompletedChatTurn(AgentModel):
    model_config = ConfigDict(revalidate_instances='always')
    user_message: StrictStr = Field(min_length=1)
    assistant_message: StrictStr = Field(min_length=1)
    completed_at: datetime
    # Optional server provenance for turns spanning KST midnight. Without it,
    # raw remains usable but summary overlap cannot be proven safe.
    started_at: datetime | None = None
    sources: list[ChatHistorySource] = Field(default_factory=list, max_length=5)

    @field_validator('user_message', 'assistant_message')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Empty history message')
        return value

    @field_validator('completed_at', 'started_at')
    @classmethod
    def aware(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError('History requires aware completion times')
        return value

    @model_validator(mode='after')
    def ordered_times(self):
        if self.started_at is not None and self.started_at > self.completed_at:
            raise ValueError('Turn starts after completion')
        numbers = [source.citation_number for source in self.sources]
        if any(current <= previous for previous, current in zip(numbers, numbers[1:])):
            raise ValueError('History source numbers must be unique and ascending')
        return self


class DailyChatSummary(AgentModel):
    summary_date: date
    summary: StrictStr = Field(min_length=1)

    @field_validator('summary')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('Empty daily summary')
        return value


class ChatHistorySnapshot(AgentModel):
    # Server-only provenance. Never serialize the snapshot into Provider input.
    user_id: UUID
    patient_id: UUID | None
    recent_turns: list[CompletedChatTurn]
    daily_summaries: list[DailyChatSummary]


class ChatHistoryReader(Protocol):
    def read_history(self, *, user_id: UUID, patient_id: UUID | None,
                     reference_time: datetime, max_recent_turns: int) -> ChatHistorySnapshot:
        """One authorized snapshot; complete pairs only, newest first, no writes."""
        ...


def serialize_chat_data(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def utf8_token_upper_bound(serialized: str) -> int:
    """Conservative byte-BPE bound, NOT measured model tokens or Provider usage."""
    return len(serialized.encode('utf-8'))


def chat_history_token_counter(model: str | None = None) -> Callable[[str], int]:
    """Use the execution model's tiktoken mapping, with explicit fallbacks.

    Backend's default Chat execution uses agent_model, then summary_model.
    Custom executions must pass their model explicitly (or supply a counter).
    Encoding assets should be cached during environment setup, not live traffic.
    """
    if model is None:
        from app.config import settings
        model = settings.openai_agent_model.strip() or settings.openai_summary_model.strip()
    model = model.strip()
    if not model:
        logger.warning('Chat history tokenizer: model_unconfigured; using UTF-8 upper bound')
        return utf8_token_upper_bound
    try:
        encoding_name = tiktoken.encoding_name_for_model(model)
    except KeyError:
        logger.warning('Chat history tokenizer: model_unsupported; using UTF-8 upper bound')
        return utf8_token_upper_bound
    try:
        encoding = tiktoken.get_encoding(encoding_name)
    except Exception:
        logger.warning('Chat history tokenizer: encoding_unavailable; using UTF-8 upper bound')
        return utf8_token_upper_bound
    # Special-token-looking history strings are ordinary untrusted text.
    return lambda serialized: len(encoding.encode_ordinary(serialized))


def _history(recent, summaries):
    return {'recent_turns': recent, 'daily_summaries': summaries}


def _tokens(recent, summaries, counter):
    # Charge all keys, date/role fields, representation labels and JSON syntax.
    value = counter(serialize_chat_data({'previous_chat_context': _history(recent, summaries)}))
    if type(value) is not int or value < 1:
        raise ValueError('Invalid history token count')
    return value


def _excerpt(text):
    if len(text) <= MAX_EXCERPT_CHARACTERS:
        return text
    # Only complete leading paragraphs. No sentence splitting (abbreviations,
    # decimals, qualifiers), rephrasing, clinical ranking or arbitrary slicing.
    paragraphs = text.split('\n\n')
    selected = []
    for paragraph in paragraphs:
        if (not paragraph.strip() or not paragraph.rstrip().endswith(('.', '!', '?', '。', '！', '？'))
                or len('\n\n'.join(selected + [paragraph])) > MAX_EXCERPT_CHARACTERS):
            break
        selected.append(paragraph)
    return '\n\n'.join(selected) or None


def build_chat_history(snapshot: ChatHistorySnapshot, *, user_id: UUID, patient_id: UUID | None,
                       reference_time: datetime,
                       count_tokens: Callable[[str], int] | None = None,
                       model: str | None = None) -> dict:
    """Prefer newest turns, stop on budget overflow, disclose chronologically."""
    if count_tokens is None:
        count_tokens = chat_history_token_counter(model)
    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise ValueError('History reference must be timezone-aware')
    raw = snapshot.model_dump(warnings=False) if isinstance(snapshot, ChatHistorySnapshot) else snapshot
    snapshot = ChatHistorySnapshot.model_validate(raw)
    if snapshot.user_id != user_id or snapshot.patient_id != patient_id:
        raise ValueError('History scope mismatch')
    if len(snapshot.recent_turns) > MAX_RECENT_TURNS:
        raise ValueError('History reader exceeded turn limit')
    turns = sorted(snapshot.recent_turns, key=lambda t: t.completed_at, reverse=True)
    if any(t.completed_at >= reference_time for t in turns):
        raise ValueError('History contains a current or future turn')
    identities = [(t.completed_at, t.user_message, t.assistant_message) for t in turns]
    if len(identities) != len(set(identities)):
        raise ValueError('Duplicate completed turn')
    dates = [s.summary_date for s in snapshot.daily_summaries]
    if len(dates) != len(set(dates)):
        raise ValueError('Duplicate daily summary')
    # Raw wins for the entire KST date, including raw dropped by budget/extraction.
    # Never mix a partial date's raw turns with that date's aggregate summary.
    unknown_start = any(t.started_at is None for t in turns)
    raw_ranges = [(t.started_at.astimezone(KST).date(), t.completed_at.astimezone(KST).date())
                  for t in turns if t.started_at is not None]
    today = reference_time.astimezone(KST).date()
    if any(day >= today for day in dates):
        raise ValueError('Daily summary must cover a completed KST day')
    recent = []
    for index, turn in enumerate(turns):
        question, answer = turn.user_message, turn.assistant_message
        if index >= 2:
            question, answer = _excerpt(question), _excerpt(answer)
            if question is None or answer is None:
                continue
        row = {'completed_at': turn.completed_at.isoformat(),
               'representation': 'original' if index < 2 else 'verbatim_excerpt',
               'messages': [{'role': 'user', 'content': question}, {'role': 'assistant', 'content': answer}]}
        if turn.started_at is not None:
            row['started_at'] = turn.started_at.isoformat()
        if turn.sources:
            row['sources'] = [source.model_dump() for source in turn.sources]
        proposed = [row] + recent
        if turn.sources and _tokens(proposed, [], count_tokens) > RECENT_TOKEN_LIMIT:
            # Identification metadata is optional; preserve the conversation first.
            del row['sources']
        if _tokens(proposed, [], count_tokens) > RECENT_TOKEN_LIMIT:
            proposed = [{key: value for key, value in item.items() if key != 'sources'} for item in proposed]
            if _tokens(proposed, [], count_tokens) > RECENT_TOKEN_LIMIT:
                break
        recent = proposed
    summaries = []
    for summary in sorted(snapshot.daily_summaries, key=lambda s: s.summary_date, reverse=True):
        if unknown_start or any(start <= summary.summary_date <= end for start, end in raw_ranges):
            continue
        row = {'summary_date': summary.summary_date.isoformat(), 'summary': summary.summary}
        proposed = [row] + summaries
        if (_tokens([], proposed, count_tokens) > SUMMARY_TOKEN_LIMIT
                or _tokens(recent, proposed, count_tokens) > RECENT_TOKEN_LIMIT + SUMMARY_TOKEN_LIMIT):
            break
        summaries = proposed
    if (_tokens(recent, [], count_tokens) > RECENT_TOKEN_LIMIT
            or _tokens([], summaries, count_tokens) > SUMMARY_TOKEN_LIMIT
            or _tokens(recent, summaries, count_tokens) > RECENT_TOKEN_LIMIT + SUMMARY_TOKEN_LIMIT):
        raise ValueError('Serialized history exceeds budget')
    return _history(recent, summaries)
