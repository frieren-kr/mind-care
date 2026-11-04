"""근거 등급 일괄 저장 배치 (담당: 김현서).

아직 등급을 판단하지 않은 논문을 evidence.classify()로 분류해서
paper_analysis의 study_type / evidence_level 두 칸에 저장한다. (주현님 save_evidence())

  get_papers_without_evidence()  — study_type이 빈 논문 고르기          [주현님 함수]
  classify()                     — 연구 유형 + 1~6 등급 (동물 연구는 animal / None)  [evidence.py]
  save_evidence()                — 두 칸만 저장. 요약 칸은 건드리지 않음  [주현님 함수]

화면용 A~D는 저장하지 않는다. API가 to_grade(evidence_level)로 그때그때 바꾼다.

실행 (backend/ 에서, Docker DB 켜고):
    python -m scripts.classify_papers --dry-run     # 저장 없이 분포만 확인
    python -m scripts.classify_papers               # 저장
여러 번 실행해도 안전하다 (이미 판단한 논문은 다시 고르지 않는다).

규칙(evidence.py)을 고친 뒤 이미 저장된 등급까지 다시 매기려면 --all:
    python -m scripts.classify_papers --all --dry-run   # 전체 논문 새 규칙 분포만 확인
    python -m scripts.classify_papers --all             # 전체 논문 다시 저장 (덮어쓰기)
※ 검색(search.py)은 검색할 때마다 classify()를 새로 돌리므로 규칙 변경이 바로 반영된다.
  --all은 DB에 저장된 등급(논문 상세 화면의 A~D 배지)을 새 규칙에 맞추는 용도다.
"""
import argparse
from collections import Counter

from app.ai.retrieval.evidence import LEVEL, classify, to_grade
from app.db.functions import get_papers_by_ids, get_papers_without_evidence, save_evidence, search_similar

CHUNK = 200


def main() -> None:
    ap = argparse.ArgumentParser(description="근거 등급 일괄 저장")
    ap.add_argument("--dry-run", action="store_true", help="저장하지 않고 분포만 출력")
    ap.add_argument("--all", action="store_true", help="이미 등급이 있는 논문까지 전부 다시 판단 (규칙 변경 후)")
    a = ap.parse_args()

    by_type: Counter = Counter()
    by_source: Counter = Counter()
    total_saved = 0

    if a.all:
        # 임베딩된 논문 전체를 가져온다 (DB 직접 조회 금지 규칙 → 큰 top_k로 search_similar, evidence_stats와 같은 방식)
        from app.ai.retrieval.embedding import MODEL_NAME, embed_query

        hits = search_similar(embed_query("dementia"), top_k=100_000, model_name=MODEL_NAME)
        ids = [h.paper_id for h in hits]
        items = []
        for i in range(0, len(ids), CHUNK):
            for p in get_papers_by_ids(ids[i:i + CHUNK]):
                ev = classify(p.publication_types, p.mesh_terms, p.title, p.abstract)
                by_type[ev.study_type] += 1
                by_source[ev.source] += 1
                items.append((p.paper_id, ev.study_type, ev.evidence_level))
        print(f"전체 다시 판단: {len(items)}편 (임베딩된 논문 기준)")
        if not a.dry_run:
            for i in range(0, len(items), CHUNK):
                total_saved += save_evidence(items[i:i + CHUNK]).saved
            print(f"저장(덮어쓰기): {total_saved}편")
    elif a.dry_run:
        # dry-run은 저장을 안 하므로 같은 논문이 계속 나온다 → 한 번에 크게 받아서 끝낸다
        papers = get_papers_without_evidence(limit=100_000)
        for p in papers:
            ev = classify(p.publication_types, p.mesh_terms, p.title, p.abstract)
            by_type[ev.study_type] += 1
            by_source[ev.source] += 1
        print(f"(dry-run) 등급 판단 대상: {len(papers)}편")
    else:
        while True:
            papers = get_papers_without_evidence(limit=CHUNK)
            if not papers:
                break
            items = []
            for p in papers:
                ev = classify(p.publication_types, p.mesh_terms, p.title, p.abstract)
                by_type[ev.study_type] += 1
                by_source[ev.source] += 1
                items.append((p.paper_id, ev.study_type, ev.evidence_level))
            result = save_evidence(items)
            total_saved += result.saved
            print(f"  {result.saved}/{result.total}편 저장 (누적 {total_saved})")
            if result.saved == 0:  # 저장이 안 되면 같은 논문이 또 나오므로 멈춘다
                print("  ⚠ 저장된 논문이 없어 멈춥니다. 위 경고(papers에 없는 paper_id 등)를 확인하세요.")
                break
        print(f"\n완료: {total_saved}편 저장")

    if not by_type:
        print("새로 판단할 논문이 없습니다. (이미 전부 등급이 있음)")
        return

    n = sum(by_type.values())
    print(f"\n{'study_type':<18} {'1~6':>4} {'A~D':>4} {'편수':>6} {'비율':>7}")
    for t in LEVEL:
        if by_type[t]:
            lv = LEVEL[t]
            print(f"{t:<18} {str(lv or '-'):>4} {str(to_grade(lv) or '-'):>4} {by_type[t]:>6} {by_type[t] / n:>6.1%}")
    print("\n판단 근거: " + " / ".join(f"{k} {v}편" for k, v in by_source.most_common()))


if __name__ == "__main__":
    main()
