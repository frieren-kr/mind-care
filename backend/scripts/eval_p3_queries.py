"""P3(약물·비약물 치료) 검색어 실험 (담당: 김현서).

같은 골드셋(P3 라벨)으로 검색어 짜는 방식 몇 가지를 비교한다.
여러 줄짜리 검색어는 search_by_queries()처럼 줄마다 순위를 뽑아 번갈아(round-robin) 합치고 중복을 뺀다.

채점은 evaluate_search.py의 [A] 방식과 같다 (DB 순위 중 정답지에 있는 논문만 남겨 P@k·nDCG).

실행 (backend/ 에서, Docker DB 켜고):
    python -m scripts.eval_p3_queries            # 표만
    python -m scripts.eval_p3_queries --show 10  # 가장 좋은 방식의 Top-10도 출력
"""
import argparse

from scripts.evaluate_search import load_gold, ndcg_at, precision_at, rank_all, recall2_at

STAGE = "mild dementia"

VARIANTS: dict[str, list[str]] = {
    # 지금까지 쓰던 영어 한 줄 (evaluate_search.py의 en_rewritten)
    "A_한줄(기존)": [
        "effectiveness of pharmacological and non-pharmacological treatments "
        "for mild dementia and mild cognitive impairment",
    ],
    # 약물 / 비약물 두 줄로 나누기
    "B_약물·비약물_2줄": [
        f"pharmacological treatment for {STAGE}: efficacy and safety",
        f"non-pharmacological interventions for {STAGE}: effectiveness",
    ],
    # 오늘 정한 단어표 형식 그대로 (관심사 '약물 치료', '비약물 치료'가 들어온 경우)
    "C_단어표형식": [
        f"pharmacological treatment for people with {STAGE}",
        f"non-pharmacological interventions for people with {STAGE}",
    ],
    # 구체적인 치료 이름으로 4줄
    "D_치료이름_4줄": [
        f"cholinesterase inhibitors and memantine for {STAGE}",
        f"anti-amyloid antibody treatment (lecanemab, donanemab) for early Alzheimer's disease",
        f"cognitive stimulation and cognitive training for {STAGE}",
        f"physical exercise intervention for {STAGE} and mild cognitive impairment",
    ],
    # 약물 2줄 + 비약물 2줄 (D보다 넓게)
    "E_약물2+비약물2": [
        f"pharmacological treatment for {STAGE}: efficacy and safety",
        f"disease-modifying drugs for early Alzheimer's disease",
        f"non-pharmacological interventions for {STAGE}: effectiveness",
        f"cognitive, physical and psychosocial interventions for {STAGE}",
    ],
}


def merge_round_robin(rankings: list[list[str]]) -> list[str]:
    """search_by_queries()처럼 줄마다 1편씩 번갈아 뽑고 중복은 건너뛴다."""
    out, seen = [], set()
    for i in range(max(len(r) for r in rankings)):
        for r in rankings:
            if i < len(r) and r[i] not in seen:
                seen.add(r[i])
                out.append(r[i])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="P3 검색어 실험")
    ap.add_argument("--show", type=int, default=0, help="가장 좋은 방식의 Top-N 출력")
    a = ap.parse_args()

    g = load_gold()["P3"]
    titles: dict[str, str] = {}
    results = []
    for name, queries in VARIANTS.items():
        rankings = []
        for q in queries:
            ranked, t = rank_all(q)
            titles.update(t)
            rankings.append([pm for pm in ranked if pm in g])  # [A] 라벨 범위
        merged = merge_round_robin(rankings)
        labels = [g[pm] for pm in merged]
        results.append((name, merged, {
            "P@5": precision_at(labels, 5),
            "P@10": precision_at(labels, 10),
            "nDCG@10": ndcg_at(labels, labels, 10),
            "2점회수@10": recall2_at(labels, labels, 10),
        }))

    cols = ["P@5", "P@10", "nDCG@10", "2점회수@10"]
    print(f"\nP3 약물·비약물 치료 — 정답지 {len(g)}편 (2점 {sum(1 for v in g.values() if v == 2)}편)\n")
    print(f"{'방식':<18}" + "".join(f"{c:>12}" for c in cols))
    for name, _, m in results:
        print(f"{name:<18}" + "".join(f"{m[c]:>12.3f}" for c in cols))

    best = max(results, key=lambda r: r[2]["nDCG@10"])
    print(f"\n가장 좋은 방식(nDCG@10): {best[0]}")
    if a.show:
        for i, pm in enumerate(best[1][: a.show], 1):
            print(f"  {i:2d}. ({g[pm]}) {pm}  {titles.get(pm, '')[:90]}")


if __name__ == "__main__":
    main()
