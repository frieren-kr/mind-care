"""검색 → Evidence Package (담당: 김현서).

search("엄마가 밤에 자꾸 깨요", k=5) 를 부르면 관련 논문 k편을
웅님과 합의한 Evidence Package 형식으로 돌려준다. (노션 「Evidence Package 형식」)

흐름:
  ① embed_query()        — 질문 → bge-m3 벡터 (논문 저장 때와 같은 모델)
  ② search_similar()     — DB에서 가장 가까운 논문 k개의 (paper_id, 점수)  [주현님 함수]
  ③ get_papers_by_ids()  — 그 논문들의 제목·초록·저널·DOI 등            [주현님 함수]
  ④ classify()           — 연구 유형(study_type) 붙이기 + 동물 연구 제외  [evidence.py]
  ⑤ Evidence Package로 변환

직접 실행해서 테스트 (backend/ 에서):
    python -m app.ai.retrieval.search "엄마가 밤에 자꾸 깨요"
    python -m app.ai.retrieval.search "caregiver burden intervention" --k 10
    python -m app.ai.retrieval.search "엄마가 밤에 자꾸 깨요" --include-animal   # 동물 연구도 보기
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.ai.retrieval.embedding import MODEL_NAME, embed_query
from app.ai.retrieval.evidence import classify
from app.db.functions import get_papers_by_ids, search_similar

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


CANDIDATE_MULTIPLIER = 3  # 동물 연구 등을 걸러낸 뒤에도 k편이 남도록 k×3편을 먼저 가져온다


def search(query: str, k: int = 5, exclude_animal: bool = True) -> EvidencePackage:
    """질문(한국어·영어 모두 가능)으로 관련 논문 k편을 찾아 Evidence Package로 돌려준다.

    exclude_animal=True면 동물·세포 연구(쥐 실험 등)는 결과에서 뺀다. (보호자 피드용 기본값)
    """
    query = (query or "").strip()
    if not query or k <= 0:
        return EvidencePackage(query=query, evidence=[])

    n = k * CANDIDATE_MULTIPLIER if exclude_animal else k
    hits = search_similar(embed_query(query), top_k=n, model_name=MODEL_NAME)  # ①②
    if not hits:
        return EvidencePackage(query=query, evidence=[])

    papers = get_papers_by_ids([h.paper_id for h in hits])                     # ③ (입력 순서 유지)
    score_by_id = {h.paper_id: h.score for h in hits}

    evidence = []
    for p in papers:
        ev = classify(p.publication_types, p.mesh_terms, p.title, p.abstract)  # ④
        if exclude_animal and ev.is_animal:
            continue
        evidence.append(                                                       # ⑤
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
        if len(evidence) == k:
            break
    return EvidencePackage(query=query, evidence=evidence)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="search() 테스트")
    ap.add_argument("query", help="검색할 질문 (따옴표로 감싸기)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--json", action="store_true", help="Evidence Package JSON 전체 출력")
    ap.add_argument("--include-animal", action="store_true", help="동물·세포 연구도 포함")
    a = ap.parse_args()

    pkg = search(a.query, a.k, exclude_animal=not a.include_animal)
    if a.json:
        print(pkg.model_dump_json(indent=2))
    else:
        print(f'\n질문: "{pkg.query}"  →  {len(pkg.evidence)}편\n')
        for i, e in enumerate(pkg.evidence, 1):
            print(f"{i}. [{e.relevance_score:.3f}] [{e.study_type}] {e.title}")
            print(f"   {e.journal or '-'} ({e.publication_year or '-'})  {e.source_url}\n")
