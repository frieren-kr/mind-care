"""데이터 접근 함수 (박주현 담당).

★ 팀 규칙 ★
AI/백엔드 팀원은 DB에 직접 SQL을 쓰지 않는다.
반드시 이 모듈의 함수를 통해서만 데이터에 접근한다.
함수 이름 / 입출력(JSON) 형식이 바뀌면 반드시 CLAUDE.md에 기록한다.

테이블 정의는 `app/db/migrations/001_init.sql`이 정본이다.
save_papers()는 구현되어 있고, 나머지 함수는 아직 시그니처/반환 스키마만 확정한
스텁이다. 실제 SQL 구현은 스키마 팀 리뷰 후 채운다.
"""
from typing import Optional
from uuid import UUID

from app.db.connection import get_connection
from app.schemas import AnalysisIn, PaperIn, PaperOut, SavePapersResult, SearchResult


def save_papers(papers: list[PaperIn]) -> SavePapersResult:
    """수집기가 가져온 논문을 papers 테이블에 저장한다.

    (source, external_id) UNIQUE 제약 + ON CONFLICT DO NOTHING 으로
    이미 있는 논문은 자동으로 건너뛴다. 기존 행은 갱신하지 않는다.

    Args:
        papers: 저장할 논문 목록 (schemas.PaperIn). 배치 안에 같은
            (source, external_id)가 여러 번 있으면 첫 건만 남긴다.

    Returns:
        SavePapersResult — 시도 건수 / 신규 저장 건수 / 중복 스킵 건수 +
        새로 저장된 papers.id 목록.
    """
    # 배치 내 중복 제거 (첫 건 우선) — 집계 숫자를 정확하게 맞추기 위함.
    unique: dict[tuple[str, str], PaperIn] = {}
    for paper in papers:
        unique.setdefault((paper.source, paper.external_id), paper)
    rows = list(unique.values())

    if not rows:
        return SavePapersResult(total=0, inserted=0, skipped=0, inserted_ids=[])

    # unnest로 한 번의 INSERT에 배치 전체를 넣는다 (왕복 1회).
    sql = """
        INSERT INTO papers (source, external_id, title, abstract, published_date, url)
        SELECT *
          FROM unnest(
                   %s::text[], %s::text[], %s::text[],
                   %s::text[], %s::date[], %s::text[]
               )
        ON CONFLICT (source, external_id) DO NOTHING
        RETURNING id
    """
    params = (
        [r.source for r in rows],
        [r.external_id for r in rows],
        [r.title for r in rows],
        [r.abstract for r in rows],
        [r.published_date for r in rows],
        [r.url for r in rows],
    )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            inserted_ids = [row[0] for row in cur.fetchall()]

    return SavePapersResult(
        total=len(rows),
        inserted=len(inserted_ids),
        skipped=len(rows) - len(inserted_ids),
        inserted_ids=inserted_ids,
    )


def get_new_papers(
    since: Optional[str] = None,
    limit: int = 100,
    source: Optional[str] = None,
) -> list[PaperOut]:
    """아직 분석(paper_analysis)되지 않은 신규 논문을 반환한다.

    papers LEFT JOIN paper_analysis 에서 분석 결과가 없는 행만 고른다.
    (paper_analysis는 paper_id UNIQUE이므로 논문당 분석은 최대 1건)

    Args:
        since: ISO8601 날짜/시각 문자열. 이 시점 이후 collected_at 분만.
            None이면 전체 미분석분.
        limit: 최대 반환 개수.
        source: 'pubmed' 등 수집처 필터. None이면 전체 수집처.

    Returns:
        PaperOut 리스트. published_date DESC 정렬. (schemas.py 참고)
    """
    raise NotImplementedError


def save_summary(analysis: AnalysisIn) -> UUID:
    """AI 배치가 만든 논문 분석 결과를 저장한다.

    paper_analysis에 upsert 한다 (paper_id UNIQUE → 재분석 시 갱신).
    analysis.embedding이 있으면 paper_embeddings에도 함께 upsert 한다.

    Args:
        analysis: 근거분류(study_type/evidence_level/guideline_relation) +
            구조화 요약(finding/comparison/limitation) + 태그, 선택적 임베딩.
            (schemas.AnalysisIn)

    Returns:
        저장된 paper_analysis row의 id (UUID).
    """
    raise NotImplementedError


def search_similar(
    embedding: list[float],
    top_k: int = 5,
    model_name: Optional[str] = None,
) -> list[SearchResult]:
    """질의 임베딩과 유사한 논문을 pgvector 코사인 거리 기준으로 검색한다.

    paper_embeddings를 검색하고 papers / paper_analysis를 조인해 돌려준다.

    Args:
        embedding: 질의 벡터. 길이는 001_init.sql의 vector(N)과 일치해야 한다.
        top_k: 반환할 개수.
        model_name: 임베딩 모델 필터. 여러 모델이 섞여 있을 때 같은 모델끼리만
            비교하기 위해 쓴다. None이면 전체.

    Returns:
        SearchResult 리스트 (score 내림차순).
    """
    raise NotImplementedError
