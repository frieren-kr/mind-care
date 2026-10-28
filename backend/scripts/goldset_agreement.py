"""골드셋 사람 검수 결과 ↔ AI 라벨 일치율 계산 (담당: 김현서).

검수자가 채운 goldset_review_30.csv(또는 같은 열의 .xlsx를 CSV로 저장한 것)와
goldset_review_30_key.csv(AI 점수)를 비교한다.

  - 일치율: 사람 점수 = AI 점수인 칸의 비율 (0/1/2 정확히 같음)
  - 관련 여부 일치율: '관련 있음(1·2)' vs '없음(0)'만 같은지 (P@k가 이 기준이라 더 중요)
  - Cohen's kappa: 우연히 맞을 확률을 뺀 일치도 (0.6 이상이면 '상당한 일치')
  - 어긋난 칸 목록

실행 (backend/ 에서, DB 필요 없음):
    python -m scripts.goldset_agreement
    python -m scripts.goldset_agreement --review 다른파일.csv
"""
import argparse
import csv
from collections import Counter
from pathlib import Path

REVIEW_DIR = Path(__file__).resolve().parents[1] / "app" / "ai" / "eval" / "review"
COLS = {"P1": "P1 수면 점수", "P2": "P2 배회·공격성 점수", "P3": "P3 치료 정보 점수"}


def kappa(pairs: list[tuple[int, int]]) -> float:
    n = len(pairs)
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", default=str(REVIEW_DIR / "goldset_review_30.csv"))
    ap.add_argument("--key", default=str(REVIEW_DIR / "goldset_review_30_key.csv"))
    a = ap.parse_args()

    with open(a.key, encoding="utf-8-sig") as f:
        ai = {(r["pmid"].strip(), r["profile_id"]): (int(r["ai_label"]), r["confidence"], r["묶음"])
              for r in csv.DictReader(f)}
    pairs, misses, by_group = [], [], Counter()
    with open(a.review, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            for pid, col in COLS.items():
                v = (r.get(col) or "").strip()
                if v not in {"0", "1", "2"}:
                    continue
                key = (r["PMID"].strip(), pid)
                if key not in ai:
                    continue
                h, (m, conf, group) = int(v), ai[key]
                pairs.append((h, m))
                by_group[(group, h == m)] += 1
                if h != m:
                    misses.append((r["번호"], pid, h, m, conf, r["제목"][:70]))

    if not pairs:
        print("채워진 점수가 없습니다. P1/P2/P3 점수 칸에 0·1·2를 적었는지 확인하세요.")
        return
    n = len(pairs)
    exact = sum(h == m for h, m in pairs) / n
    binary = sum((h > 0) == (m > 0) for h, m in pairs) / n
    print(f"\n검수한 칸: {n}칸")
    print(f"정확히 일치 (0/1/2): {exact:.1%}")
    print(f"관련 여부 일치 (0 vs 1·2): {binary:.1%}")
    print(f"Cohen's kappa: {kappa(pairs):.2f}  (0.4~0.6 보통 / 0.6~0.8 상당 / 0.8 이상 거의 완벽)")
    print("\n묶음별 정확 일치율")
    for g in ("검색 상위", "AI 자신없음", "무작위"):
        t = by_group[(g, True)] + by_group[(g, False)]
        if t:
            print(f"  {g:<8} {by_group[(g, True)] / t:.1%}  ({t}칸)")
    print("\n사람 → AI 점수 표 (행=사람, 열=AI)")
    cm = Counter(pairs)
    print("        AI0  AI1  AI2")
    for h in (0, 1, 2):
        print(f"  사람{h} " + "".join(f"{cm[(h, m)]:>5}" for m in (0, 1, 2)))
    if misses:
        print(f"\n어긋난 칸 {len(misses)}개")
        for no, pid, h, m, conf, title in misses:
            print(f"  #{no} {pid} 사람 {h} / AI {m} (신뢰도 {conf}) {title}")


if __name__ == "__main__":
    main()
