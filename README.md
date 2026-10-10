# 마인드 케어 (Mind Care)

치매·경도인지장애 가족 간병인을 위한 연구 요약·개인화 앱. PubMed 논문을 주기적으로 수집해 LLM + RAG로 관련성·근거를 분류·요약하고, 간병인에게 쉬운 말로 전달하며 개인화 챗봇으로 답합니다. (팀 온기억, 산학프로젝트2 Fall 2026)

## 기술 스택

- **백엔드**: Python + FastAPI
- **프론트엔드**: React Native + Expo
- **DB**: PostgreSQL + pgvector
  - 논문 관련성은 수집 단계 키워드 필터(전역 관련 여부 보장) + `feed_items.relevance_score`(사용자별 관련도) 2단계로 처리하며, 논문 테이블에 전역 관련성 플래그는 두지 않습니다.
- **AI**: 범용 LLM API(GPT/Gemini) + RAG + Embedding
- **논문 수집**: PubMed(NCBI E-utilities) API — 주 1회 배치

## 빠른 시작

DB는 Docker로 띄웁니다. 로컬에 PostgreSQL을 설치할 필요가 없고, 최초 기동 시
`app/db/migrations/` 안의 SQL이 파일명 순서대로(`001` → `002` → `003` → …) 자동 적용됩니다.
(준비물: Docker Desktop)

```bash
cd backend
cp .env.example .env          # POSTGRES_PASSWORD / DATABASE_URL 채우기 (.env는 커밋 금지)
docker compose up -d          # PostgreSQL 17 + pgvector 기동
docker compose ps             # STATUS가 "healthy"면 준비 완료
```

### ⚠️ 이미 DB를 띄워 둔 사람은 002를 직접 적용해야 합니다

마이그레이션 자동 실행은 **DB를 처음 만들 때(볼륨이 비어 있을 때) 한 번만** 일어납니다.
이미 `docker compose up -d`로 DB를 쓰고 있었다면 `002_add_paper_metadata.sql`
(논문 메타데이터 컬럼: `journal` / `doi` / `publication_types` / `mesh_terms`)이
자동으로 반영되지 않으므로, `backend/` 에서 아래 명령으로 직접 적용하세요.

```powershell
# Windows PowerShell
Get-Content app\db\migrations\002_add_paper_metadata.sql -Raw -Encoding UTF8 | docker exec -i mindcare-db psql -U mindcare -d mindcare
```

```bash
# macOS / Linux / Git Bash
docker exec -i mindcare-db psql -U mindcare -d mindcare < app/db/migrations/002_add_paper_metadata.sql
```

`ADD COLUMN IF NOT EXISTS`라서 **여러 번 실행해도 안전합니다.** 적용됐는지 확인:

```bash
docker exec mindcare-db psql -U mindcare -d mindcare -c "\d papers"
```

`journal` / `doi` / `publication_types` / `mesh_terms` 네 컬럼이 보이면 완료입니다.
(DB를 처음부터 다시 만들어도 됩니다 — `docker compose down -v` 후 `up -d`. 단, **저장된 논문 데이터가 모두 지워집니다.**)

### ⚠️ 이미 DB를 띄워 둔 사람은 003도 직접 적용해야 합니다

위와 같은 이유로 `003_embedding_dim_1024.sql`도 자동 반영되지 않습니다.
임베딩 모델을 **bge-m3(1024차원)**로 확정해서 `paper_embeddings.embedding`을
`vector(768)` → `vector(1024)`로 바꾸고, 인덱스를 IVFFlat → **HNSW**로 교체하는 마이그레이션입니다.
`backend/` 에서 아래 명령으로 적용하세요.

```powershell
# Windows PowerShell
Get-Content app\db\migrations\003_embedding_dim_1024.sql -Raw -Encoding UTF8 | docker exec -i mindcare-db psql -U mindcare -d mindcare
```

```bash
# macOS / Linux / Git Bash
docker exec -i mindcare-db psql -U mindcare -d mindcare < app/db/migrations/003_embedding_dim_1024.sql
```

**여러 번 실행해도 안전합니다** (이미 1024면 차원 변경 단계를 건너뜁니다). 적용됐는지 확인:

```bash
docker exec mindcare-db psql -U mindcare -d mindcare -c "\d paper_embeddings"
```

`embedding | vector(1024)` 와 `idx_paper_embeddings_hnsw` 가 보이면 완료입니다.

