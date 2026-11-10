"""골드셋 v2 만들기 — '환자 중심' 기준으로 AI 라벨을 고치고, 사람이 검수한 칸은 사람 점수로 덮어쓴다.

왜 필요한가
  석희님이 30편×3상황(90칸)을 직접 채점해 보니 AI 라벨과 34%만 일치했다.
  차이의 대부분(41칸)은 "간병인 본인 연구 / 행동 지침 없음"인 논문에
  AI는 1점, 사람은 0점을 준 경우였다. 팀은 환자 중심 논문만 추천하기로 했으므로
  사람 기준에 맞춰 AI 라벨을 고친다.

규칙 (AI 라벨에만 적용, reason 글자로 판단)
  ① 1점인데 간병인·보호자 본인 대상(간병인/보호자/가족 지원) → 0점
  ② 1점인데 "행동 지침 없음" 또는 "치료 정보 없음" → 0점
  ③ 2점인데 "간병인 본인" 대상 → 0점
  ※ 환자 대상 '프로토콜(결과 없음)'은 사람이 1~2점을 줘서 그대로 둔다.
  그다음 사람이 검수한 90칸은 사람 점수로 덮어쓴다.

실행 (backend 폴더에서):
  python -m scripts.make_goldset_v2            # v2 저장 + 30편 일치율 출력
  python -m scripts.make_goldset_v2 --dry-run  # 저장 없이 숫자만
"""
import argparse
import csv
import re
from collections import Counter
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1] / "app" / "ai" / "eval"
V1 = EVAL_DIR / "goldset_labels_901x3.csv"
V2 = EVAL_DIR / "goldset_labels_901x3_v2.csv"
HUMAN = EVAL_DIR / "review" / "goldset_review_30_human.csv"

CAREGIVER_RE = re.compile(r"간병인|보호자|가족 지원")
NO_ACTION_RE = re.compile(r"행동 지침 없음|치료 정보 없음")
CAREGIVER_SELF_RE = re.compile(r"간병인 본인")


def fix_label(label: int, reason: str) -> tuple[int, str]:
    """(고친 점수, 고친 이유 코드). 안 고치면 이유는 ''."""
    if label == 1 and CAREGIVER_RE.search(reason):
        return 0, "rule1_caregiver"
    if label == 1 and NO_ACTION_RE.search(reason):
        return 0, "rule2_no_action"
    if label == 2 and CAREGIVER_SELF_RE.search(reason):
        return 0, "rule3_caregiver_self"
    return label, ""


def load_human() -> dict[tuple[str, str], int]:
    with open(HUMAN, encoding="utf-8-sig") as f:
        return {(r["pmid"].strip(), r["profile_id"]): int(r["human_label"]) for r in csv.DictReader(f)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    human = load_human()
    with open(V1, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    changed = Counter()
    before_pairs, after_pairs = [], []
    for r in rows:
        key = (r["pmid"].strip(), r["profile_id"])
        v1 = int(r["label"])
        v2, why = fix_label(v1, r["reason"])
        if key in human:  # 사람 검수 칸: 규칙 효과를 먼저 재고, 사람 점수로 덮어쓴다
            before_pairs.append((human[key], v1))
            after_pairs.append((human[key], v2))
            if human[key] != v2:
                why = "human_review"
            v2 = human[key]
        if why:
            changed[why] += 1
        r["label_v1"] = v1
        r["label"] = v2
        r["change"] = why

    agree = lambda ps: sum(a == b for a, b in ps) / len(ps)
    print(f"사람 검수 {len(before_pairs)}칸 일치율: 규칙 전 {agree(before_pairs):.0%} → 규칙 후 {agree(after_pairs):.0%}")
    print("바뀐 칸:", dict(changed), f"(전체 {len(rows)}칸)")
    print("v1 분포:", dict(sorted(Counter(r['label_v1'] for r in rows).items())),
          " v2 분포:", dict(sorted(Counter(r['label'] for r in rows).items())))
    for p in ("P1", "P2", "P3"):
        c = Counter(r["label"] for r in rows if r["profile_id"] == p)
        print(f"  {p} v2: 0점 {c[0]} / 1점 {c[1]} / 2점 {c[2]}")

    if not args.dry_run:
        fields = list(rows[0].keys())
        with open(V2, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"저장: {V2.relative_to(EVAL_DIR.parents[2])}")


if __name__ == "__main__":
    main()
