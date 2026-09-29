"""라벨링 후보 논문을 PMID 목록 파일로 적재한다.

정확도 측정(라벨링)에 쓸 논문은 AI 담당과 합의한 PMID 목록으로 고정되어 있다.
검색(esearch) 결과에 의존하면 매번 목록이 달라지므로, PMID를 직접 주는
fetch_papers_by_pmids()로 받아 save_papers()로 저장한다.

★ 초록 없는 논문 처리 ★
수집기는 기본적으로 초록 없는 논문을 버리지만, 라벨링 후보는 빠지면 안 되므로
이 스크립트는 그런 PMID를 조용히 버리지 않고 목록으로 출력한다. 저장 여부는
사람이 정한다 — 저장하려면 --include-no-abstract 를 붙여 다시 실행한다.

실행 (backend/ 에서):
    python -m scripts.seed_labeling_papers --dry-run
    python -m scripts.seed_labeling_papers
    python -m scripts.seed_labeling_papers --include-no-abstract
    python -m scripts.seed_labeling_papers --file app/db/seed/labeling_candidates.txt
"""
import argparse
import logging
import re
from pathlib import Path

from app.collectors.pubmed import fetch_papers_by_pmids
from app.db.functions import save_papers

logger = logging.getLogger(__name__)

SEED_DIR = Path(__file__).resolve().parents[1] / "app" / "db" / "seed"
_PMID_PATTERN = re.compile(r"\d{4,9}")


def find_seed_file(explicit: str | None = None) -> Path:
    """PMID 목록 파일을 찾는다. --file로 직접 줄 수도 있다."""
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            raise SystemExit(f"파일을 찾을 수 없습니다: {path}")
        return path

    candidates = sorted(
        p for p in SEED_DIR.glob("*")
        if p.is_file() and p.suffix.lower() in {".txt", ".csv", ".tsv"} and p.name != "README.md"
    )
    if not candidates:
        raise SystemExit(
            f"PMID 목록 파일이 없습니다: {SEED_DIR}\n"
            f"  (형식은 {SEED_DIR / 'README.md'} 참고. --file 로 다른 경로를 줄 수도 있습니다.)"
        )
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        raise SystemExit(f"목록 파일이 여러 개입니다 ({names}). --file 로 하나를 지정하세요.")
    return candidates[0]


def read_pmids(path: Path) -> list[str]:
    """파일에서 PMID를 읽는다.

    한 줄에 하나가 기본이고, 쉼표/공백으로 나열해도 된다.
    '#'으로 시작하는 줄과 숫자가 아닌 토큰(제목 메모 등)은 무시한다.
    중복은 제거하고 파일에 적힌 순서를 유지한다.
    """
    pmids: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        pmids.extend(_PMID_PATTERN.findall(line))
    return list(dict.fromkeys(pmids))


def main() -> None:
    parser = argparse.ArgumentParser(description="라벨링 후보 논문 적재")
    parser.add_argument("--file", default=None, help="PMID 목록 파일 경로 (기본: app/db/seed/ 안의 파일)")
    parser.add_argument("--dry-run", action="store_true", help="DB에 저장하지 않고 결과만 출력")
    parser.add_argument(
        "--include-no-abstract",
        action="store_true",
        help="초록이 없는 논문도 저장한다 (기본은 저장하지 않고 PMID만 보고)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    path = find_seed_file(args.file)
    pmids = read_pmids(path)
    print(f"목록 파일: {path}")
    print(f"PMID {len(pmids)}건 읽음")
    if not pmids:
        raise SystemExit("목록이 비어 있습니다.")

    fetched = fetch_papers_by_pmids(pmids, require_abstract=not args.include_no_abstract)
    print(f"PubMed에서 받아온 논문: {len(fetched.papers)}건")

    # ★ 빠진 논문은 조용히 넘어가지 않고 반드시 눈에 보이게 출력한다.
    if fetched.missing_abstract:
        print(f"\n[초록 없음 — 저장하지 않음] {len(fetched.missing_abstract)}건")
        for pmid in fetched.missing_abstract:
            print(f"  PMID {pmid}  https://pubmed.ncbi.nlm.nih.gov/{pmid}/")
        print("  → 이 논문들도 저장하려면 --include-no-abstract 로 다시 실행하세요.")
    if fetched.not_found:
        print(f"\n[PubMed에서 찾지 못함] {len(fetched.not_found)}건: {', '.join(fetched.not_found)}")

    if args.dry_run:
        print(f"\n--dry-run: 저장하지 않았습니다. (저장 대상 {len(fetched.papers)}건)")
        return

    result = save_papers(fetched.papers)
    print(
        f"\n저장 완료: 시도 {result.total}건 / 신규 {result.inserted}건 / "
        f"이미 있어서 건너뜀 {result.skipped}건"
    )


if __name__ == "__main__":
    main()
