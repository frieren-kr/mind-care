"""골드셋으로 검색 정확도 측정 (담당: 김현서).

정답지: app/ai/eval/goldset_labels_901x3.csv
  - 논문 901편 × 프로필 3개 = 2,703건, 판단 2(매우 관련) / 1(약간) / 0(없음)
  - 1차 AI 초안 + AI 눈가림 교차검토 판 (사람 검수 전) → 보고서엔 "AI 2차 교차검토"로 적는다.

무엇을 재나
  프로필마다 질문을 3가지로 바꿔 가며 검색해 보고, 상위 결과가 정답지와 얼마나 맞는지 본다.
    ko_profile    : 프로필 설명 그대로 (한국어)
    ko_colloquial : 보호자가 실제로 칠 법한 구어체 (한국어)
    en_rewritten  : 증상·대상 중심으로 다시 쓴 영어 질문 (Query Rewriting 흉내)
  → 세 줄을 비교하면 "질문을 다시 쓰면 좋아지나?"에 대한 첫 근거가 된다.

평가 방식
  [A] 라벨 범위 (메인 지표)
      DB에서 검색한 순위 중 **정답지에 있는 논문만** 남겨 순위를 매긴다.
      모든 논문에 정답이 있으니 P@k·nDCG를 공정하게 잴 수 있다.
  [B] 전체 DB (참고)
      실제 서비스처럼 DB 전체에서 Top-10을 뽑고, 그중 정답지에 있는 비율(judged@10)을 같이 적는다.
      정답지 밖 논문은 채점할 수 없어서 [B]의 P@10은 "채점 가능한 논문 기준"이다.

지표 (모두 0~1, 클수록 좋음)
  P@5, P@10   : 상위 5/10편 중 관련(판단 1 이상) 비율
  nDCG@10     : 관련 높은 논문(2점)이 위에 있을수록 높음. 이득 = 2^판단 - 1
  2점회수@10  : 매우 관련(2점) 논문 중 상위 10편 안에 들어온 비율

실행 (backend/ 에서, Docker DB가 켜져 있어야 함)
    python -m scripts.evaluate_search                 # 표 출력 + 결과 파일 저장
    python -m scripts.evaluate_search --show 10       # 질문별 Top-10 목록도 출력
    python -m scripts.evaluate_search --tag after           # 결과 파일 이름에 붙일 이름 (전후 비교용)
    python -m scripts.evaluate_search --no-dementia-filter  # 치매 필터 끈 상태 (= before)

결과 파일: app/ai/eval/results/<날짜>_<tag>.csv (지표) / _top10.csv (질문별 Top-10)
"""
import argparse
import csv
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from app.ai.retrieval.embedding import MODEL_NAME, embed_query
from app.ai.retrieval.evidence import classify, is_dementia_related
from app.db.functions import get_papers_by_ids, search_similar

EVAL_DIR = Path(__file__).resolve().parents[1] / "app" / "ai" / "eval"
GOLD_CSV = EVAL_DIR / "goldset_labels_901x3.csv"
RESULT_DIR = EVAL_DIR / "results"

ALL_K = 100_000  # DB 전체 순위를 받기 위한 큰 값

PROFILES = {
    "P1": "수면장애",
    "P2": "배회·공격성",
    "P3": "약물·비약물 치료",
}

