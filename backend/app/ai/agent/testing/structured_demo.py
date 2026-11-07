"""Chat V1/Feed V2 offline demo; model drafts never contain source identities."""
from uuid import UUID

from ..agent_loop_runner import AgentLoopRunner
from ..outputs import ChatAnswerV1, ChatDraft, FeedAnswerV2
from ..registry import ToolName
from ..schemas import AgentContext, ModelTurn, ToolCall
from ..workflows.feed_prompts import FeedPromptBuilder
from .fake_llm import FakeLLMClient
from .fake_tools import fake_registry


def main():
    context = AgentContext(request_id="fake-structured-demo", user_id=UUID(int=1), patient_id=UUID(int=2))
    for output_model in (ChatAnswerV1, FeedAnswerV2):
        turns = []
        if output_model is ChatAnswerV1:
            turns.append(ModelTurn(tool_calls=[ToolCall(id="demo_logs", name=ToolName.RECENT_CARE_LOGS.value, arguments={})]))
        turns.append(ModelTurn(tool_calls=[ToolCall(id="demo_search", name=ToolName.SEARCH_EVIDENCE.value,
                                                  arguments={"query":"synthetic sleep", "top_k":5})]))
        text = (ChatDraft(answer="[FAKE 데모] 합성 근거이며 실제 의료 판단이 아닙니다.", selected=[True]).model_dump_json()
                if output_model is ChatAnswerV1 else
                '{"decisions":[{"include":true,"personal_reason":"[FAKE] 제공된 합성 수면 기록과 관련 있어요.","category":"care"}]}')
        client = FakeLLMClient([*turns, ModelTurn(text=text)])
        result = AgentLoopRunner(client, fake_registry(),
                                 prompt_builder=FeedPromptBuilder() if output_model is FeedAnswerV2 else None).run_structured(
            "[FAKE] 합성 기록과 관련 근거를 보여주세요.", context, output_model=output_model)
        print(result.model_dump_json(indent=2))
        if result.execution.status != "completed":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
