"""V1 AI 콘텐츠 계약과 서버용 출처 해석. Frontend DTO/DB 계약이 아니다."""
import json
import unicodedata
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

from .schemas import AgentModel, AgentResult
from .tools.contracts import EvidencePackage


def _nonblank(value: str) -> str:
    # 공백/제어/서식 문자만으로는 콘텐츠가 아니다. 결합 문자, private-use 등은
    # 글꼴에 따라 표시될 수 있으므로 제거하거나 정규화하지 않는다.
    if not any(not char.isspace() and unicodedata.category(char) not in {"Cc", "Cf", "Cs", "Cn"}
               for char in value):
        raise ValueError("Text must not be blank")
    return value  # 원문을 보존한다. 길이 검증 전에 임의로 잘라내지 않는다.


ContentText = Annotated[str, StringConstraints(strict=True, min_length=1), AfterValidator(_nonblank)]
SummaryBullet = Annotated[ContentText, Field(max_length=500)]
SummaryBullets = Annotated[list[SummaryBullet], Field(min_length=1, max_length=5)]
Category = Literal["treatment", "care", "prevention", "diagnosis"]


class EvidenceReference(AgentModel):
    """현재 실행에서 성공한 search_evidence 결과의 0-based 위치."""

    tool_call_id: ContentText = Field(max_length=256)
    evidence_index: StrictInt = Field(ge=0)


def _unique_refs(refs: list[EvidenceReference]) -> None:
    keys = [(ref.tool_call_id, ref.evidence_index) for ref in refs]
    if len(keys) != len(set(keys)):
        raise ValueError("Evidence references must be unique")


class ChatAnswerV1(AgentModel):
    schema_version: Literal["1"]
    response_type: Literal["chat"]
    answer: ContentText = Field(max_length=12000)
    citation_refs: list[EvidenceReference] = Field(max_length=20)

    @model_validator(mode="after")
    def check_refs(self):
        _unique_refs(self.citation_refs)
        return self


class FeedContentItemV1(AgentModel):
    source_ref: EvidenceReference
    headline: ContentText = Field(max_length=200)
    summary_bullets: SummaryBullets
    personal_reason: ContentText = Field(max_length=1000)
    category: Category


class FeedAnswerV1(AgentModel):
    schema_version: Literal["1"]
    response_type: Literal["feed"]
    # 0개도 정상: 검색/필터링/개인화 근거 부족은 효과 없음이라는 뜻이 아니다.
    items: list[FeedContentItemV1] = Field(max_length=5)

    @model_validator(mode="after")
    def check_refs(self):
        _unique_refs([item.source_ref for item in self.items])
        return self


class PaperParagraphV1(AgentModel):
    text: ContentText = Field(max_length=2000)


class PaperBodyV1(AgentModel):
    easy: list[PaperParagraphV1] = Field(min_length=1, max_length=10)
    detail: list[PaperParagraphV1] = Field(min_length=1, max_length=10)


class GlossaryEntryV1(AgentModel):
    term: ContentText = Field(max_length=100)
    meaning: ContentText = Field(max_length=500)


class PaperDetailContentV1(AgentModel):
    """보조 계약만 제공. 독립 Workflow/문단별 인용/저장은 구현하지 않는다."""

    schema_version: Literal["1"]
    response_type: Literal["paper_detail"]
    source_ref: EvidenceReference
    summary_bullets: SummaryBullets
    body: PaperBodyV1
    # 제공된 개인화 Context가 없으면 명시적 null. UI DTO 조립 정책은 별도다.
    personal_meaning: ContentText | None = Field(max_length=2000)
    limitations: list[SummaryBullet] = Field(max_length=5)
    glossary: list[GlossaryEntryV1] = Field(max_length=10)


FinalAnswerV1 = Annotated[ChatAnswerV1 | FeedAnswerV1, Field(discriminator="response_type")]
FinalOutputModel = type[ChatAnswerV1] | type[FeedAnswerV1]


class ResolvedEvidence(AgentModel):
    """서버가 원본 Tool 결과에서 복사하는 metadata. LLM 생성 대상이 아니다."""

    source_ref: EvidenceReference
    evidence_type: Literal["new_research", "guideline"]
    title: str
    pmid: str | None
    doi: str | None
    journal: str | None
    publication_year: int | None
    study_type: str | None
    organization: str | None
    source_url: str | None
    full_text_available: bool


