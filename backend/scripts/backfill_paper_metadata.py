"""이미 저장된 논문의 메타데이터를 PubMed에서 다시 받아 채운다 (1회성 백필).

002_add_paper_metadata.sql로 journal / doi / publication_types / mesh_terms 컬럼이
생겼지만, 그 전에 저장된 논문은 네 컬럼이 비어 있다. 이 스크립트가 그 논문들의
PMID로 efetch를 다시 호출해 메타데이터만 채운다.

save_papers()는 (source, external_id)가 겹치면 기존 행을 건드리지 않고 건너뛴다
(다른 파트가 전제로 쓰는 동작이라 바꾸지 않는다). 그래서 채워 넣는 일은
update_paper_metadata()가 따로 맡는다 — 제목·초록은 그대로 두고 메타데이터만 갱신한다.

실행 (backend/ 에서):
    python -m scripts.backfill_paper_metadata            # 실제 갱신
    python -m scripts.backfill_paper_metadata --dry-run  # 받아오기만 하고 DB는 그대로
"""
import argparse
import logging

from app.collectors.pubmed import SOURCE, fetch_papers_by_pmids
from app.db.functions import get_papers_missing_metadata, update_paper_metadata

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="논문 메타데이터 백필")
    parser.add_argument("--limit", type=int, default=500, help="한 번에 처리할 최대 논문 수 (기본 500)")
    parser.add_argument("--dry-run", action="store_true", help="DB를 갱신하지 않고 결과만 출력")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    targets = get_papers_missing_metadata(limit=args.limit, source=SOURCE)
    if not targets:
        print("메타데이터가 비어 있는 논문이 없습니다. (할 일 없음)")
        return

    pmids = [p.external_id for p in targets]
    print(f"대상 {len(pmids)}건 — PubMed에서 메타데이터를 다시 받아옵니다.")

    # 백필은 이미 저장된 논문을 채우는 일이라 초록 유무로 거르지 않는다.
    fetched = fetch_papers_by_pmids(pmids, require_abstract=False)
    print(f"받아온 논문: {len(fetched.papers)}건 / 못 찾은 PMID: {len(fetched.not_found)}건")

    if args.dry_run:
        for paper in fetched.papers[:5]:
            print(f"  [{paper.external_id}] {paper.journal} | doi={paper.doi} "
                  f"| types={paper.publication_types} | mesh={len(paper.mesh_terms)}개")
        print(f"\n--dry-run: DB를 갱신하지 않았습니다. (대상 {len(fetched.papers)}건)")
        return

    result = update_paper_metadata(fetched.papers)
    print(f"갱신 완료: 시도 {result.total}건 / 갱신 {result.updated}건")
    if result.not_found:
        print(f"papers에 없어 건너뛴 external_id {len(result.not_found)}건: {result.not_found}")
    if fetched.not_found:
        print(f"PubMed에서 못 찾은 PMID {len(fetched.not_found)}건: {fetched.not_found}")


if __name__ == "__main__":
    main()
