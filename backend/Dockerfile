# 마인드 케어 백엔드 (FastAPI + 주기 배치). 빌드는 backend/ 에서:  docker build -t mindcare-backend .
# 키·비밀번호는 이미지에 넣지 않는다 — 실행할 때 --env-file .env(또는 compose의 env_file)로 넘긴다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # bge-m3(약 2GB) 캐시 위치. 이 경로를 볼륨으로 붙이면 컨테이너를 다시 만들어도 다시 받지 않는다.
    HF_HOME=/models/huggingface \
    EMBEDDING_DEVICE=cpu

WORKDIR /app

# torch를 CPU 전용 휠로 먼저 깐다 (로컬 .venv와 같은 2.14.1+cpu). 그래야 sentence-transformers가 GPU용(CUDA 수 GB)을 끌어오지 않는다.
RUN pip install torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# root가 아닌 사용자로 실행. 모델 캐시·로그·배치 상태 폴더는 미리 만들어 두어야 새 볼륨도 이 사용자 소유가 된다.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /models/huggingface /app/logs /app/state \
    && chown -R app:app /models /app/logs /app/state
USER app
VOLUME ["/models"]

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
