"""DB 논문 전체의 연구 유형(study_type) 분포 보기 (담당: 김현서).

근거등급 규칙 매핑(evidence.py)이 실제 논문에 어떻게 붙는지 확인하는 용도.
임베딩된 논문 전체를 가져오기 위해 search_similar()에 큰 top_k를 준다(DB 직접 조회 금지 규칙 때문).

실행 (backend/ 에서, Docker DB가 떠 있어야 함):
    python -m scripts.evidence_stats
    python -m scripts.evidence_stats --show other      # 'other'로 분류된 논문 제목 보기
"""
import argparse
from collections import Counter

from app.ai.retrieval.embedding import MODEL_NAME, embed_query
from app.ai.retrieval.evidence import LEVEL, classify
from app.db.functions import get_papers_by_ids, search_similar


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", help="이 study_type으로 분류된 논문 제목 출력 (예: other, animal)")
    ap.add_argument("--n", type=int, default=15, help="--show 할 때 몇 편 볼지")
    a = ap.parse_args()

    hits = search_similar(embed_query("dementia"), top_k=100_000, model_name=MODEL_NAME)
    papers = get_papers_by_ids([h.paper_id for h in hits])
    results = [(p, classify(p.publication_types, p.mesh_terms, p.title, p.abstract)) for p in papers]

    by_type = Counter(ev.study_type for _, ev in results)
    by_source = Counter(ev.source for _, ev in results)
    total = len(results)
    print(f"\n논문 {total}편 연구 유형 분포\n")
    print(f"{'study_type':<18}{'등급':>4}{'편수':>7}{'비율':>8}")
    for st, cnt in sorted(by_type.items(), key=lambda x: (LEVEL.get(x[0]) or 9, -x[1])):
        lv = LEVEL.get(st)
        print(f"{st:<18}{(str(lv) if lv else '-'):>4}{cnt:>7}{cnt / total:>8.1%}")
    print(f"\n판단 근거: PubMed 꼬리표 {by_source['pubtype']}편 / RCT꼬리표→관찰 보정 {by_source['pubtype+title']}편 / 제목·초록 키워드 {by_source['keyword']}편 / 판단 불가 {by_source['none']}편")

    if a.show:
        print(f"\n[{a.show}] 예시 {a.n}편")
        for p, ev in [r for r in results if r[1].study_type == a.show][: a.n]:
            print(f" - {p.title[:110]}  {p.publication_types}")


if __name__ == "__main__":
    main()
