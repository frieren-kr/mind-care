"""데이터 접근 함수의 입출력 스키마 (팀 공용 계약).

이 스키마가 바뀌면 반드시 CLAUDE.md의 "데이터 접근 계약" 섹션도 갱신한다.
"""
from typing import Optional

from pydantic import BaseModel, Field


class PaperOut(BaseModel):
    """get_new_papers()가 반환하는 논문 한 건."""

    id: int
    pmid: str = Field(description="PubMed ID")
    title: str
    abstract: Optional[str] = None
    journal: Optional[str] = None
    published_at: Optional[str] = Field(default=None, description="ISO8601 날짜")
    collected_at: str = Field(description="수집 시각 ISO8601")


class SummaryIn(BaseModel):
    """save_summary()가 받는 요약/분류 결과."""

    paper_id: int = Field(description="papers 테이블의 id (pmid 아님)")
    is_relevant: bool = Field(description="간병인 대상 관련성 여부")
    evidence_grade: str = Field(description="근거 등급 (예: A/B/C 또는 study type)")
    summary_text: str = Field(description="쉬운 말로 요약한 본문")
    embedding: list[float] = Field(description="요약문 임베딩 (길이=embedding_dim)")


class SearchResult(BaseModel):
    """search_similar()가 반환하는 검색 결과 한 건."""

    summary_id: int
    paper_id: int
    summary_text: str
    score: float = Field(description="유사도 점수 (1 - 코사인 거리)")
