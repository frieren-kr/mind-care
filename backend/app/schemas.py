"""데이터 접근 함수의 입출력 스키마 (팀 공용 계약).

테이블 정의는 `app/db/migrations/001_init.sql`이 정본이고,
이 파일은 그 스키마를 파이썬 쪽에서 드러내는 계약이다.
스키마가 바뀌면 반드시 CLAUDE.md의 "데이터 접근 계약" 섹션도 갱신한다.
"""
from datetime import date, datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PaperOut(BaseModel):
    """get_new_papers()가 반환하는 논문 한 건. (papers 테이블)"""

    id: UUID
    source: str = Field(description="pubmed / semantic_scholar / openalex")
    external_id: str = Field(description="PMID 또는 DOI. (source, external_id)가 유니크")
    title: str
    abstract: Optional[str] = None
    published_date: Optional[date] = None
    url: Optional[str] = None
    collected_at: datetime = Field(description="수집 시각")


class AnalysisIn(BaseModel):
    """save_summary()가 받는 논문 분석 결과. (paper_analysis 테이블)

    embedding을 함께 넣으면 paper_embeddings에도 같이 upsert 한다.
    embedding 길이는 001_init.sql의 vector(N)과 일치해야 한다.
    """

    # model_name 필드가 pydantic 보호 네임스페이스 "model_"과 겹쳐서 해제한다.
    model_config = ConfigDict(protected_namespaces=())

    paper_id: UUID = Field(description="papers.id (external_id 아님)")
    study_type: Optional[str] = Field(
        default=None, description="RCT / 체계적 문헌고찰 / 관찰연구 / 사례보고 / 전문가의견"
    )
    evidence_level: Optional[str] = Field(default=None, description="GRADE 유사 등급 (Level I ~ VII)")
    guideline_relation: Optional[str] = Field(default=None, description="기존 가이드라인과 일치/보완/상충")
    summary_finding: Optional[str] = Field(default=None, description="① 무엇이 새로 밝혀졌나")
    summary_comparison: Optional[str] = Field(default=None, description="② 기존 권고와 비교")
    summary_limitation: Optional[str] = Field(default=None, description="③ 한계")
    tags: list[str] = Field(default_factory=list, description="매칭용 태그 (수면장애, 배회 등)")
    model_name: Optional[str] = Field(default=None, description="분석에 쓴 LLM 이름")
    embedding: Optional[list[float]] = Field(
        default=None, description="논문 임베딩. 주면 paper_embeddings에 함께 저장"
    )
    embedding_model: Optional[str] = Field(
        default=None, description="임베딩 모델 이름. embedding을 줄 때 필수"
    )


class SearchResult(BaseModel):
    """search_similar()가 반환하는 검색 결과 한 건.

    paper_embeddings 유사도 검색 결과에 papers / paper_analysis를 조인한 형태.
    paper_id는 chat_messages.cited_paper_ids에 그대로 넣을 수 있다.
    """

    paper_id: UUID
    title: str
    url: Optional[str] = None
    summary_finding: Optional[str] = Field(default=None, description="분석 전이면 None")
    evidence_level: Optional[str] = None
    score: float = Field(description="유사도 점수 (1 - 코사인 거리)")