QUERIES = {
    "P1": {
        "ko_profile": "경도 단계, 밤에 잠을 못 주무시는 어머니를 돌보는 딸",
        "ko_colloquial": "엄마가 밤에 자꾸 깨서 잠을 못 주무세요. 어떻게 해야 하나요?",
        "en_rewritten": "sleep disturbance in mild dementia: interventions family caregivers can use at home",
    },
    "P2": {
        "ko_profile": "중등도 단계, 저녁마다 집 밖으로 나가려는 아버지를 돌보는 배우자",
        "ko_colloquial": "남편이 저녁만 되면 자꾸 밖에 나가려고 하고 화를 내요. 어떻게 해야 하나요?",
        "en_rewritten": "wandering, agitation and sundowning in moderate dementia: "
        "non-pharmacological strategies for family caregivers",
    },
    "P3": {
        "ko_profile": "막 진단받은 경도 환자의 가족, 약물·비약물 치료 정보를 찾는 중",
        "ko_colloquial": "엄마가 치매 초기 진단을 받았는데 어떤 치료를 받을 수 있나요?",
        "en_rewritten": "effectiveness of pharmacological and non-pharmacological treatments "
        "for mild dementia and mild cognitive impairment",
    },
}


# ---------------------------------------------------------------- 지표
def precision_at(labels: list[int], k: int) -> float:
    top = labels[:k]
    return sum(1 for x in top if x >= 1) / k if top else 0.0


def dcg(labels: list[int]) -> float:
    return sum((2**rel - 1) / math.log2(i + 2) for i, rel in enumerate(labels))


def ndcg_at(labels: list[int], all_labels: list[int], k: int) -> float:
    ideal = dcg(sorted(all_labels, reverse=True)[:k])
    return dcg(labels[:k]) / ideal if ideal > 0 else 0.0


def recall2_at(labels: list[int], all_labels: list[int], k: int) -> float:
    total = sum(1 for x in all_labels if x == 2)
    return sum(1 for x in labels[:k] if x == 2) / total if total else float("nan")