> 저장해 둔 임베딩이 있었다면 **지워집니다** — 768차원 벡터를 1024차원으로 바꿀 방법이 없기 때문입니다.
> (몇 건을 지웠는지 실행할 때 NOTICE로 알려 줍니다. 논문 `papers`과 요약 `paper_analysis`는 그대로입니다.)
> bge-m3로 다시 만들면 됩니다.

`.env`도 함께 맞춰 주세요 (`.env.example` 참고):

```
EMBEDDING_MODEL=bge-m3
EMBEDDING_DIM=1024
```

### ⚠️ 이미 DB를 띄워 둔 사람은 004도 직접 적용해야 합니다

위와 같은 이유로 `004_patient_management.sql`도 자동 반영되지 않습니다.
`users`에 `phone_number` 컬럼을 추가하고, 환자 관리 테이블 4개
(`clinical_assessments` / `safety_events` / `medications` / `medical_visits`)를 만드는 마이그레이션입니다.
`backend/` 에서 아래 명령으로 적용하세요.

```powershell
# Windows PowerShell
Get-Content app\db\migrations\004_patient_management.sql -Raw -Encoding UTF8 | docker exec -i mindcare-db psql -U mindcare -d mindcare
```

```bash
# macOS / Linux / Git Bash
docker exec -i mindcare-db psql -U mindcare -d mindcare < app/db/migrations/004_patient_management.sql
```

`ADD COLUMN IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`라서
**여러 번 실행해도 안전합니다** (이미 있으면 NOTICE만 남기고 건너뜁니다). 적용됐는지 확인:

```bash
docker exec mindcare-db psql -U mindcare -d mindcare -c "\d clinical_assessments"
```

`clinical_assessments` / `safety_events` / `medications` / `medical_visits` 네 테이블과
`users`의 `phone_number` 컬럼이 보이면 완료입니다.
(논문 `papers`·임베딩 `paper_embeddings` 데이터는 건드리지 않습니다.)

### ⚠️ 이미 DB를 띄워 둔 사람은 005도 직접 적용해야 합니다

위와 같은 이유로 `005_medical_visits_detail.sql`도 자동 반영되지 않습니다.
병원 방문 기록 `medical_visits`에 진료 내용 컬럼 7개
(`visit_reason` / `diagnosis` / `treatment_content` / `test_summary` / `medication_change` /
`doctor_note` / `follow_up_plan`)를 추가하고, 조회용 인덱스 2개
(`idx_patient_profiles_caregiver` / `idx_care_logs_patient_time`)를 만드는 마이그레이션입니다.
**004를 먼저 적용한 뒤** `backend/` 에서 아래 명령으로 적용하세요.

```powershell
# Windows PowerShell
Get-Content app\db\migrations\005_medical_visits_detail.sql -Raw -Encoding UTF8 | docker exec -i mindcare-db psql -U mindcare -d mindcare
```

```bash
# macOS / Linux / Git Bash
docker exec -i mindcare-db psql -U mindcare -d mindcare < app/db/migrations/005_medical_visits_detail.sql
```

`ADD COLUMN IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`라서
**여러 번 실행해도 안전합니다** (이미 있으면 NOTICE만 남기고 건너뜁니다). 적용됐는지 확인:

```bash
docker exec mindcare-db psql -U mindcare -d mindcare -c "\d medical_visits"
```

`visit_reason`부터 `follow_up_plan`까지 일곱 컬럼이 보이면 완료입니다.
(논문 `papers`·임베딩 `paper_embeddings` 데이터는 건드리지 않습니다.)

### 팀원 데이터 맞추기 (팀 기준 논문 1,000편 + 임베딩)

팀은 **같은 논문 1,000편 + bge-m3 임베딩 1,000개**를 기준 데이터로 공유합니다.
논문 덤프는 용량이 커서 **레포에 넣지 않고 노션에서 받습니다**
(`mindcare_papers_bge-m3.dump`, custom 형식·데이터만).

> 같은 덤프로 복원하면 모든 팀원의 `papers.id`(= AI가 쓰는 `paper_id`)가 **똑같아집니다.**
> 그래서 임베딩·요약·검색 결과를 팀원끼리 그대로 주고받을 수 있습니다.

1. 노션에서 `mindcare_papers_bge-m3.dump`를 받습니다. (레포 밖, 예: 바탕화면)
2. DB를 띄웁니다: `docker compose up -d` (STATUS가 healthy인지 `docker compose ps`로 확인).
3. 기존 논문을 비우고 덤프를 복원합니다. PowerShell은 `<` 입력 리다이렉트가 안 되므로
   **`docker cp`로 컨테이너에 넣고 그 안에서 `pg_restore`** 하세요.

