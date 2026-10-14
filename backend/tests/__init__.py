"""테스트에서는 서버 시작 때 임베딩 모델 예열(main.lifespan)을 끈다."""
import os

os.environ.setdefault("PRELOAD_EMBEDDING_MODEL", "0")
