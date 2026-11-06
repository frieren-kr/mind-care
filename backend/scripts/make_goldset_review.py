"""골드셋 사람 검수용 30편 뽑기 (담당: 김현서).

정답지(AI 라벨) 901편 중 30편을 골라, 사람이 점수를 매길 표를 만든다.
  ① 검색 상위에 자주 나오는 논문 10편  — 점수에 실제로 영향을 주는 논문
  ② AI 신뢰도 '하'가 있는 논문 10편   — 틀렸을 가능성이 큰 논문
  ③ 무작위 10편                        — 전체 정답지 품질 대표
검수자는 AI 점수를 보면 따라가게 되므로(앵커링), 검수용 표에는 AI 점수를 넣지 않고 따로 저장한다.

결과 (app/ai/eval/review/):
  goldset_review_30.csv      — 검수자에게 줄 표 (제목·초록·링크, 빈 P1/P2/P3 점수 칸)
  goldset_review_30_key.csv  — AI 점수·신뢰도·이유 (일치율 계산용, 검수자에게 주지 않음)

실행 (backend/ 에서, Docker DB 켜고):
    python -m scripts.make_goldset_review
같은 seed면 항상 같은 30편이 뽑힌다.
"""
import argparse
import csv
import random
from collections import Counter, defaultdict
from pathlib import Path

from app.ai.retrieval.embedding import MODEL_NAME, embed_query
from app.ai.retrieval.evidence import classify, is_dementia_related
from app.db.functions import get_papers_by_ids, search_similar
from scripts.eval_patient_focus import QUERIES as PATIENT_QUERIES
from scripts.evaluate_search import ALL_K, GOLD_CSV, PROFILES, QUERIES as GOLD_QUERIES

OUT_DIR = Path(__file__).resolve().parents[1] / "app" / "ai" / "eval" / "review"
TOP_N = 20          # 질문마다 정답지 논문 중 상위 몇 편을 '자주 나오는 논문' 후보로 볼지
PER_GROUP = 10
ABSTRACT_CHARS = 700


def load_rows() -> list[dict]:
    with open(GOLD_CSV, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def ranked_gold(query: str, gold_pmids: set[str], papers_by_pmid: dict) -> list[str]:
    """search()와 같은 필터(동물 제외 + 치매 필터)로 줄 세운 뒤 정답지에 있는 논문만 남긴 PMID 순서."""
    hits = search_similar(embed_query(query), top_k=ALL_K, model_name=MODEL_NAME)
    out = []
    for p in get_papers_by_ids([h.paper_id for h in hits]):
        pm = p.external_id.strip()
        papers_by_pmid.setdefault(pm, p)
        if classify(p.publication_types, p.mesh_terms, p.title, p.abstract).is_animal:
            continue
        if not is_dementia_related(p.mesh_terms, p.title, p.abstract):
            continue
        if pm in gold_pmids:
            out.append(pm)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    rng = random.Random(a.seed)

    rows = load_rows()
    gold_pmids = {r["pmid"].strip() for r in rows}
    by_pmid: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_pmid[r["pmid"].strip()].append(r)

    # ① 검색 상위에 자주 나오는 논문
    queries = [q for qs in GOLD_QUERIES.values() for q in qs.values()]
    queries += [q for qs in PATIENT_QUERIES.values() for q in qs]
    papers: dict = {}
    freq: Counter = Counter()
    best: dict[str, int] = {}
    for q in queries:
        ranked = ranked_gold(q, gold_pmids, papers)
        for rank, pm in enumerate(ranked[:TOP_N]):
            freq[pm] += 1
            best[pm] = min(best.get(pm, 999), rank)
        print(f"  검색 완료: {q[:60]}")
    in_db = [pm for pm in gold_pmids if pm in papers]   # DB에 있는 정답지 논문만 (검수해도 평가에 쓰이게)
    top = sorted(freq, key=lambda pm: (-freq[pm], best[pm]))[:PER_GROUP]

    # ② AI 신뢰도 '하'가 하나라도 있는 논문
    chosen = set(top)
    low = sorted(pm for pm in in_db if pm not in chosen and any(r["confidence"] == "하" for r in by_pmid[pm]))
    low = rng.sample(low, min(PER_GROUP, len(low)))
    chosen |= set(low)

    # ③ 무작위
    rest = sorted(pm for pm in in_db if pm not in chosen)
    rand = rng.sample(rest, min(PER_GROUP, len(rest)))

    picks = [("검색 상위", pm) for pm in top] + [("AI 자신없음", pm) for pm in low] + [("무작위", pm) for pm in rand]
    rng.shuffle(picks)   # 묶음 순서대로 보면 선입견이 생기므로 섞는다

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    review = OUT_DIR / "goldset_review_30.csv"
    key = OUT_DIR / "goldset_review_30_key.csv"
    with open(review, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["번호", "PMID", "제목", "초록(앞부분)", "PubMed 링크",
                    "P1 수면 점수", "P2 배회·공격성 점수", "P3 치료 정보 점수", "메모"])
        for i, (_, pm) in enumerate(picks, 1):
            p = papers[pm]
            abstract = (p.abstract or "").strip().replace("\n", " ")
            if len(abstract) > ABSTRACT_CHARS:
                abstract = abstract[:ABSTRACT_CHARS].rsplit(" ", 1)[0] + " …"
            w.writerow([i, pm, p.title, abstract, f"https://pubmed.ncbi.nlm.nih.gov/{pm}/", "", "", "", ""])
    with open(key, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["번호", "묶음", "pmid", "profile_id", "ai_label", "confidence", "reason"])
        for i, (group, pm) in enumerate(picks, 1):
            for r in sorted(by_pmid[pm], key=lambda r: r["profile_id"]):
                w.writerow([i, group, pm, r["profile_id"], r["label"], r["confidence"], r["reason"]])

    c = Counter(g for g, _ in picks)
    print(f"\n뽑은 논문 {len(picks)}편: " + " / ".join(f"{k} {v}" for k, v in c.items()))
    print(f"검수용 표: {review.relative_to(OUT_DIR.parents[3])}")
    print(f"AI 점수(검수자에게 주지 않음): {key.relative_to(OUT_DIR.parents[3])}")


if __name__ == "__main__":
    main()
