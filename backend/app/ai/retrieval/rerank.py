"""Reranking — 검색 상위 후보를 '질문+논문을 같이 읽는' 모델로 다시 정렬 (담당: 김현서).

왜 필요한가
  bge-m3 임베딩은 질문과 논문을 '따로' 숫자로 바꾼 뒤 거리만 잰다 (빠르지만 대충 비교).
  그래서 '수면'이라는 주제는 잘 잡아도 "누구의 수면인지", "치료법인지 위험요인인지" 같은 세부를 놓친다.
  Reranker(Cross-Encoder)는 질문과 논문을 **한 번에 같이 읽고** 관련도를 매긴다 (느리지만 정확).
  → 임베딩으로 30편 정도를 빠르게 고른 뒤, 그 30편만 reranker로 다시 줄 세운다. (2단계 검색)

모델: BAAI/bge-reranker-v2-m3 (bge-m3와 같은 계열, 한국어·영어 모두 지원, 무료, 처음 실행 시 약 2.3GB 다운로드)

사용
    from app.ai.retrieval.rerank import rerank_scores
    scores = rerank_scores("sleep disturbance in mild dementia", ["제목. 초록", ...])   # 0~1, 클수록 관련

※ relevance_score(임베딩 유사도)는 바꾸지 않는다. reranker 점수는 '순서'를 정하는 데만 쓴다.
"""
import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

RERANK_MODEL_ID = "BAAI/bge-reranker-v2-m3"
RERANK_MAX_LENGTH = 512   # 질문+제목+초록을 최대 512토큰까지 읽음 (길면 CPU에서 느림)
RERANK_POOL = 30          # 임베딩 상위 몇 편을 다시 정렬할지


@lru_cache(maxsize=1)
def get_reranker():
    """reranker 모델을 한 번만 불러온다.

    RERANK_DEVICE(없으면 EMBEDDING_DEVICE) 환경변수로 장치를 고정할 수 있다 (예: cpu).
    맥에서 bge-m3와 reranker를 둘 다 GPU(MPS)에 올리면 메모리가 부족할 수 있다 → RERANK_DEVICE=cpu
    """
    from sentence_transformers import CrossEncoder  # 무거워서 필요할 때만 import

    device = os.environ.get("RERANK_DEVICE") or os.environ.get("EMBEDDING_DEVICE") or None
    model = CrossEncoder(RERANK_MODEL_ID, max_length=RERANK_MAX_LENGTH, device=device)
    logger.info("reranker 로드: %s (device=%s)", RERANK_MODEL_ID, model.device)
    return model


def rerank_scores(query: str, docs: list[str], batch_size: int = 4) -> list[float]:
    """(질문, 문서) 쌍마다 관련도 0~1. 입력 순서 그대로 돌려준다."""
    if not docs:
        return []
    import torch

    scores = get_reranker().predict(
        [(query, d) for d in docs],
        batch_size=batch_size,
        activation_fct=torch.nn.Sigmoid(),  # 원래 점수(logit)를 0~1로
        show_progress_bar=False,
    )
    return [float(s) for s in scores]
