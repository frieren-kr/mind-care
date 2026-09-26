"""PubMed 논문 수집 (박주현 담당).

NCBI E-utilities를 두 단계로 호출한다:
  1. esearch — 검색어 + 최근 N일 조건으로 PMID 목록을 받는다.
  2. efetch  — PMID 묶음의 상세 정보(XML)를 받아 papers 스키마에 맞게 파싱한다.

주 1회 배치(Celery/cron)로 `collect_and_store()`를 실행한다.
저장은 반드시 `app.db.functions.save_papers()`를 통해서만 한다 (팀 규칙: raw SQL 금지).

단독 실행:
    python -m app.collectors.pubmed --days-back 7 --max-results 20 --dry-run
"""
import argparse
import logging
import time
from datetime import date
from typing import Optional
from xml.etree import ElementTree as ET

import httpx

from app.config import settings
from app.db.functions import save_papers
from app.schemas import PaperIn, SavePapersResult

logger = logging.getLogger(__name__)

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
SOURCE = "pubmed"

# ------------------------------------------------------------------
# 검색어 — 수집 단계에서 치매/MCI 관련성을 보장한다.
# (CLAUDE.md 결정사항 2026-09-23: 전역 관련성은 이 단계에서, 사용자별 관련도는
#  feed_items.relevance_score에서 판단한다.)
# 검색어를 바꿀 때는 이 상수만 고친다.
# ------------------------------------------------------------------
SEARCH_QUERY = (
    '("Dementia"[MeSH] OR "Cognitive Dysfunction"[MeSH]) '
    'AND ("caregivers"[MeSH] OR "caregiver burden" OR "sleep disorders" '
    'OR "wandering behavior" OR "agitation")'
)

# NCBI 예절: tool/email을 함께 보내면 문제가 생겼을 때 NCBI가 연락해 준다.
TOOL_NAME = "mind-care"
EFETCH_BATCH_SIZE = 100          # efetch 한 번에 요청할 PMID 개수
REQUEST_TIMEOUT = 30.0

# 요청 제한: API 키가 없으면 초당 3회, 있으면 초당 10회.
_INTERVAL_WITHOUT_KEY = 1 / 3
_INTERVAL_WITH_KEY = 1 / 10
_last_request_at = 0.0

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _throttle() -> None:
    """NCBI 초당 요청 제한을 넘지 않도록 직전 요청과의 간격을 맞춘다."""
    global _last_request_at
    interval = _INTERVAL_WITH_KEY if settings.pubmed_api_key else _INTERVAL_WITHOUT_KEY
    wait = _last_request_at + interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _common_params() -> dict[str, str]:
    """모든 E-utilities 요청에 공통으로 붙는 파라미터.

    api_key는 .env에 있으면 쓰고, 없으면 아예 보내지 않는다 (키 없이도 동작).
    """
    params = {"db": "pubmed", "tool": TOOL_NAME}
    if settings.pubmed_api_key:
        params["api_key"] = settings.pubmed_api_key
    if settings.pubmed_email:
        params["email"] = settings.pubmed_email
    return params


