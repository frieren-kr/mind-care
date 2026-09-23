"""데이터 접근 함수 (박주현 담당).

★ 팀 규칙 ★
AI/백엔드 팀원은 DB에 직접 SQL을 쓰지 않는다.
반드시 이 모듈의 함수를 통해서만 데이터에 접근한다.
함수 이름 / 입출력(JSON) 형식이 바뀌면 반드시 CLAUDE.md에 기록한다.

테이블 정의는 `app/db/migrations/001_init.sql`이 정본이다.
아래 함수들은 현재 시그니처와 반환 스키마를 확정하기 위한 스텁이다.
실제 SQL 구현은 스키마 팀 리뷰 후 채운다.
"""
from typing import Optional
from uuid import UUID

from app.schemas import AnalysisIn, PaperOut, SearchResult


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
