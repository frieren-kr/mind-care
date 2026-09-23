# Mind Care 백엔드

FastAPI 기반 백엔드.

## 개발 환경 준비

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows (PowerShell: .venv\Scripts\Activate.ps1)
pip install -r requirements.txt
cp .env.example .env          # 값 채우기
```

## 실행

```bash
uvicorn main:app --reload     # http://127.0.0.1:8000, 문서: /docs
```

## 구조

- `main.py` — FastAPI 앱 진입점
- `app/config.py` — 환경 변수
- `app/db/functions.py` — **데이터 접근 함수 (팀 공용 계약, 박주현 담당)**
- `app/schemas.py` — 함수 입출력 스키마
- `app/collectors/pubmed.py` — PubMed 수집 (주 1회 배치)