```powershell
# Windows PowerShell — 덤프 경로는 각자 받은 위치로 바꾸세요
docker exec mindcare-db psql -U mindcare -d mindcare -c "TRUNCATE papers CASCADE;"
docker cp "$HOME\Desktop\mindcare_papers_bge-m3.dump" mindcare-db:/tmp/restore.dump
docker exec mindcare-db pg_restore --data-only --single-transaction -U mindcare -d mindcare /tmp/restore.dump
docker exec mindcare-db rm /tmp/restore.dump
```

`--single-transaction`이라 중간에 실패하면 전부 되돌아갑니다. 확인:

```powershell
docker exec mindcare-db psql -U mindcare -d mindcare -c "SELECT (SELECT count(*) FROM papers) AS papers, (SELECT count(*) FROM paper_embeddings) AS embeddings;"
```

`papers 1000 / embeddings 1000`이면 완료입니다.
(덤프 파일은 `.env`처럼 **레포에 커밋하지 않습니다.**)

### 개발용 샘플 사용자 넣기

피드·추천·챗봇을 개발할 때 쓸 예시 간병인 4명(환자 5명)을 넣습니다. 실제 사람 정보가 아닙니다.
005까지 적용한 DB가 떠 있는 상태로, 가상환경을 켜고 `backend/` 에서 실행하세요.

```bash
python -m scripts.seed_dev_profiles   # 예시 프로필 적재 (이미 있으면 건너뜀 — 여러 번 실행해도 안전)
python -m scripts.list_users          # user_id / patient_id 확인
```

`list_users`가 보여 주는 `user_id`로 `/feed/{user_id}` 같은 주소를 호출해 볼 수 있습니다.
(`user_id`는 넣을 때마다 새로 만들어져서 **팀원마다 다릅니다.** 다시 넣으려면 `--reset`.)

백엔드 실행:

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1    # Windows PowerShell
pip install -r requirements.txt
uvicorn main:app --reload     # http://127.0.0.1:8000
```

서버는 `backend/` 폴더에서, DB(`docker compose up -d`)가 떠 있는 상태로 실행합니다.
띄운 뒤 브라우저에서 **Swagger 문서 http://127.0.0.1:8000/docs** 를 열면 API 주소와 응답 예시를 보고
바로 호출해 볼 수 있습니다.

| 주소 | 설명 |
|---|---|
| `GET /papers?limit=20&offset=0` | 논문 목록 (최신 발행일 순, `limit` 최대 100) |
| `GET /papers/{paper_id}` | 논문 상세 + 요약 (요약이 아직 없으면 `null`) |

프론트엔드 실행:

```bash
cd frontend
npm install
npm start
```

DB 확인·재생성·문제 해결 등 자세한 내용은 [`backend/README.md`](backend/README.md)를 참고하세요.

## 팀 협업 규칙 (핵심)

- 작업은 `develop` 브랜치에 push한다.
- `main`에는 직접 push하지 않는다 (PR + 팀장 승인 후 병합).
- 자기 담당 폴더만 수정한다. 다른 영역은 담당자에게 먼저 확인한다.
- `.env` / API 키 / 비밀번호는 커밋하지 않는다.
- AI 도구가 알아서 커밋·push하지 않게 한다 (명시적으로 요청할 때만).

자세한 규칙은 [`AGENTS.md`](AGENTS.md)를 참고하세요.

> **사용하는 AI 도구가 `AGENTS.md`를 읽는지 확인해주세요.**
> Gemini CLI는 설정에서 `AGENTS.md`를 지정해야 합니다.

## 폴더 구조

```
mind-care/
├─ backend/     FastAPI 백엔드
│  ├─ main.py           앱 진입점
│  ├─ app/
│  │  ├─ config.py      환경 변수
│  │  ├─ schemas.py     데이터 접근 함수 입출력 스키마
│  │  ├─ api/           API 라우터 (기능별 파일: papers.py …)
│  │  ├─ db/            DB 접근 함수 계층 (팀 공용 관문) + migrations/
│  │  └─ collectors/    PubMed 수집
│  └─ requirements.txt
├─ frontend/    React Native(Expo) 앱
└─ AGENTS.md    아키텍처·팀 협업 규칙·데이터 접근 계약 (모든 AI 도구 공통)
```

자세한 개발 환경 설정과 실행 방법은 [`backend/README.md`](backend/README.md), 아키텍처·팀 규칙은 [`AGENTS.md`](AGENTS.md)를 참고하세요.