# ---------------------------------------------------------------- 데이터
def load_gold() -> dict[str, dict[str, int]]:
    """{프로필 코드: {PMID: 판단}}"""
    gold: dict[str, dict[str, int]] = defaultdict(dict)
    with open(GOLD_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            gold[row["profile_id"]][row["pmid"].strip()] = int(row["label"])
    return gold


def rank_all(query: str, dementia_only: bool = True) -> tuple[list[str], dict[str, str]]:
    """질문으로 DB 전체를 유사도 순으로 정렬한 PMID 목록 (search()와 같은 필터: 동물 제외 + 치매 필터)."""
    hits = search_similar(embed_query(query), top_k=ALL_K, model_name=MODEL_NAME)
    papers = get_papers_by_ids([h.paper_id for h in hits])
    ranked = []
    for p in papers:
        ev = classify(p.publication_types, p.mesh_terms, p.title, p.abstract)
        if ev.is_animal:
            continue
        if dementia_only and not is_dementia_related(p.mesh_terms, p.title, p.abstract):
            continue
        ranked.append(p.external_id.strip())
    return ranked, {p.external_id.strip(): p.title for p in papers}


# ---------------------------------------------------------------- 실행
def main() -> None:
    parser = argparse.ArgumentParser(description="골드셋 검색 정확도 측정")
    parser.add_argument("--show", type=int, default=0, help="질문별 Top-N 목록 출력 (0이면 안 함)")
    parser.add_argument("--tag", default="baseline", help="결과 파일 이름에 붙일 이름")
    parser.add_argument("--no-dementia-filter", action="store_true",
                        help="치매 필터 끄기 (필터 넣기 전 before 결과 재현용)")
    args = parser.parse_args()

    gold = load_gold()
    gold_pmids = set().union(*[set(v) for v in gold.values()])

    rows, top_rows = [], []
    db_pmids: set[str] = set()
    titles: dict[str, str] = {}
    top10_by_profile: dict[str, set[str]] = defaultdict(set)

    for pid, qset in QUERIES.items():
        g = gold[pid]
        for qname, query in qset.items():
            ranked_db, t = rank_all(query, dementia_only=not args.no_dementia_filter)
            titles.update(t)
            db_pmids.update(ranked_db)

            # [A] 라벨 범위: 정답지에 있는 논문만 남긴 순위
            ranked_gold = [pm for pm in ranked_db if pm in g]
            labels = [g[pm] for pm in ranked_gold]
            all_labels = list(g[pm] for pm in ranked_gold)  # DB에 있는 라벨 논문 기준 이상적 순위

            # [B] 전체 DB Top-10
            top10_db = ranked_db[:10]
            judged = [pm for pm in top10_db if pm in g]
            p10_db = sum(1 for pm in judged if g[pm] >= 1) / len(judged) if judged else float("nan")

            rows.append({
                "profile": pid,
                "profile_name": PROFILES[pid],
                "query_type": qname,
                "query": query,
                "P@5": round(precision_at(labels, 5), 3),
                "P@10": round(precision_at(labels, 10), 3),
                "nDCG@10": round(ndcg_at(labels, all_labels, 10), 3),
                "2점회수@10": round(recall2_at(labels, all_labels, 10), 3),
                "DB_P@10(채점가능)": round(p10_db, 3),
                "DB_judged@10": round(len(judged) / 10, 2),
            })
            for rank, pm in enumerate(ranked_gold[:10], 1):
                top_rows.append({"profile": pid, "query_type": qname, "rank": rank, "pmid": pm,
                                 "label": g[pm], "title": titles.get(pm, "")})
            if qname == "en_rewritten":
                top10_by_profile[pid] = set(ranked_gold[:10])

            if args.show:
                print(f"\n[{pid} {PROFILES[pid]} / {qname}] {query}")
                for rank, pm in enumerate(ranked_gold[: args.show], 1):
                    print(f"  {rank:2d}. ({g[pm]}) {pm}  {titles.get(pm, '')[:90]}")

    # ---- 범위 확인
    covered = gold_pmids & db_pmids
    print(f"\n정답지 {len(gold_pmids)}편 중 DB(임베딩 완료)에 있는 논문: {len(covered)}편"
          f" → [A]는 이 {len(covered)}편 기준으로 채점")
    if len(covered) < len(gold_pmids):
        print(f"  (DB에 없는 정답지 논문 {len(gold_pmids) - len(covered)}편은 평가에서 빠짐)")

    # ---- 표 출력
    cols = ["P@5", "P@10", "nDCG@10", "2점회수@10", "DB_P@10(채점가능)", "DB_judged@10"]
    print("\n프로필            질문 유형       " + "  ".join(f"{c:>10}" for c in cols))
    for r in rows:
        print(f"{r['profile']} {r['profile_name']:<10} {r['query_type']:<14} "
              + "  ".join(f"{r[c]:>10}" for c in cols))

    print("\n질문 유형별 평균 (프로필 3개)")
    for qname in ["ko_profile", "ko_colloquial", "en_rewritten"]:
        sub = [r for r in rows if r["query_type"] == qname]
        avg = {c: sum(r[c] for r in sub if not math.isnan(r[c])) / len(sub) for c in cols[:4]}
        print(f"  {qname:<14} " + "  ".join(f"{c} {avg[c]:.3f}" for c in cols[:4]))

    # ---- 같은 논문이 여러 프로필에 반복되는지 (영어 질문 Top-10 기준)
    sets = list(top10_by_profile.values())
    in_all = set.intersection(*sets) if len(sets) == 3 else set()
    print(f"\n세 프로필 Top-10(en_rewritten)에 모두 나온 논문: {len(in_all)}편")
    for pm in in_all:
        print(f"  - {pm} {titles.get(pm, '')[:90]}")

    # ---- 저장
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out = RESULT_DIR / f"{stamp}_{args.tag}.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    out_top = RESULT_DIR / f"{stamp}_{args.tag}_top10.csv"
    with open(out_top, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(top_rows[0].keys()))
        w.writeheader()
        w.writerows(top_rows)
    print(f"\n저장: {out.relative_to(EVAL_DIR.parents[2])}")
    print(f"      {out_top.relative_to(EVAL_DIR.parents[2])}")


if __name__ == "__main__":
    main()
