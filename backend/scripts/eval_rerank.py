"""Reranking 실험 — 임베딩 상위 30편을 bge-reranker-v2-m3로 다시 정렬하면 나아지나? (담당: 김현서)

비교하는 2가지
  base   : 지금 search()와 같은 순서 (임베딩 + 환자 검색일 때 보호자 논문 제외)
  rerank : base 상위 30편을 reranker 점수로 다시 정렬

검색어
  patient : 지금 피드·Agent 규칙과 같은 영어 증상 검색어 (scripts/eval_patient_focus.py와 같음)
  ko      : 보호자가 실제로 칠 법한 한국어 구어체 (evaluate_search.py의 ko_colloquial)

채점은 evaluate_search.py [A] 방식 (정답지에 있는 논문만 남긴 순위). 걸린 시간도 같이 잰다.
정답지: --gold v2(기본) = 환자 중심 규칙 + 사람 검수 반영 / --gold v1 = 처음 AI 라벨

실행 (backend/ 에서, Docker DB 켜고). 처음엔 reranker 모델(약 2.3GB) 다운로드부터 한다.
    python -m scripts.eval_rerank
    python -m scripts.eval_rerank --show 10      # base vs rerank Top-10 비교 출력
    RERANK_DEVICE=cpu python -m scripts.eval_rerank   # GPU 메모리 부족(MPS OOM)일 때
"""
import argparse
import time
from collections import Counter, defaultdict

from app.ai.retrieval.embedding import MODEL_NAME, embed_query, paper_text
from app.ai.retrieval.evidence import (
    classify,
    is_caregiver_focused,
    is_dementia_related,
    query_targets_caregiver,
)
from app.ai.retrieval.rerank import RERANK_POOL, get_reranker, rerank_scores
from app.db.functions import get_papers_by_ids, search_similar
from scripts.eval_patient_focus import QUERIES as PATIENT_QUERIES
from scripts.evaluate_search import ALL_K, GOLD_CSV, GOLD_V2_CSV, PROFILES, QUERIES as GOLD_QUERIES, load_gold, ndcg_at, precision_at, recall2_at

def base_ranking(query: str) -> list[dict]:
    """지금 search()와 같은 필터·순서로 DB 전체를 줄 세운다."""
    hits = search_similar(embed_query(query), top_k=ALL_K, model_name=MODEL_NAME)
    score = {h.paper_id: h.score for h in hits}
    drop = not query_targets_caregiver(query)  # search()의 _drop_for와 같은 규칙
    rows = []
    for p in get_papers_by_ids([h.paper_id for h in hits]):
        if classify(p.publication_types, p.mesh_terms, p.title, p.abstract).is_animal:
            continue
        if not is_dementia_related(p.mesh_terms, p.title, p.abstract):
            continue
        cg = is_caregiver_focused(p.title)
        if drop and cg:
            continue
        rows.append({"pmid": p.external_id.strip(), "title": p.title, "text": paper_text(p.title, p.abstract),
                     "cg": cg, "s": float(score[p.paper_id])})
    rows.sort(key=lambda r: r["s"], reverse=True)
    return rows


def score_pool(query: str, rows: list[dict]) -> float:
    """상위 RERANK_POOL편에 reranker 점수(r0)를 한 번만 매긴다. 걸린 초를 돌려준다."""
    t0 = time.time()
    pool = rows[:RERANK_POOL]
    for r, s in zip(pool, rerank_scores(query, [r["text"] for r in pool])):
        r["r0"] = s
    return time.time() - t0


def reranked(rows: list[dict]) -> list[dict]:
    """미리 매긴 r0로 상위 30편만 다시 정렬한다 (모델을 다시 돌리지 않음)."""
    pool, rest = rows[:RERANK_POOL], rows[RERANK_POOL:]
    return sorted(pool, key=lambda r: r["r0"], reverse=True) + rest


def metrics(rows: list[dict], g: dict) -> dict:
    labels = [g[r["pmid"]] for r in rows if r["pmid"] in g]
    return {"P@5": precision_at(labels, 5), "P@10": precision_at(labels, 10),
            "nDCG@10": ndcg_at(labels, labels, 10), "2점@10": recall2_at(labels, labels, 10),
            "보호자@10": sum(1 for r in rows[:10] if r["cg"])}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=0)
    ap.add_argument("--gold", choices=["v1", "v2"], default="v2")
    a = ap.parse_args()

    gold = load_gold(GOLD_CSV if a.gold == "v1" else GOLD_V2_CSV)
    print(f"정답지: {a.gold}")
    t0 = time.time()
    get_reranker()
    print(f"reranker 로드: {time.time() - t0:.1f}초")

    sets = {pid: {"patient": PATIENT_QUERIES[pid], "ko": [GOLD_QUERIES[pid]["ko_colloquial"]]} for pid in PROFILES}
    agg: dict = defaultdict(Counter)   # (프로필, 검색어종류, 방식) → 지표 합
    cnt: Counter = Counter()
    secs = []
    shows = []
    for pid, groups in sets.items():
        g = gold[pid]
        for qtype, queries in groups.items():
            for q in queries:
                base = base_ranking(q)
                secs.append(score_pool(q, base))
                print(f"  [{pid}] {q[:60]} … rerank {secs[-1]:.0f}초", flush=True)
                rr = reranked(base)
                for name, rows in (("base", base), ("rerank", rr)):
                    for k, v in metrics(rows, g).items():
                        if v == v:  # NaN 제외
                            agg[(pid, qtype, name)][k] += v
                    cnt[(pid, qtype, name)] += 1
                shows.append((pid, q, base, rr, g))

    cols = ["P@5", "P@10", "nDCG@10", "2점@10", "보호자@10"]
    print(f"\n{'프로필':<14}{'검색어':<9}{'방식':<11}" + "".join(f"{c:>10}" for c in cols))
    for pid in PROFILES:
        for qtype in ("patient", "ko"):
            for name in ("base", "rerank"):
                key = (pid, qtype, name)
                n = cnt[key]
                print(f"{pid} {PROFILES[pid]:<11}{qtype:<9}{name:<11}"
                      + "".join(f"{agg[key][c] / n:>10.3f}" for c in cols))
        print()

    print("전체 평균")
    for qtype in ("patient", "ko"):
        for name in ("base", "rerank"):
            keys = [(pid, qtype, name) for pid in PROFILES]
            line = "".join(f"{sum(agg[k][c] / cnt[k] for k in keys) / len(keys):>10.3f}" for c in cols)
            print(f"  {qtype:<9}{name:<11}{line}")
    print(f"\nrerank 시간: 질문당 평균 {sum(secs) / len(secs):.1f}초 (후보 {RERANK_POOL}편)")

    if a.show:
        for pid, q, base, rr, g in shows:
            print(f"\n[{pid}] {q}")
            for title, rows in (("base", base), ("rerank", rr)):
                print(f"  -- {title}")
                for i, r in enumerate(rows[: a.show], 1):
                    print(f"   {i:2d}. ({g.get(r['pmid'], '-')}) {'[보호자]' if r['cg'] else '        '} {r['title'][:80]}")


if __name__ == "__main__":
    main()
