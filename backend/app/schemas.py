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
    evidence_level: Optional[str] = Field(
        default=None,
        description='근거 등급 "1"~"6" (evidence.py 기준). 비워 보내면 기존 값을 유지한다 — 등급 저장은 save_evidence()',
    )
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


# ------------------------------------------------------------
# 논문 API 응답 (GET /papers, GET /papers/{paper_id}) — 2026-10-07 추가
# 필드 이름은 화면(frontend/src/types/index.ts)에 맞춘다:
#   papers.id → id, published_date → published_at, url → pubmed_url
# 요약(paper_analysis)이 아직 없는 논문은 요약 쪽 필드가 모두 null이다.
# (예시에는 요약이 있는 경우를 적는다 — FastAPI가 Swagger 예시에서 null 값을 지우기 때문)
# ------------------------------------------------------------


class PaperListItem(BaseModel):
    """list_papers()가 반환하는 목록 한 줄. 초록은 뺀 가벼운 형태."""

    id: UUID = Field(description="papers.id")
    title: str
    published_at: Optional[date] = Field(default=None, description="발행일 (papers.published_date)")
    journal: Optional[str] = None
    evidence_level: Optional[str] = Field(default=None, description="근거 등급. 요약 전이면 null")
    summary_finding: Optional[str] = Field(default=None, description="요약 첫 칸. 요약 전이면 null")


class PaperListResponse(BaseModel):
    """list_papers() / GET /papers 응답."""

    total: int = Field(description="전체 논문 수 (limit/offset과 무관)")
    limit: int
    offset: int
    items: list[PaperListItem]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total": 1000,
                "limit": 20,
                "offset": 0,
                "items": [
                    {
                        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                        "title": "Sleep disturbance and caregiver burden in dementia",
                        "published_at": "2026-09-20",
                        "journal": "Alzheimer's & Dementia",
                        "evidence_level": "2",
                        "summary_finding": "수면 문제가 심할수록 보호자 부담이 커졌다.",
                    }
                ],
            }
        }
    )


class PaperDetailResponse(BaseModel):
    """get_paper_detail() / GET /papers/{paper_id} 응답. 논문 + 요약(없으면 null)."""

    id: UUID = Field(description="papers.id")
    title: str
    abstract: Optional[str] = None
    published_at: Optional[date] = Field(default=None, description="발행일 (papers.published_date)")
    journal: Optional[str] = None
    pubmed_url: Optional[str] = Field(default=None, description="원문 링크 (papers.url)")
    doi: Optional[str] = None
    publication_types: list[str] = Field(default_factory=list, description="PubMed PublicationType")
    mesh_terms: list[str] = Field(default_factory=list)
    # --- 아래는 paper_analysis. 요약이 아직 없으면 모두 null ---
    study_type: Optional[str] = None
    evidence_level: Optional[str] = None
    summary_finding: Optional[str] = Field(default=None, description="① 무엇이 새로 밝혀졌나")
    summary_comparison: Optional[str] = Field(default=None, description="② 기존 권고와 비교")
    summary_limitation: Optional[str] = Field(default=None, description="③ 한계")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "title": "Sleep disturbance and caregiver burden in dementia",
                "abstract": "BACKGROUND: ...",
                "published_at": "2026-09-20",
                "journal": "Alzheimer's & Dementia",
                "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
                "doi": "10.1002/alz.12345",
                "publication_types": ["Journal Article", "Randomized Controlled Trial"],
                "mesh_terms": ["Dementia", "Caregivers"],
                "study_type": "rct",
                "evidence_level": "2",
                "summary_finding": "수면 문제가 심할수록 보호자 부담이 커졌다.",
                "summary_comparison": "기존 권고(수면 위생 교육)와 방향이 같다.",
                "summary_limitation": "참여자 수가 적고 추적 기간이 짧다.",
            }
        }
    )


# ------------------------------------------------------------
# 논문 추천 API 응답 (GET /recommendations/{user_id}) — 2026-10-10 추가
# 화면 피드 카드(frontend/src/types/index.ts의 FeedItem)와 뜻이 같은 항목은 화면 이름을 쓰고,
# 나머지는 /papers 응답과 같은 이름을 쓴다 (id, published_at, pubmed_url, summary_* …).
# ------------------------------------------------------------


