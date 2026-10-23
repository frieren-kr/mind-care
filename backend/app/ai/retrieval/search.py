"""검색 → Evidence Package (담당: 김현서).

search("엄마가 밤에 자꾸 깨요", k=5) 를 부르면 관련 논문 k편을
웅님과 합의한 Evidence Package 형식으로 돌려준다. (노션 「Evidence Package 형식」)

흐름:
  ① embed_query()        — 질문 → bge-m3 벡터 (논문 저장 때와 같은 모델)
  ② search_similar()     — DB에서 가장 가까운 논문 k개의 (paper_id, 점수)  [주현님 함수]
  ③ get_papers_by_ids()  — 그 논문들의 제목·초록·저널·DOI 등            [주현님 함수]
  ④ classify()           — 연구 유형(study_type) 붙이기 + 동물·비치매 연구 제외  [evidence.py]
  ⑤ Evidence Package로 변환

직접 실행해서 테스트 (backend/ 에서):
    python -m app.ai.retrieval.search "엄마가 밤에 자꾸 깨요"
    python -m app.ai.retrieval.search "caregiver burden intervention" --k 10
    python -m app.ai.retrieval.search "엄마가 밤에 자꾸 깨요" --include-animal   # 동물 연구도 보기
    python -m app.ai.retrieval.search "엄마가 밤에 자꾸 깨요" --all-diseases     # 치매 외 질환 논문도 보기

여러 질문을 합칠 때 (환자 여러 명 / 고민 여러 개):
    search_by_queries(["sleep disturbance in mild dementia ...", "wandering in dementia ..."], k=5)

가이드라인 붙이기 (A안: 논문 k편과 따로, evidence 목록 맨 뒤에 evidence_type="guideline"으로):
    search("엄마가 밤에 자꾸 깨요", k=5, guidelines=2)   # 논문 5편 + 가이드라인 2개
    python -m app.ai.retrieval.search "엄마가 밤에 자꾸 깨요" --guidelines 2
  - 기본값은 0개라서 기존 호출(주현님 피드·웅님 Agent)은 결과가 그대로다.
  - 같은 장·절의 조각이 여러 개 걸리면 점수가 가장 높은 조각 하나만 넣는다.
  - 가이드라인은 guideline_knowledge(scripts/load_guidelines.py로 적재)에서 찾는다.
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.ai.retrieval.embedding import MODEL_NAME, embed_query
from app.ai.retrieval.evidence import classify, is_dementia_related
from app.db.functions import (
    get_guidelines_by_ids,
    get_papers_by_ids,
    search_similar,
    search_similar_guidelines,
)

PUBMED_URL = "https://pubmed.ncbi.nlm.nih.gov/{pmid}/"


class EvidenceItem(BaseModel):
    """근거 한 건 (논문 또는 가이드라인). 값이 없으면 None(null)."""

    evidence_type: Literal["new_research", "guideline"]
    pmid: Optional[str] = None
    title: str
    publication_year: Optional[int] = None
    study_type: Optional[str] = Field(
        default=None,
        description="meta_analysis / systematic_review / guideline / rct / clinical_trial / "
        "observational / qualitative / review / case_report / expert_opinion / protocol / other",
    )
    relevance_score: float = Field(description="질문과의 코사인 유사도 (0~1, 클수록 관련)")
    ai_summary: Optional[str] = Field(default=None, description="웅님 요약. 아직 연결 전이라 None")
    abstract: Optional[str] = None
    full_text_available: bool = False
    journal: Optional[str] = None
    doi: Optional[str] = None
    organization: Optional[str] = None
    source_url: Optional[str] = None


class EvidencePackage(BaseModel):
    """search() 결과 = 웅님 Agent/요약에 넘기는 형식."""

    query: str
    evidence: list[EvidenceItem]


CANDIDATE_MULTIPLIER = 3  # 동물·비치매 연구 등을 걸러낸 뒤에도 k편이 남도록 k×3편을 먼저 가져온다


def _candidates(
    query_vec: list[float],
    n: int,
    exclude_animal: bool = True,
    dementia_only: bool = True,
) -> list[EvidenceItem]:
    """질문 벡터로 DB에서 가까운 논문 n편을 받아, 필터를 통과한 것만 유사도 순으로 돌려준다."""
    hits = search_similar(query_vec, top_k=n, model_name=MODEL_NAME)           # ①②
    if not hits:
        return []
    papers = get_papers_by_ids([h.paper_id for h in hits])                     # ③ (입력 순서 유지)
    score_by_id = {h.paper_id: h.score for h in hits}

    out = []
    for p in papers:
        ev = classify(p.publication_types, p.mesh_terms, p.title, p.abstract)  # ④
        if exclude_animal and ev.is_animal:
            continue
        if dementia_only and not is_dementia_related(p.mesh_terms, p.title, p.abstract):
            continue
        out.append(                                                            # ⑤
            EvidenceItem(
                evidence_type="new_research",
                pmid=p.external_id,
                title=p.title,
                publication_year=p.published_date.year if p.published_date else None,
                study_type=ev.study_type,
                relevance_score=round(float(score_by_id[p.paper_id]), 4),
                abstract=p.abstract,
                journal=p.journal,
                doi=p.doi,
                source_url=PUBMED_URL.format(pmid=p.external_id),
            )
        )
    return out


def _guideline_candidates(query_vec: list[float], n: int) -> list[EvidenceItem]:
    """질문 벡터로 가이드라인 조각을 찾아, 장·절마다 가장 가까운 조각 하나씩만 유사도 순으로 돌려준다."""
    if n <= 0:
        return []
    hits = search_similar_guidelines(query_vec, k=n * CANDIDATE_MULTIPLIER)   # [주현님 함수]
    if not hits:
        return []
    chunks = get_guidelines_by_ids([h.id for h in hits])                       # (입력 순서 유지)
    score_by_id = {h.id: h.score for h in hits}

    out, seen = [], set()
    for g in chunks:
        key = (g.source_url, g.section)
        if key in seen:  # 같은 장·절의 다른 조각은 건너뜀 (점수 높은 조각이 먼저 옴)
            continue
        seen.add(key)
        out.append(
            EvidenceItem(
                evidence_type="guideline",
                title=f"{g.doc_title} — {g.section}" if g.section else g.doc_title,
                publication_year=g.published_year,
                study_type="guideline",
                relevance_score=round(float(score_by_id[g.id]), 4),
                abstract=g.content,  # 가이드라인 조각 본문
                organization=g.source,
                source_url=g.source_url,
            )
        )
        if len(out) == n:
            break
    return out


def search_guidelines(query: str, k: int = 2) -> list[EvidenceItem]:
    """가이드라인만 따로 찾는다 (장·절이 겹치지 않게 k개)."""
    query = (query or "").strip()
    if not query or k <= 0:
        return []
    return _guideline_candidates(embed_query(query), k)


def search(
    query: str,
    k: int = 5,
    exclude_animal: bool = True,
    dementia_only: bool = True,
    guidelines: int = 0,
) -> EvidencePackage:
    """질문(한국어·영어 모두 가능)으로 관련 논문 k편을 찾아 Evidence Package로 돌려준다.

    exclude_animal=True면 동물·세포 연구(쥐 실험 등)는 결과에서 뺀다. (보호자 피드용 기본값)
    dementia_only=True면 치매·MCI와 무관한 논문(뇌졸중·파킨슨 등)은 뺀다. (evidence.is_dementia_related)
    guidelines=N이면 논문 k편 뒤에 가이드라인 N개를 따로 붙인다 (evidence_type="guideline"). 기본 0.
    """
    query = (query or "").strip()
    if not query or (k <= 0 and guidelines <= 0):
        return EvidencePackage(query=query, evidence=[])
    vec = embed_query(query)  # 논문·가이드라인 검색에 같은 벡터를 쓴다
    items: list[EvidenceItem] = []
    if k > 0:
        filtering = exclude_animal or dementia_only
        n = k * CANDIDATE_MULTIPLIER if filtering else k
        items = _candidates(vec, n, exclude_animal, dementia_only)[:k]
    return EvidencePackage(query=query, evidence=items + _guideline_candidates(vec, guidelines))


def search_by_queries(
    queries: list[str],
    k: int = 5,
    exclude_animal: bool = True,
    dementia_only: bool = True,
    guidelines: int = 0,
) -> EvidencePackage:
    """질문 여러 개(예: 환자별·고민별로 웅님 LLM이 만든 쿼리)로 검색해서 k편으로 합친다.

    합치는 방식: 질문마다 1등, 그다음 2등 … 순서로 번갈아 뽑는다(라운드 로빈).
      - 고민이 여러 개여도 한 고민 논문만 몰리지 않고 골고루 들어간다.
      - 같은 논문이 여러 질문에 걸리면 한 번만 넣는다(먼저 뽑힌 자리, 가장 높은 점수 유지).
    질문이 1개면 search()와 결과가 같다.
    guidelines=N이면 논문 뒤에 가이드라인 N개를 붙인다 (질문마다 번갈아, 같은 장·절은 한 번만). 기본 0.
    """
    queries = [q.strip() for q in (queries or []) if q and q.strip()]
    queries = list(dict.fromkeys(queries))  # 같은 질문 중복 제거 (순서 유지)
    label = " | ".join(queries)
    if not queries or (k <= 0 and guidelines <= 0):
        return EvidencePackage(query=label, evidence=[])

    vecs = [embed_query(q) for q in queries]
    papers = _merge_papers(vecs, k, exclude_animal, dementia_only) if k > 0 else []
    return EvidencePackage(query=label, evidence=papers + _merge_guidelines(vecs, guidelines))


def _merge_guidelines(vecs: list[list[float]], n: int) -> list[EvidenceItem]:
    """질문마다 가이드라인 후보를 받아 번갈아 n개 고른다 (같은 장·절은 한 번만)."""
    if n <= 0:
        return []
    lists = [_guideline_candidates(v, n) for v in vecs]
    out, seen = [], set()
    for rank in range(max(len(x) for x in lists) if lists else 0):
        for items in lists:
            if rank < len(items) and items[rank].title not in seen:
                seen.add(items[rank].title)
                out.append(items[rank])
                if len(out) == n:
                    return out
    return out


def _merge_papers(
    vecs: list[list[float]], k: int, exclude_animal: bool, dementia_only: bool
) -> list[EvidenceItem]:
    """질문마다 논문 후보를 받아 라운드 로빈으로 k편 고른다."""
    n = k * CANDIDATE_MULTIPLIER
    lists = [_candidates(v, n, exclude_animal, dementia_only) for v in vecs]

    picked: dict[str, EvidenceItem] = {}
    order: list[str] = []
    for rank in range(max(len(x) for x in lists) if lists else 0):
        for items in lists:
            if rank >= len(items):
                continue
            it = items[rank]
            key = it.pmid or it.title
            if key in picked:
                if it.relevance_score > picked[key].relevance_score:
                    picked[key] = it
                continue
            picked[key] = it
            order.append(key)
            if len(order) == k:
                return [picked[x] for x in order]
    return [picked[x] for x in order]


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="search() 테스트")
    ap.add_argument("query", help="검색할 질문 (따옴표로 감싸기)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--json", action="store_true", help="Evidence Package JSON 전체 출력")
    ap.add_argument("--include-animal", action="store_true", help="동물·세포 연구도 포함")
    ap.add_argument("--all-diseases", action="store_true", help="치매 외 질환 논문도 포함 (필터 끄기)")
    ap.add_argument("--guidelines", type=int, default=0, help="논문 뒤에 붙일 가이드라인 개수")
    a = ap.parse_args()

    pkg = search(a.query, a.k, exclude_animal=not a.include_animal, dementia_only=not a.all_diseases,
                 guidelines=a.guidelines)
    if a.json:
        print(pkg.model_dump_json(indent=2))
    else:
        print(f'\n질문: "{pkg.query}"  →  {len(pkg.evidence)}편\n')
        for i, e in enumerate(pkg.evidence, 1):
            tag = "가이드라인" if e.evidence_type == "guideline" else e.study_type
            print(f"{i}. [{e.relevance_score:.3f}] [{tag}] {e.title}")
            print(f"   {e.journal or e.organization or '-'} ({e.publication_year or '-'})  {e.source_url}\n")
