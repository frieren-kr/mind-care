"""데이터 접근 함수의 입출력 스키마 (팀 공용 계약).

테이블 정의는 `app/db/migrations/001_init.sql`이 정본이고,
이 파일은 그 스키마를 파이썬 쪽에서 드러내는 계약이다.
스키마가 바뀌면 반드시 AGENTS.md의 "데이터 접근 계약" 섹션도 갱신한다.
"""
from datetime import date, datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 임베딩 차원 — DB 스키마와 반드시 같아야 하는 값.
# 정본은 app/db/migrations/003_embedding_dim_1024.sql 의 vector(1024) 이고,
# 이 상수는 DB에 가기 전에 벡터 길이를 걸러내기 위해 둔다 (bge-m3 = 1024차원).
# ★ 차원을 바꿀 때는 새 마이그레이션 + 이 상수 + config.py embedding_dim 을 함께 고친다.
EMBEDDING_DIM = 1024


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
    embedding 길이는 EMBEDDING_DIM(= 1024)과 일치해야 한다.
    ⚠️ 이 임베딩 경로는 아직 실 데이터로 검증되지 않았다 (요약 담당 확인 후 처리 예정).
    임베딩만 저장할 때는 save_embeddings()를 쓴다.
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

    ★ 2026-10-01 변경: paper_id와 점수만 돌려준다.
      이전 초안에는 title / url / summary_finding / evidence_level도 들어 있었지만,
      그 정보는 AI 쪽이 get_papers_by_ids()로 받아 붙이기로 정리했다
      (get_papers_by_ids()는 입력 순서를 그대로 유지하므로 유사도 순서가 보존된다).
      검색 함수는 paper_embeddings만 보고, 논문 본문·요약과 결합하지 않는다.

    paper_id는 chat_messages.cited_paper_ids에 그대로 넣을 수 있다.
    """

    paper_id: UUID
    score: float = Field(
        description="코사인 유사도 (1 - 코사인 거리). 1에 가까울수록 비슷하다"
    )


class PaperIn(BaseModel):
    """save_papers()가 받는 수집된 논문 한 건. (papers 테이블 입력)

    수집기(collectors/)가 만들어서 save_papers()에 넘긴다.
    id / collected_at은 DB가 채우므로 여기에 없다.
    (source, external_id)가 UNIQUE라 같은 논문을 다시 넣어도 중복 저장되지 않는다.
    """

    source: str = Field(description="pubmed / semantic_scholar / openalex")
    external_id: str = Field(description="PMID 또는 DOI")
    title: str
    abstract: Optional[str] = None
    published_date: Optional[date] = None
    url: Optional[str] = None
    # --- 002_add_paper_metadata.sql에서 추가된 컬럼 (2026-09-29) ---
    journal: Optional[str] = Field(default=None, description="학술지명")
    doi: Optional[str] = Field(default=None, description="DOI")
    publication_types: list[str] = Field(
        default_factory=list,
        description="PubMed PublicationType 목록 (RCT / Review 등) — 근거 등급 분류 입력",
    )
    mesh_terms: list[str] = Field(
        default_factory=list, description="MeSH 용어 목록 — 주제 필터링"
    )


class SavePapersResult(BaseModel):
    """save_papers()가 반환하는 저장 결과 집계."""

    total: int = Field(description="저장을 시도한 건수 (입력 개수, 배치 내 중복 제거 후)")
    inserted: int = Field(description="새로 저장된 건수")
    skipped: int = Field(description="이미 있어서 건너뛴 건수 (source, external_id 중복)")
    inserted_ids: list[UUID] = Field(
        default_factory=list, description="새로 저장된 papers.id 목록"
    )


class PaperDetail(BaseModel):
    """get_papers_by_ids()가 반환하는 논문 한 건.

    search_similar()가 돌려준 paper_id로 본문·메타데이터를 마저 받아올 때 쓴다.
    (AI 담당이 근거 등급 분류·주제 필터링에 필요한 필드만 모은 형태)
    """

    paper_id: UUID = Field(description="papers.id — 입력으로 준 id와 같다")
    external_id: str = Field(description="PMID (source='pubmed'인 경우)")
    title: str
    abstract: Optional[str] = None
    journal: Optional[str] = None
    published_date: Optional[date] = None
    publication_types: list[str] = Field(default_factory=list)
    mesh_terms: list[str] = Field(default_factory=list)
    doi: Optional[str] = None


class UpdateMetadataResult(BaseModel):
    """update_paper_metadata()가 반환하는 갱신 결과 집계."""

    total: int = Field(description="갱신을 시도한 건수 (입력 개수, 중복 제거 후)")
    updated: int = Field(description="실제로 갱신된 papers 행 수")
    not_found: list[str] = Field(
        default_factory=list,
        description="papers에 없어서 갱신하지 못한 external_id 목록 (이 함수는 새로 넣지 않는다)",
    )


# ------------------------------------------------------------
# 임베딩 전용 (paper_embeddings) — 2026-10-01 추가
# 요약(paper_analysis)과 임베딩을 따로 돌릴 수 있게 분리한 경로다.
# 이 두 모델을 쓰는 함수는 paper_analysis를 건드리지 않는다.
# ------------------------------------------------------------


class EmbeddingIn(BaseModel):
    """save_embeddings()가 받는 임베딩 한 건. (paper_embeddings 입력)

    논문당 임베딩은 1건만 유지한다 (paper_embeddings.paper_id가 PRIMARY KEY).
    모델 이름은 배치 전체가 같으므로 save_embeddings()의 model_name 인자로 따로 받는다.
    """

    paper_id: UUID = Field(description="papers.id (external_id 아님)")
    embedding: list[float] = Field(description=f"임베딩 벡터. 길이는 반드시 {EMBEDDING_DIM}")

    @field_validator("embedding")
    @classmethod
    def _check_dim(cls, value: list[float]) -> list[float]:
        """DB에 가기 전에 차원을 검사한다.

        다른 모델(예: 1536차원 OpenAI)로 만든 벡터가 섞여 들어오면
        DB는 거부하지만 어떤 논문이 문제인지 알기 어렵다. 여기서 먼저 걸러
        paper_id와 실제 길이를 함께 알려 준다.
        """
        if len(value) != EMBEDDING_DIM:
            raise ValueError(
                f"임베딩 차원이 맞지 않습니다: {len(value)} (기대값 {EMBEDDING_DIM}). "
                "bge-m3가 아닌 모델의 벡터가 섞이지 않았는지 확인하세요."
            )
        return value


class SaveEmbeddingsResult(BaseModel):
    """save_embeddings()가 반환하는 저장 결과 집계."""

    total: int = Field(description="저장을 시도한 건수 (배치 내 중복 paper_id 제거 후)")
    saved: int = Field(description="실제로 저장/갱신된 건수")
    not_found: list[UUID] = Field(
        default_factory=list,
        description="papers에 없어서 저장하지 못한 paper_id 목록 (이 함수는 논문을 새로 넣지 않는다)",
    )
