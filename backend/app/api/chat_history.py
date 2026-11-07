"""챗봇 이전 대화 Reader (박주현 담당).

웅 님 인터페이스 `ChatHistoryReader.read_history(...) -> ChatHistorySnapshot`
(app/ai/agent/workflows/chat_history_context.py)을 chat_store 조회로 채운다.
db 계층은 app/ai를 import 하지 않으므로 이 변환은 API 쪽에 둔다.

선택·예산·원문/요약 겹침 판단은 Agent(build_chat_history)가 한다. 여기서는 그대로 옮기기만 한다.
조회 실패는 그대로 올린다 → prepare_chat_input이 경고만 남기고 이력 없이 답한다.
"""
from datetime import datetime
from typing import Optional
from uuid import UUID

from app.ai.agent.workflows.chat_history_context import (
    ChatHistorySnapshot, CompletedChatTurn, DailyChatSummary)
from app.db import chat_store


class ChatStoreHistoryReader:
    """상태 없음. 요청마다 read_chat_history() 한 번 (커넥션·트랜잭션 하나)."""

    def read_history(self, *, user_id: UUID, patient_id: Optional[UUID],
                     reference_time: datetime, max_recent_turns: int = 5) -> ChatHistorySnapshot:
        turns, summaries = chat_store.read_chat_history(user_id, patient_id, reference_time, max_recent_turns)
        return ChatHistorySnapshot(
            user_id=user_id,
            patient_id=patient_id,
            recent_turns=[CompletedChatTurn(user_message=t.question, assistant_message=t.answer,
                                            started_at=t.started_at, completed_at=t.completed_at)
                          for t in turns],
            daily_summaries=[DailyChatSummary(summary_date=s.summary_date, summary=s.summary)
                             for s in summaries],
        )
