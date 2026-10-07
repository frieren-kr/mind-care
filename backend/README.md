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

## 데이터베이스 실행 (Docker)

로컬에 PostgreSQL을 직접 설치하지 않아도 됩니다. `docker-compose.yml`이
pgvector가 들어 있는 PostgreSQL 17 이미지를 띄우고, **최초 기동 시
`app/db/migrations/` 안의 SQL을 파일명 순서대로(`001` → `002` → `003`) 자동 실행**해
테이블을 만들어 줍니다.

준비물: [Docker Desktop](https://www.docker.com/products/docker-desktop/) (설치 후 Engine이 running 상태여야 합니다)

```bash
cd backend
cp .env.example .env          # POSTGRES_PASSWORD 와 DATABASE_URL 비밀번호를 같은 값으로 채운다
docker compose up -d          # DB 기동 (최초 1회는 이미지 내려받기 때문에 몇 분 걸립니다)
docker compose ps             # STATUS가 "healthy"가 되면 준비 완료
```

`.env`는 커밋하지 않습니다. 비밀번호는 각자 원하는 값으로 정하면 되고,
`POSTGRES_PASSWORD`가 비어 있으면 컨테이너가 일부러 기동에 실패합니다(빈 비밀번호 방지).

### 스키마가 잘 만들어졌는지 확인

```bash
docker exec mindcare-db psql -U mindcare -d mindcare -c "\dt"          # 테이블 11개
docker exec mindcare-db psql -U mindcare -d mindcare -c "\dx"          # vector, uuid-ossp 확장
docker compose logs db                                                 # 초기화 로그
```

### 자주 쓰는 명령

```bash
docker compose stop           # 일시 중지
docker compose up -d          # 다시 시작
docker compose down           # 컨테이너 삭제 (데이터는 볼륨에 남는다)
docker compose down -v        # 데이터까지 삭제 → 다음 기동 때 001_init.sql 재실행
```

마이그레이션은 **볼륨이 비어 있을 때만** 실행됩니다. 이미 DB를 쓰고 있었다면 새 마이그레이션
(`002`, `003`)은 자동 반영되지 않으니 루트 [`README.md`](../README.md)의 직접 적용 안내를 따르세요.
스키마를 처음부터 다시 만들려면 `docker compose down -v`로 데이터를 지우고 올립니다.

### 문제가 생기면

- `docker: command not found` / `'docker'을(를) 인식할 수 없습니다` — Docker Desktop 설치 후
  기존에 열어둔 터미널은 PATH를 모릅니다. 터미널을 새로 열면 됩니다.
- `port 5432 already in use` — 로컬에 다른 PostgreSQL이 돌고 있는 경우입니다. 그것을 끄거나
  `docker-compose.yml`의 포트를 `"5433:5432"`로 바꾸고 `.env`의 `DATABASE_URL` 포트도 맞춥니다.

## 동작 확인 (PubMed 수집 → DB 저장)

DB가 떠 있는 상태에서:

```bash
python -m app.collectors.pubmed --days-back 7 --max-results 20 --dry-run   # 저장 없이 수집만
python -m app.collectors.pubmed --days-back 7 --max-results 20             # 수집 + DB 저장
```

같은 논문을 다시 수집해도 `(source, external_id)` UNIQUE 제약으로 중복 저장되지 않고
`skipped`로 집계됩니다.

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
