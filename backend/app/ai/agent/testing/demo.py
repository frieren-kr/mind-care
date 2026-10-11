"""DB/API key 없는 명시적 Fake 데모: python -m app.ai.agent.testing.demo."""
from uuid import UUID

from ..agent_loop_runner import AgentLoopRunner
from ..registry import ToolName
from ..schemas import AgentContext, ModelTurn, ToolCall
from .fake_llm import FakeLLMClient
from .fake_tools import fake_registry


def main():
    context = AgentContext(request_id="fake-demo-request", user_id=UUID(int=1), patient_id=UUID(int=2))
    client = FakeLLMClient([
        ModelTurn(tool_calls=[ToolCall(id="demo_1", name=ToolName.RECENT_CARE_LOGS.value, arguments={})]),
        ModelTurn(tool_calls=[ToolCall(id="demo_2", name=ToolName.SEARCH_EVIDENCE.value,
                                      arguments={"query": "dementia sleep disturbance", "top_k": 5})]),
        ModelTurn(text="[FAKE 데모] 합성 돌봄 기록과 합성 근거를 확인했습니다. 실제 의료 판단이 아닙니다."),
    ])
    result = AgentLoopRunner(client, fake_registry()).run("최근 이 환자의 상태를 보고 관련 정보를 알려줘.", context)
    print(result.model_dump_json(indent=2))
    if result.status != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
