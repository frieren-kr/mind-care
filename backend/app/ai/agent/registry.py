"""Tool 이름/모델 계약과 실행 Registry. Fake는 production 등록을 거부한다."""
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal

from pydantic import BaseModel

from .schemas import AgentContext
from .tools import contracts as c


class ToolName(str, Enum):
    PATIENT_PROFILE = "get_patient_profile"
    RECENT_CARE_LOGS = "get_recent_care_logs"
    SEARCH_EVIDENCE = "search_evidence"


@dataclass(frozen=True)
class ToolContract:
    name: ToolName
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]

    def definition(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name.value,
            "description": self.description,
            "parameters": self.input_model.model_json_schema(),
        }}


TOOL_CONTRACTS = {
    contract.name: contract for contract in (
        ToolContract(ToolName.PATIENT_PROFILE,
                     "개인화 context가 필요할 때 trusted server Context가 지정한 환자의 저장된 기본 프로필 snapshot을 읽기 전용으로 조회한다. "
                     "저장된 profile 값만 사용하며 최근 기록·임상 평가·복약 정보를 추론하거나 결합하지 않는다.",
                     c.PatientProfileInput, c.PatientProfileOutput),
        ToolContract(ToolName.RECENT_CARE_LOGS,
                     "개인화 context가 필요할 때 trusted server Context가 지정한 현재 환자에 대해 간병인이 저장한 최근 patient_care 돌봄 기록을 읽기 전용으로 조회한다. "
                     "반환된 기록은 간병인의 관찰·서술이며 임상적으로 검증된 사실이나 진단으로 간주하지 않는다.",
                     c.RecentCareLogsInput, c.RecentCareLogsOutput),
        ToolContract(ToolName.SEARCH_EVIDENCE,
                     "연구·의학적 근거가 필요할 때 현재 corpus에서 치매/MCI 및 인지건강 관련 연구 근거 후보를 검색한다. "
                     "검색 결과나 relevance_score만으로 의료 주장 진위 또는 evidence level을 판단하지 않는다.",
                     c.EvidenceSearchInput, c.EvidencePackage),
    )
}


@dataclass(frozen=True)
class ToolSpec:
    contract: ToolContract
    handler: Callable[[AgentContext, Any], Any]
    test_only: bool = False


class ToolRegistry:
    def __init__(self, specs: tuple[ToolSpec, ...] = (), *, mode: Literal["production", "test"] = "production"):
        if mode not in ("production", "test"):
            raise ValueError("Invalid registry mode")
        self.mode = mode
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            if not isinstance(spec, ToolSpec) or not isinstance(spec.contract, ToolContract):
                raise ValueError("A ToolSpec with a ToolContract is required")
            if not callable(spec.handler):
                raise ValueError("Tool handler must be callable")
            for model in (spec.contract.input_model, spec.contract.output_model):
                if not isinstance(model, type) or not issubclass(model, BaseModel) or model is BaseModel:
                    raise ValueError("Tool models must be concrete Pydantic model classes")
                try:
                    schema = model.model_json_schema()
                    if not isinstance(schema, dict) or schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
                        raise ValueError("Invalid model JSON Schema")
                except Exception:
                    raise ValueError("Tool model must provide a valid JSON Schema") from None
            if not isinstance(spec.contract.name, ToolName):
                raise ValueError("Tool name must match a canonical ToolName")
            canonical = TOOL_CONTRACTS.get(spec.contract.name)
            if canonical is None or spec.contract != canonical:
                raise ValueError("Tool contract must match the canonical contract")
            if spec.test_only and mode != "test":
                raise ValueError("Test-only tools cannot be registered in production")
            name = spec.contract.name.value
            if name in self._specs:
                raise ValueError("Duplicate tool registration")
            self._specs[name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def definitions(self) -> list[dict[str, Any]]:
        return [spec.contract.definition() for spec in self._specs.values()]


def production_registry() -> ToolRegistry:
    """Backing 서비스가 없는 3개 Tool은 현재 운영에 노출하지 않는다."""
    return ToolRegistry()
