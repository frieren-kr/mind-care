"""Tool 이름/모델 계약과 실행 Registry. Fake는 production 등록을 거부한다."""
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal

from pydantic import BaseModel

from .schemas import AgentContext
from .tools import contracts as c
from .tools.medications import PATIENT_MEDICATIONS_DATA_POLICY
from .tools.medical_visits import PATIENT_MEDICAL_VISITS_DATA_POLICY
from .tools.assessments import PATIENT_ASSESSMENTS_DATA_POLICY
from .tools.patient_profile import PATIENT_PROFILE_DATA_POLICY
from .tools.recent_care_logs import RECENT_CARE_LOGS_DATA_POLICY
from .tools.search_evidence import SEARCH_EVIDENCE_DATA_POLICY
from .tools.safety_events import PATIENT_SAFETY_EVENTS_DATA_POLICY
from .tools.caregiver_profile import CAREGIVER_PROFILE_DATA_POLICY


class ToolName(str, Enum):
    PATIENT_PROFILE = "get_patient_profile"
    RECENT_CARE_LOGS = "get_recent_care_logs"
    SEARCH_EVIDENCE = "search_evidence"
    PATIENT_MEDICATIONS = "get_patient_medications"
    PATIENT_MEDICAL_VISITS = "get_patient_medical_visits"
    PATIENT_ASSESSMENTS = "get_patient_assessments"
    PATIENT_SAFETY_EVENTS = "get_patient_safety_events"
    CAREGIVER_PROFILE = "get_caregiver_profile"


@dataclass(frozen=True)
class ToolContract:
    name: ToolName
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    data_policy: str = ""
    patient_scoped: bool = False

    def definition(self) -> dict[str, Any]:
        return {"type": "function", "strict": False,
            "name": self.name.value,
            "description": self.description,
            "parameters": self.input_model.model_json_schema(),
        }