class RecommendationItem(BaseModel):
    """추천 논문 한 편. 요약(paper_analysis)이 아직 없으면 요약 쪽 필드는 모두 null."""

    id: UUID = Field(description="papers.id — GET /papers/{id}로 상세를 받을 수 있다")
    title: str
    published_at: Optional[date] = Field(default=None, description="발행일 (papers.published_date)")
    journal: Optional[str] = None
    pubmed_url: Optional[str] = Field(default=None, description="원문 링크 (papers.url)")
    relevance_score: float = Field(description="검색어와의 코사인 유사도 (0~1, 클수록 관련)")
    personal_reason: Optional[str] = Field(
        default=None, description="왜 이 사용자에게 관련 있는지 한 줄. 피드 흐름 연결 전이라 항상 null"
    )
    # --- 아래는 paper_analysis. 요약이 아직 없으면 모두 null ---
    study_type: Optional[str] = None
    evidence_level: Optional[str] = None
    summary_finding: Optional[str] = Field(default=None, description="① 무엇이 새로 밝혀졌나")
    summary_comparison: Optional[str] = Field(default=None, description="② 기존 권고와 비교")
    summary_limitation: Optional[str] = Field(default=None, description="③ 한계")


class RecommendationResponse(BaseModel):
    """GET /recommendations/{user_id} 응답."""

    user_id: UUID
    patient_id: UUID = Field(description="추천 기준이 된 환자 (patient_profiles.id)")
    query: str = Field(description="이번 추천에 쓴 검색어 (데모·디버깅용)")
    items: list[RecommendationItem] = Field(description="관련도 높은 순")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "user_id": "593bb66f-148d-481e-9537-00473b147d26",
                "patient_id": "1e89c282-59a8-4036-a006-803e4644dc7a",
                "query": "치매 환자(경도인지장애)의 수면장애, 기억력 저하 증상과 수면 위생, 운동에 도움이 되는 연구",
                "items": [
                    {
                        "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                        "title": "Sleep disturbance and caregiver burden in dementia",
                        "published_at": "2026-09-20",
                        "journal": "Alzheimer's & Dementia",
                        "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/12345678/",
                        "relevance_score": 0.6123,
                        "personal_reason": None,
                        "study_type": "rct",
                        "evidence_level": "2",
                        "summary_finding": "수면 문제가 심할수록 보호자 부담이 커졌다.",
                        "summary_comparison": "기존 권고(수면 위생 교육)와 방향이 같다.",
                        "summary_limitation": "참여자 수가 적고 추적 기간이 짧다.",
                    }
                ],
            }
        }
    )


# ------------------------------------------------------------
# 근거 등급 전용 (paper_analysis의 study_type / evidence_level) — 2026-10-09 추가
# 등급(김현서)과 요약(허웅)이 같은 줄을 쓰기 때문에, 서로의 칸을 지우지 않게 경로를 나눴다.
# ------------------------------------------------------------


class EvidenceIn(BaseModel):
    """save_evidence()가 받는 근거 등급 한 건. (paper_analysis의 두 칸만)"""

    paper_id: UUID = Field(description="papers.id (external_id 아님)")
    study_type: str = Field(
        min_length=1,
        description="연구 유형 (evidence.py의 LEVEL 키: rct, animal, other …). "
        "'판단을 했다'는 표시라서 비울 수 없다",
    )
    evidence_level: Optional[int] = Field(
        default=None, ge=1, le=6,
        description="근거 등급 1(강함)~6(약함). 등급 없음(동물 연구 등)은 None. DB에는 문자열로 저장",
    )


class SaveEvidenceResult(BaseModel):
    """save_evidence()가 반환하는 저장 결과 집계."""

    total: int = Field(description="저장을 시도한 건수 (배치 내 중복 paper_id 제거 후)")
    saved: int = Field(description="실제로 저장/갱신된 건수")
    not_found: list[UUID] = Field(
        default_factory=list, description="papers에 없어서 저장하지 못한 paper_id 목록"
    )


class SaveEmbeddingsResult(BaseModel):
    """save_embeddings()가 반환하는 저장 결과 집계."""

    total: int = Field(description="저장을 시도한 건수 (배치 내 중복 paper_id 제거 후)")
    saved: int = Field(description="실제로 저장/갱신된 건수")
    not_found: list[UUID] = Field(
        default_factory=list,
        description="papers에 없어서 저장하지 못한 paper_id 목록 (이 함수는 논문을 새로 넣지 않는다)",
    )
