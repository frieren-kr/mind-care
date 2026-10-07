"""bge-m3 임베딩 (담당: 김현서).

글 → 1024차원 벡터. 논문 저장(embed_papers)과 검색(search)이 **같은 함수**를 써야
같은 벡터 공간에서 비교된다. 모델은 처음 호출할 때 한 번만 불러서 재사용한다.

- 논문: embed_documents([paper_text(title, abstract), ...])
- 질문: embed_query("엄마가 밤에 자꾸 깨요")

bge-m3는 질문/문서 앞에 prefix가 필요 없다(e5와 다름). 결과는 정규화(길이 1)되어
코사인 유사도 = 내적이 된다. 모델 비교 근거: 노션 1주차 「임베딩 모델 비교」.
"""
import gc
import logging
import os
from functools import lru_cache

from app.config import settings

logger = logging.getLogger(__name__)

HF_MODEL_ID = "BAAI/bge-m3"          # Hugging Face에서 받을 모델
MODEL_NAME = settings.embedding_model  # DB(paper_embeddings.model_name)에 기록할 이름 = "bge-m3"
EMBEDDING_DIM = settings.embedding_dim # 1024 (003 마이그레이션과 같아야 함)
MAX_SEQ_LENGTH = 512                   # 제목+초록은 대부분 512토큰 안. 길게 두면 CPU에서 매우 느림


@lru_cache(maxsize=1)
def get_model():
    """bge-m3를 한 번만 불러서 돌려준다. (처음 실행 시 약 2GB 다운로드)

    EMBEDDING_DEVICE 환경변수로 장치를 고정할 수 있다 (예: cpu). 없으면 자동(mps/cuda/cpu).
    """
    from sentence_transformers import SentenceTransformer  # 무거워서 필요할 때만 import

    device = os.environ.get("EMBEDDING_DEVICE") or None
    model = SentenceTransformer(HF_MODEL_ID, device=device)
    model.max_seq_length = MAX_SEQ_LENGTH
    logger.info("임베딩 모델 로드: %s (device=%s)", HF_MODEL_ID, model.device)
    return model


def paper_text(title: str, abstract: str | None) -> str:
    """논문을 임베딩할 때 쓰는 글 = 제목 + 초록. (검색 품질 실험 v2와 같은 방식)"""
    title = (title or "").strip()
    abstract = (abstract or "").strip()
    return f"{title}. {abstract}" if abstract else title


def _encode(texts: list[str], batch_size: int) -> list[list[float]]:
    vectors = get_model().encode(
        texts,
        normalize_embeddings=True,
        batch_size=batch_size,
        show_progress_bar=False,
    )
    out = [v.tolist() for v in vectors]
    for v in out:
        if len(v) != EMBEDDING_DIM:
            raise ValueError(f"임베딩 차원 {len(v)} ≠ {EMBEDDING_DIM}. 모델 설정을 확인하세요.")
    return out


def embed_documents(texts: list[str], batch_size: int = 4) -> list[list[float]]:
    """논문 여러 개 → 벡터 여러 개. 입력 순서 그대로 돌려준다."""
    if not texts:
        return []
    return _encode(texts, batch_size)


def embed_query(text: str) -> list[float]:
    """질문 하나 → 벡터 하나. (search()에서 사용)"""
    return _encode([text], batch_size=1)[0]


def free_memory() -> None:
    """배치 사이에 GPU(MPS) 메모리를 비운다. 인텔/M 맥에서 메모리 부족 에러 예방용."""
    gc.collect()
    try:
        import torch

        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:  # torch가 없거나 MPS가 없으면 무시
        pass
