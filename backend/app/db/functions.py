"""데이터 접근 함수 (박주현 담당).

★ 팀 규칙 ★
AI/백엔드 팀원은 DB에 직접 SQL을 쓰지 않는다.
반드시 이 모듈의 함수를 통해서만 데이터에 접근한다.
함수 이름 / 입출력(JSON) 형식이 바뀌면 반드시 CLAUDE.md에 기록한다.

아래 함수들은 현재 시그니처와 반환 스키마를 확정하기 위한 스텁이다.
실제 SQL 구현은 스키마 확정 후 채운다.
"""
from typing import Optional

from app.schemas import PaperOut, SummaryIn, SearchResult


def get_new_papers(since: Optional[str] = None, limit: int = 100) -> list[PaperOut]:
    """아직 처리(요약)되지 않은 신규 논문을 반환한다.

    Args:
        since: ISO8601 날짜 문자열. 이 시점 이후 수집분만. None이면 전체 미처리분.
        limit: 최대 반환 개수.

    Returns:
        PaperOut 리스트. (schemas.py 참고)
    """
    raise NotImplementedError


def save_summary(summary: SummaryIn) -> int:
    """AI 파이프라인이 만든 요약/분류 결과를 저장한다.

    Args:
        summary: 논문 id, 관련성, 근거등급, 요약문, 임베딩 등. (schemas.SummaryIn)

    Returns:
        저장된 summary row의 id.
    """
    raise NotImplementedError


def search_similar(embedding: list[float], top_k: int = 5) -> list[SearchResult]:
    """질의 임베딩과 유사한 요약을 pgvector 코사인 거리 기준으로 검색한다.

    Args:
        embedding: 질의 벡터. 길이는 settings.embedding_dim과 일치해야 한다.
        top_k: 반환할 개수.

    Returns:
        SearchResult 리스트 (유사도 점수 포함).
    """
    raise NotImplementedError