def fetch_pmids(
    query: str = SEARCH_QUERY,
    retmax: int = 100,
    days_back: Optional[int] = None,
) -> list[str]:
    """esearch로 검색어에 맞는 PMID 목록을 가져온다.

    Args:
        query: PubMed 검색식. 기본값은 SEARCH_QUERY.
        retmax: 최대 PMID 개수.
        days_back: 지정하면 최근 N일 내 '발행(pdat)'된 논문으로 제한한다.

    Returns:
        PMID 문자열 목록 (최신순).
    """
    params = _common_params() | {
        "term": query,
        "retmax": str(retmax),
        "retmode": "json",
        "sort": "date",
    }
    if days_back is not None:
        params["datetype"] = "pdat"      # 발행일 기준
        params["reldate"] = str(days_back)

    _throttle()
    resp = httpx.get(f"{EUTILS_BASE}/esearch.fcgi", params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    result = resp.json().get("esearchresult", {})
    pmids = result.get("idlist", [])
    logger.info(
        "esearch: 검색 결과 %s건 중 PMID %d개 수신 (최근 %s일)",
        result.get("count", "?"),
        len(pmids),
        days_back if days_back is not None else "전체",
    )
    return pmids


def _fetch_articles_xml(pmids: list[str]) -> ET.Element:
    """efetch로 PMID 묶음의 상세 정보(XML)를 받아 루트 엘리먼트를 반환한다."""
    data = _common_params() | {
        "id": ",".join(pmids),
        "retmode": "xml",
    }
    _throttle()
    # PMID가 많을 수 있어 NCBI 권장대로 POST로 보낸다.
    resp = httpx.post(f"{EUTILS_BASE}/efetch.fcgi", data=data, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return ET.fromstring(resp.content)


def _text(element: Optional[ET.Element]) -> str:
    """엘리먼트 안의 모든 텍스트를 이어붙인다 (인라인 태그가 섞여 있어도 안전하게)."""
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def _parse_abstract(article: ET.Element) -> Optional[str]:
    """초록을 뽑는다. 구조화 초록(Label 있음)이면 'LABEL: 내용' 형태로 합친다."""
    parts: list[str] = []
    for node in article.findall(".//Abstract/AbstractText"):
        body = _text(node)
        if not body:
            continue
        label = node.get("Label")
        parts.append(f"{label}: {body}" if label else body)
    return "\n\n".join(parts) or None


def _parse_published_date(article: ET.Element) -> Optional[date]:
    """발행일을 뽑는다. ArticleDate(전자판)를 우선하고, 없으면 Journal PubDate를 쓴다.

    PubMed 날짜는 연도만 있거나 MedlineDate(예: "2024 Nov-Dec") 자유형식인 경우가
    있어 없는 항목은 1로 채운다. 연도를 아예 못 찾으면 None.
    """
    candidates = article.findall(".//Article/ArticleDate")
    candidates += article.findall(".//Article/Journal/JournalIssue/PubDate")

    for node in candidates:
        year_text = _text(node.find("Year"))
        if not year_text:
            # MedlineDate 자유형식 → 앞 4자리를 연도로 본다.
            year_text = _text(node.find("MedlineDate"))[:4]
        if not year_text.isdigit():
            continue

        month_text = _text(node.find("Month")).lower()
        month = _MONTHS.get(month_text[:3]) or (int(month_text) if month_text.isdigit() else 1)
        day_text = _text(node.find("Day"))
        day = int(day_text) if day_text.isdigit() else 1
        try:
            return date(int(year_text), month, day)
        except ValueError:
            continue
    return None


def _parse_article(article: ET.Element) -> Optional[PaperIn]:
    """PubmedArticle 하나를 PaperIn으로 바꾼다. PMID/제목/초록이 없으면 None."""
    pmid = _text(article.find(".//MedlineCitation/PMID"))
    title = _text(article.find(".//Article/ArticleTitle"))
    abstract = _parse_abstract(article)

    if not pmid or not title:
        logger.debug("PMID/제목이 없어 건너뜀 (pmid=%r, title=%r)", pmid, title)
        return None
    if not abstract:
        logger.debug("초록이 없어 건너뜀 (PMID %s)", pmid)
        return None

    return PaperIn(
        source=SOURCE,
        external_id=pmid,
        title=title,
        abstract=abstract,
        published_date=_parse_published_date(article),
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
    )


def fetch_papers(days_back: int = 7, max_results: int = 100) -> list[PaperIn]:
    """최근 N일 내 발행된 치매·MCI 관련 논문을 수집한다. (저장은 하지 않는다)

    Args:
        days_back: 최근 며칠 내 발행분을 볼지.
        max_results: 최대 수집 개수.

    Returns:
        PaperIn 목록. 초록이 없는 논문은 제외된다.
    """
    pmids = fetch_pmids(SEARCH_QUERY, retmax=max_results, days_back=days_back)
    if not pmids:
        logger.info("수집 결과 없음 (최근 %d일)", days_back)
        return []

    papers: list[PaperIn] = []
    for start in range(0, len(pmids), EFETCH_BATCH_SIZE):
        batch = pmids[start:start + EFETCH_BATCH_SIZE]
        root = _fetch_articles_xml(batch)
        for article in root.findall(".//PubmedArticle"):
            paper = _parse_article(article)
            if paper is not None:
                papers.append(paper)
        logger.info("efetch: %d개 요청 → 누적 %d건 파싱", len(batch), len(papers))

    logger.info(
        "수집 완료: PMID %d개 중 %d건 파싱 (초록 없음 등 %d건 제외)",
        len(pmids),
        len(papers),
        len(pmids) - len(papers),
    )
    return papers


def collect_and_store(days_back: int = 7, max_results: int = 100) -> SavePapersResult:
    """수집 → 저장까지 수행한다. 주 1회 배치의 진입점.

    Args:
        days_back: 최근 며칠 내 발행분을 볼지.
        max_results: 최대 수집 개수.

    Returns:
        SavePapersResult — 시도 / 신규 저장 / 중복 스킵 건수.
    """
    papers = fetch_papers(days_back=days_back, max_results=max_results)
    result = save_papers(papers)
    logger.info(
        "저장 완료: 수집 %d건, 신규 저장 %d건, 중복 스킵 %d건",
        result.total,
        result.inserted,
        result.skipped,
    )
    return result


def main() -> None:
    """단독 실행용. --dry-run이면 API 호출까지만 하고 DB에 저장하지 않는다."""
    parser = argparse.ArgumentParser(description="PubMed 논문 수집")
    parser.add_argument("--days-back", type=int, default=7, help="최근 N일 내 발행분 (기본 7)")
    parser.add_argument("--max-results", type=int, default=100, help="최대 수집 개수 (기본 100)")
    parser.add_argument("--dry-run", action="store_true", help="DB 저장 없이 수집만")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.dry_run:
        papers = fetch_papers(days_back=args.days_back, max_results=args.max_results)
        for paper in papers:
            print(f"\n[{paper.external_id}] {paper.published_date} {paper.url}")
            print(f"  제목: {paper.title}")
            print(f"  초록: {(paper.abstract or '')[:200]}...")
        print(f"\n총 {len(papers)}건 수집 (저장 안 함)")
        return

    collect_and_store(days_back=args.days_back, max_results=args.max_results)


if __name__ == "__main__":
    main()