TOOL_CONTRACTS = {
    contract.name: contract for contract in (
        ToolContract(ToolName.PATIENT_PROFILE,
                     "서버가 선택한 환자의 앱에 저장된 기본 프로필을 읽기 전용으로 조회한다. "
                     "표시 이름·별칭, 등록된 치매 단계, 진단일, 증상 태그 및 관심 치료·관리 분야를 반환한다. "
                     "최근 돌봄 기록, 검사·평가 이력, 복약 상태는 이 Tool의 조회 대상이 아니다.",
                     c.PatientProfileInput, c.PatientProfileOutput,
                     data_policy=PATIENT_PROFILE_DATA_POLICY, patient_scoped=True),
        ToolContract(ToolName.RECENT_CARE_LOGS,
                     "서버가 선택한 환자에 대해 간병인이 앱에 등록한 최근 patient_care 돌봄 기록을 읽기 전용으로 조회한다. "
                     "서버가 정한 종료 시각을 기준으로 지정한 최근 N×24시간 동안의 기록을 확인·요약·비교할 때 사용한다. "
                     "각 기록의 저장된 시각, 관찰 자유서술과 환자 기분 태그, 조회 시작·종료 시각 및 해당 조건의 전체 기록 수를 반환한다. "
                     "반환 기록 수는 limit로 제한되며, 다른 종류의 돌봄 기록이나 공식 진료·검사 결과 자체를 조회하는 Tool은 아니다.",
                     c.RecentCareLogsInput, c.RecentCareLogsOutput,
                     data_policy=RECENT_CARE_LOGS_DATA_POLICY, patient_scoped=True),
        ToolContract(ToolName.SEARCH_EVIDENCE,
                     "연구·의학적 근거가 필요할 때 현재 corpus에서 치매/MCI 및 인지건강 관련 연구 근거 후보를 검색한다. "
                     "관련 의학·연구 용어를 사용한 영어 query를 가능한 한 우선 작성하도록 권장한다. "
                     "한국어 query도 허용하지만 영어보다 우선순위가 낮으며, 영어로 검색 의도를 정확하게 표현하기 어려울 때 사용할 수 있다.",
                     c.EvidenceSearchInput, c.EvidencePackage, data_policy=SEARCH_EVIDENCE_DATA_POLICY),
        ToolContract(ToolName.PATIENT_MEDICATIONS,
                     "서버가 선택한 환자의 등록된 복약 정보를 읽기 전용으로 조회한다. "
                     "약품명, 1회 복용량, 복용 주기, 현재 복용 또는 중단 상태, 복용 시작·종료일 및 관련 자유 메모를 반환한다. "
                     "is_taking은 약물의 등록된 복용 상태로, 개별 날짜의 복용 완료 여부가 아니다. "
                     "note는 다양한 복약 관련 관찰이 포함될 수 있는 자유 서술이다.",
                     c.PatientMedicationsInput, c.PatientMedicationsOutput,
                     data_policy=PATIENT_MEDICATIONS_DATA_POLICY, patient_scoped=True),
        ToolContract(ToolName.PATIENT_MEDICAL_VISITS,
                     "서버가 선택한 환자의 등록된 병원 진료 이력과 미방문·향후 방문 예정 기록을 읽기 전용으로 조회한다. "
                     "방문 날짜, 병원·진료과, 진료 내용, 검사 요약, 약물 변경 메모, 의사 전달사항 및 후속 계획을 확인할 수 있다. "
                     "미방문 상태는 예약 확정이나 결석을 의미하지 않는다.",
                     c.PatientMedicalVisitsInput, c.PatientMedicalVisitsOutput,
                     data_policy=PATIENT_MEDICAL_VISITS_DATA_POLICY, patient_scoped=True),
        ToolContract(ToolName.PATIENT_ASSESSMENTS,
                     "서버가 선택한 환자의 등록된 인지기능·치매 중증도·일상생활 기능·행동·심리 증상 및 우울 관련 "
                     "검사·평가 기록을 읽기 전용으로 조회한다. 검사 종류별 최신 결과 또는 과거 평가 이력을 확인하며, "
                     "검사명, 점수, 평가 날짜, 세부 결과, 평가 주체와 점수 척도 정보를 반환한다. "
                     "등록된 점수와 평가 결과만으로 현재 임상 상태나 질환 진행을 확정하지 않는다.",
                     c.PatientAssessmentsInput, c.PatientAssessmentsOutput,
                     data_policy=PATIENT_ASSESSMENTS_DATA_POLICY, patient_scoped=True),
        ToolContract(ToolName.PATIENT_SAFETY_EVENTS,
                     "서버가 선택한 환자의 앱에 저장된 날짜별 낙상·배회·실종 체크 기록을 읽기 전용으로 조회한다. "
                     "최근 N개 달력 날짜, 몇 개월 전의 달력 월 한 개(0=이번 달), 지정한 연월 중 하나를 선택한다. "
                     "기록 대상 날짜, 세 체크값과 상황 메모 및 실제 조회 기간을 반환하며 한 번에 최대 31개 날짜를 조회한다. "
                     "실시간 위치나 현재 실종·발견·해결 상태를 조회하는 Tool은 아니다.",
                     c.PatientSafetyEventsInput, c.PatientSafetyEventsOutput,
                     data_policy=PATIENT_SAFETY_EVENTS_DATA_POLICY, patient_scoped=True),
        ToolContract(ToolName.CAREGIVER_PROFILE,
                     "현재 인증된 간병인의 앱에 저장된 프로필을 읽기 전용으로 조회한다. "
                     "등록된 관계 문자열, 돌봄 부담·기분 자가점검 점수, "
                     "생활환경 태그 및 마지막 수정 시각을 반환한다. "
                     "관계 문자열은 특정 환자와 연결된 정보가 아니며, "
                     "환자의 상태나 간병인의 현재 임상 진단을 제공하지 않는다.",
                     c.CaregiverProfileInput, c.CaregiverProfileOutput,
                     data_policy=CAREGIVER_PROFILE_DATA_POLICY, patient_scoped=False),
    )
}


@dataclass(frozen=True)
class ToolSpec:
    contract: ToolContract
    handler: Callable[[AgentContext, Any], Any]
    test_only: bool = False
    parallel_safe: bool = False


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
            if type(spec.parallel_safe) is not bool:
                raise ValueError("parallel_safe must be a boolean")
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

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def definitions(self) -> list[dict[str, Any]]:
        return [spec.contract.definition() for spec in self._specs.values()]


def production_registry() -> ToolRegistry:
    """Backing 서비스가 없는 Tool은 현재 운영에 노출하지 않는다."""
    return ToolRegistry()
