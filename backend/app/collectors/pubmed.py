"""PubMed 논문 수집 (박주현 담당).

NCBI E-utilities (esearch + efetch)를 사용해 논문을 수집한다.
주 1회 배치(Celery/cron)로 실행한다. 실제 파싱/저장 로직은 스키마 확정 후 채운다.
"""
import httpx

from app.config import settings

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def fetch_pmids(query: str, retmax: int = 100) -> list[str]:
    """esearch로 검색어에 맞는 PMID 목록을 가져온다."""
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": retmax,
        "retmode": "json",
        "api_key": settings.pubmed_api_key or None,
        "email": settings.pubmed_email or None,
    }
    params = {k: v for k, v in params.items() if v is not None}
    resp = httpx.get(f"{EUTILS_BASE}/esearch.fcgi", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("esearchresult", {}).get("idlist", [])


def collect_and_store(query: str) -> int:
    """검색 → 상세 조회 → papers 테이블 저장까지 수행. 저장 건수를 반환한다.

    TODO: efetch 파싱 후 db.functions를 통해 저장.
    """
    raise NotImplementedError
