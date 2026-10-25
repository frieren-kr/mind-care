"""환자 증상 검색에서 보호자 중심 논문 미루기 실험 (담당: 김현서).

웅님 질문: 간병인1(환자 수면 문제)로 검색하면 보호자 수면·교대근무자 수면 논문이 위로 오고,
환자 수면 논문은 7~8위로 밀린다 → 보호자 중심 논문을 점수에서 조금 깎아 뒤로 미루면 나아지나?

  1) 판별 점검: 골드셋 논문 중 is_caregiver_focused()가 True인 논문의 라벨 분포 (프로필별)
  2) 환자 중심 검색어(지금 Agent·피드 규칙과 같은 영어 증상 검색어)로
     보호자 논문 감점 0 / 0.02 / 0.05 / 0.1 / 맨 뒤 를 비교 → P@5, nDCG@10, Top-10 안 보호자 논문 수

채점은 evaluate_search.py [A] 방식 (정답지에 있는 논문만 남긴 순위).

실행 (backend/ 에서, Docker DB 켜고):
    python -m scripts.eval_patient_focus
    python -m scripts.eval_patient_focus --show 10    # 감점 0 vs 선택값 Top-10 비교 출력
"""
import argparse
from collections import Counter

from app.ai.retrieval.embedding import MODEL_NAME, embed_query
from app.ai.retrieval.evidence import classify, is_caregiver_focused, is_dementia_related
from app.db.functions import get_papers_by_ids, search_similar
from scripts.evaluate_search import ALL_K, PROFILES, load_gold, ndcg_at, precision_at

QUERIES = {
    "P1": [
        "sleep disturbance in mild dementia: interventions and management",
        "nighttime awakening and sleep problems in mild cognitive impairment",
    ],
    "P2": [
        "wandering in moderate dementia: interventions and management",
        "fall prevention in moderate dementia",
        "nighttime agitation and sundowning in moderate dementia: interventions",
    ],
    "P3": [
        "cholinesterase inhibitors and memantine for mild dementia",
        "cognitive stimulation and cognitive training for mild dementia",
    ],
}
PENALTIES = [0.0, 0.02, 0.05, 0.1, 9.0]  # 9 = 사실상 맨 뒤


def ranked_with_scores(query: str) -> list[tuple[str, float, bool, str]]:
    """(pmid, 점수, 보호자중심, 제목) — search()와 같은 필터 (동물 제외 + 치매 필터)."""
    hits = search_similar(embed_query(query), top_k=ALL_K, model_name=MODEL_NAME)
    score = {h.paper_id: h.score for h in hits}
    out = []
    for p in get_papers_by_ids([h.paper_id for h in hits]):
        if classify(p.publication_types, p.mesh_terms, p.title, p.abstract).is_animal:
            continue
        if not is_dementia_related(p.mesh_terms, p.title, p.abstract):
            continue
        out.append((p.external_id.strip(), float(score[p.paper_id]), is_caregiver_focused(p.title), p.title))
    return out


def rerank(items, penalty: float):
    return sorted(items, key=lambda x: x[1] - (penalty if x[2] else 0.0), reverse=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=0)
    ap.add_argument("--pick", type=float, default=0.05, help="--show에서 비교할 감점")
    a = ap.parse_args()
    gold = load_gold()

    # 1) 판별 점검
    cache = {pid: [(q, ranked_with_scores(q)) for q in qs] for pid, qs in QUERIES.items()}
    flag = {pm: cg for runs in cache.values() for _, items in runs for pm, _, cg, _ in items}
    print("\n[1] 골드셋에서 보호자 중심으로 판별된 논문의 라벨 분포")
    print(f"{'프로필':<16}{'보호자중심 0/1/2점':>20}{'그 외 0/1/2점':>18}")
    for pid, g in gold.items():
        cg = Counter(v for pm, v in g.items() if flag.get(pm) is True)
        ot = Counter(v for pm, v in g.items() if flag.get(pm) is False)
        print(f"{pid} {PROFILES[pid]:<12}{cg[0]:>8}/{cg[1]}/{cg[2]:<8}{ot[0]:>8}/{ot[1]}/{ot[2]}")

    # 2) 감점 비교
    print("\n[2] 환자 중심 검색어 — 보호자 논문 감점별 (질문 평균)")
    print(f"{'프로필':<16}{'감점':>6}{'P@5':>8}{'P@10':>8}{'nDCG@10':>10}{'Top10 보호자':>12}")
    for pid, runs in cache.items():
        g = gold[pid]
        for pen in PENALTIES:
            m = Counter()
            for _, items in runs:
                r = rerank(items, pen)
                labels = [g[pm] for pm, *_ in r if pm in g]
                m["P@5"] += precision_at(labels, 5)
                m["P@10"] += precision_at(labels, 10)
                m["nDCG"] += ndcg_at(labels, labels, 10)
                m["cg"] += sum(1 for x in r[:10] if x[2])
            n = len(runs)
            name = "맨 뒤" if pen >= 9 else f"{pen:.2f}"
            print(f"{pid} {PROFILES[pid]:<12}{name:>6}{m['P@5']/n:>8.3f}{m['P@10']/n:>8.3f}"
                  f"{m['nDCG']/n:>10.3f}{m['cg']/n:>12.1f}")

    if a.show:
        for pid, runs in cache.items():
            for q, items in runs:
                print(f"\n[{pid}] {q}")
                for title, pen in (("감점 0", 0.0), (f"감점 {a.pick}", a.pick)):
                    print(f"  -- {title}")
                    for i, (pm, sc, cg, t) in enumerate(rerank(items, pen)[: a.show], 1):
                        lab = gold[pid].get(pm, "-")
                        print(f"   {i:2d}. ({lab}) {'[보호자]' if cg else '        '} {sc:.3f} {t[:80]}")


if __name__ == "__main__":
    main()
