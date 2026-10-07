"""DB 논문을 bge-m3로 임베딩해서 paper_embeddings에 저장한다. (담당: 김현서)

흐름 (CHUNK편씩 반복):
  ① get_papers_without_embedding()  — 아직 bge-m3 임베딩이 없는 논문 가져오기
  ② embed_documents()               — 제목+초록 → 1024차원 벡터
  ③ save_embeddings()               — DB에 저장 (요약 테이블은 건드리지 않음)

이미 임베딩된 논문은 ①에서 빠지므로, 중간에 끊겨도 다시 실행하면 남은 것만 이어서 한다.
수집기 다음에 실행한다 (나중에 주간 스케줄러: 수집기 → 이 스크립트).

실행 (backend/ 에서, Docker DB가 떠 있어야 함):
    python -m scripts.embed_papers              # 남은 논문 전부
    python -m scripts.embed_papers --limit 50   # 50편만 (테스트용)
    python -m scripts.embed_papers --cpu        # GPU 메모리 부족 시 CPU로
"""
import argparse
import logging
import os
import time

from app.db.functions import get_papers_without_embedding, save_embeddings

logger = logging.getLogger(__name__)

CHUNK = 16  # 한 번에 가져와서 임베딩·저장할 논문 수 (작을수록 자주 저장 → 끊겨도 손해 적음)


def main() -> None:
    ap = argparse.ArgumentParser(description="논문 임베딩 → paper_embeddings 저장")
    ap.add_argument("--limit", type=int, default=None, help="최대 처리 편수 (기본: 전부)")
    ap.add_argument("--batch", type=int, default=4, help="모델에 한 번에 넣는 개수 (메모리 부족하면 2)")
    ap.add_argument("--cpu", action="store_true", help="GPU(MPS) 대신 CPU 사용")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.cpu:
        os.environ["EMBEDDING_DEVICE"] = "cpu"

    # embedding 모듈은 EMBEDDING_DEVICE를 정한 뒤에 import (모델 로드 시점에 읽음)
    from app.ai.retrieval.embedding import MODEL_NAME, embed_documents, free_memory, paper_text

    # 전체 남은 편수 (진행률 표시용)
    remaining = len(get_papers_without_embedding(MODEL_NAME, limit=100_000))
    total = min(remaining, args.limit) if args.limit else remaining
    if total == 0:
        print("임베딩할 논문이 없어요. (이미 전부 완료됐거나 papers가 비어 있음)")
        return
    print(f"임베딩 대상: {total}편 (모델: {MODEL_NAME}) — 처음 실행이면 모델 다운로드(약 2GB)부터 해요")

    from tqdm import tqdm

    done, t0 = 0, time.time()
    with tqdm(total=total, unit="편", desc="임베딩") as bar:
        while done < total:
            n = min(CHUNK, total - done)
            papers = get_papers_without_embedding(MODEL_NAME, limit=n)       # ①
            if not papers:
                break
            vectors = embed_documents(                                        # ②
                [paper_text(p.title, p.abstract) for p in papers], batch_size=args.batch
            )
            result = save_embeddings(                                         # ③
                list(zip([p.id for p in papers], vectors)), MODEL_NAME
            )
            if result.saved == 0:  # 저장이 하나도 안 되면 같은 논문만 계속 나오므로 멈춘다
                logger.error("저장된 임베딩이 0건이라 중단합니다: %s", result)
                break
            done += result.saved
            bar.update(result.saved)
            free_memory()

    print(f"\n완료: {done}편 저장 ({time.time() - t0:.0f}초)")


if __name__ == "__main__":
    main()