class StructuredAgentResult(AgentModel):
    """기존 AgentResult의 wire shape를 바꾸지 않는 opt-in wrapper."""

    model_config = ConfigDict(revalidate_instances="always")

    execution: AgentResult
    output: FinalAnswerV1 | None
    sources: list[ResolvedEvidence]

    @model_validator(mode="before")
    @classmethod
    def revalidate_nested_models(cls, value):
        # 이 wrapper만 서버 검증 경계로 삼는다. 다른 AgentModel의 instance 정책은 유지한다.
        # dict 안의 reference/source instance까지 풀어야 nested validation을 건너뛰지 않는다.
        def payload(item):
            if isinstance(item, BaseModel):
                item = item.model_dump(warnings=False)
            if isinstance(item, Mapping):
                return {key: payload(child) for key, child in item.items()}
            if isinstance(item, (list, tuple)):
                return [payload(child) for child in item]
            return item

        return payload(value)

    @model_validator(mode="after")
    def check_completion(self):
        if self.execution.status != "completed":
            if self.output is not None or self.sources or self.execution.final_answer is not None:
                raise ValueError("Failed execution must not expose an answer or sources")
            return self
        if self.output is None:
            raise ValueError("Completed structured execution requires an output")
        expected = self.output.answer if isinstance(self.output, ChatAnswerV1) else None
        if self.execution.final_answer != expected:
            raise ValueError("Legacy answer must derive from the structured chat answer")
        refs = (self.output.citation_refs if isinstance(self.output, ChatAnswerV1)
                else [item.source_ref for item in self.output.items])
        if [source.source_ref for source in self.sources] != refs:
            raise ValueError("Resolved sources must match output references in order")
        return self


def resolve_evidence_references(
    output: ChatAnswerV1 | FeedAnswerV1 | PaperDetailContentV1,
    evidence_by_call: Mapping[str, EvidencePackage],
) -> list[ResolvedEvidence]:
    """호출자가 제공하는 성공한 검색 snapshot만 사용. 누락/범위 초과는 거부한다.

    Orchestrator는 검증된 성공 search_evidence만 이 mapping에 넣는다.
    직접 사용할 경우에도 동일한 신뢰 경계를 유지해야 한다.
    """
    if type(output) not in (ChatAnswerV1, FeedAnswerV1, PaperDetailContentV1):
        raise ValueError("Unsupported output model")
    output = type(output).model_validate(output.model_dump(warnings=False))
    if isinstance(output, ChatAnswerV1):
        refs = output.citation_refs
    elif isinstance(output, FeedAnswerV1):
        refs = [item.source_ref for item in output.items]
    else:
        refs = [output.source_ref]
    resolved = []
    for ref in refs:
        package = evidence_by_call.get(ref.tool_call_id)
        if package is None:
            raise ValueError("Evidence reference does not match a successful search")
        package = EvidencePackage.model_validate(package.model_dump(warnings=False))
        if ref.evidence_index >= len(package.evidence):
            raise ValueError("Evidence reference is outside the search result")
        item = package.evidence[ref.evidence_index]
        metadata = {name: getattr(item, name) for name in ResolvedEvidence.model_fields
                    if name != "source_ref"}
        resolved.append(ResolvedEvidence(source_ref=ref, **metadata))
    return resolved


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON constant")


def validate_final_output(
    text: str, output_model: FinalOutputModel, evidence_by_call: Mapping[str, EvidencePackage],
) -> tuple[ChatAnswerV1 | FeedAnswerV1, list[ResolvedEvidence]]:
    """단일 JSON object를 검증한다. markdown 추출/자동 수정/Provider 재호출 없음."""
    if output_model not in (ChatAnswerV1, FeedAnswerV1):
        raise ValueError("Unsupported final output model")
    if not isinstance(text, str) or len(text) > 65536:
        raise ValueError("Invalid structured response size")
    payload = json.loads(text, object_pairs_hook=_json_object, parse_constant=_reject_constant)
    output = output_model.model_validate(payload)
    return output, resolve_evidence_references(output, evidence_by_call)
