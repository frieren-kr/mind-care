"""Chat/Feed V1 오프라인 예시. 합성 데이터만 사용: python -m app.ai.agent.testing.structured_demo."""
from uuid import UUID

from ..agent_loop_runner import AgentLoopRunner
from ..outputs import ChatAnswerV1, EvidenceReference, FeedAnswerV1, FeedContentItemV1
from ..registry import ToolName
from ..schemas import AgentContext, ModelTurn, ToolCall
from .fake_llm import FakeLLMClient
from .fake_tools import fake_registry


def main():
    context = AgentContext(request_id="fake-structured-demo", user_id=UUID(int=1), patient_id=UUID(int=2))
    reference = EvidenceReference(tool_call_id="demo_search", evidence_index=0)
    chat = ChatAnswerV1(schema_version="1", response_type="chat",
        answer="[FAKE 데모] 합성 수면 기록과 관련된 합성 근거입니다. 실제 의료 판단이 아닙니다.",
        citation_refs=[reference])
    feed = FeedAnswerV1(schema_version="1", response_type="feed", items=[FeedContentItemV1(
        source_ref=reference, headline="[FAKE] 수면 변화 관련 합성 연구",
        summary_bullets=["[FAKE] 실제 연구를 설명하지 않는 테스트 콘텐츠입니다."],
        personal_reason="[FAKE] 제공된 합성 수면 기록과 관련 있어요.", category="care")])
    for output in (chat, feed):
        client = FakeLLMClient([
            ModelTurn(tool_calls=[ToolCall(id="demo_logs", name=ToolName.RECENT_CARE_LOGS.value, arguments={})]),
            ModelTurn(tool_calls=[ToolCall(id="demo_search", name=ToolName.SEARCH_EVIDENCE.value,
                                          arguments={"query": "synthetic sleep", "top_k": 5})]),
            ModelTurn(text=output.model_dump_json()),
        ])
        result = AgentLoopRunner(client, fake_registry()).run_structured(
            "[FAKE] 합성 기록과 관련 근거를 보여주세요.", context, output_model=type(output))
        print(result.model_dump_json(indent=2))
        if result.execution.status != "completed":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
