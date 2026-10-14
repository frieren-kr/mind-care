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

칸이 이미 있거나 이름이 이미 바뀌어 있으면 건너뛰므로 **여러 번 실행해도 안전합니다.** 적용됐는지 확인:

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

### ⚠️ 이미 DB를 띄워 둔 사람은 006도 직접 적용해야 합니다

위와 같은 이유로 `006_profile_summaries_index.sql`도 자동 반영되지 않습니다.
월간 요약 `profile_summaries`에 인덱스 2개를 추가하는 마이그레이션입니다.

- `idx_profile_summaries_user_period` — 개인화 피드가 월간 요약 1건을 고를 때 쓰는 조회 인덱스
  (001에는 PK(`id`) 인덱스만 있어서 `user_id`로 찾으면 테이블 전체를 읽습니다).
- `profile_summaries_user_period_key` — `(user_id, period_start, period_end)` UNIQUE.
  `save_profile_summary()`가 "같은 기간이면 갱신"(upsert)할 때 `ON CONFLICT` 대상이 됩니다.
  이게 없으면 같은 달 요약을 두 번 돌릴 때 행이 2개 쌓입니다.

`backend/` 에서 아래 명령으로 적용하세요.

```powershell
# Windows PowerShell
Get-Content app\db\migrations\006_profile_summaries_index.sql -Raw -Encoding UTF8 | docker exec -i mindcare-db psql -U mindcare -d mindcare
```

```bash
# macOS / Linux / Git Bash
docker exec -i mindcare-db psql -U mindcare -d mindcare < app/db/migrations/006_profile_summaries_index.sql
```

`CREATE INDEX IF NOT EXISTS`라서 **여러 번 실행해도 안전합니다.** 적용됐는지 확인:

```bash
docker exec mindcare-db psql -U mindcare -d mindcare -c "\d profile_summaries"
```

`idx_profile_summaries_user_period`와 `profile_summaries_user_period_key`가 보이면 완료입니다.

> UNIQUE 인덱스라서, **이미 같은 기간 요약이 중복으로 쌓여 있으면 생성이 실패합니다.**
> 그럴 때는 아래로 중복을 확인한 뒤 정리하고 다시 적용하세요.
>
> ```bash
> docker exec mindcare-db psql -U mindcare -d mindcare -c "SELECT user_id, period_start, period_end, count(*) FROM profile_summaries GROUP BY 1,2,3 HAVING count(*) > 1;"
> ```

### ⚠️ 이미 DB를 띄워 둔 사람은 007도 직접 적용해야 합니다

위와 같은 이유로 `007_paper_analysis_title_ko.sql`도 자동 반영되지 않습니다.
요약 테이블 `paper_analysis`에 `title_ko`(논문 제목의 한국어 번역) 칸을 추가하고,
`summary_comparison` 칸의 이름을 `summary_background`(연구 배경)로 바꾸는 마이그레이션입니다.
**적용하지 않으면 `/papers`, `/recommendations`와 `save_summary()`가 "column ... does not exist"로 실패합니다.**

`backend/` 에서 아래 명령으로 적용하세요.

```powershell
# Windows PowerShell
Get-Content app\db\migrations\007_paper_analysis_title_ko.sql -Raw -Encoding UTF8 | docker exec -i mindcare-db psql -U mindcare -d mindcare
```

```bash
# macOS / Linux / Git Bash
docker exec -i mindcare-db psql -U mindcare -d mindcare < app/db/migrations/007_paper_analysis_title_ko.sql
```

칸이 이미 있거나 이름이 이미 바뀌어 있으면 건너뛰므로 **여러 번 실행해도 안전합니다.** 적용됐는지 확인:

```bash
docker exec mindcare-db psql -U mindcare -d mindcare -c "\d paper_analysis"
```

`title_ko`와 `summary_background` 컬럼이 보이고 `summary_comparison`이 없으면 완료입니다. (기존 요약·등급 데이터는 건드리지 않습니다.)

### ⚠️ 이미 DB를 띄워 둔 사람은 008도 직접 적용해야 합니다

위와 같은 이유로 `008_guideline_embedding.sql`도 자동 반영되지 않습니다.
가이드라인 테이블 `guideline_knowledge`를 **"가이드라인 문서의 조각 + 임베딩"** 으로 넓히는 마이그레이션입니다.

- 칸 추가: `doc_title`, `section`, `chunk_index`, `source_url`, `published_year`, `language`,
  `embedding`(`vector(1024)`), `embedding_model`, `created_at`
- `(source_url, chunk_index)` UNIQUE 인덱스 — 같은 조각을 다시 저장하면 갱신됩니다.
- `embedding` HNSW 인덱스(코사인) — 논문 임베딩과 같은 방식입니다.

`backend/` 에서 아래 명령으로 적용하세요.

```powershell
# Windows PowerShell
Get-Content app\db\migrations\008_guideline_embedding.sql -Raw -Encoding UTF8 | docker exec -i mindcare-db psql -U mindcare -d mindcare
```

```bash
# macOS / Linux / Git Bash
docker exec -i mindcare-db psql -U mindcare -d mindcare < app/db/migrations/008_guideline_embedding.sql
```

모든 문장이 `IF NOT EXISTS`라서 **여러 번 실행해도 안전합니다.** 적용됐는지 확인:

```bash
docker exec mindcare-db psql -U mindcare -d mindcare -c "\d guideline_knowledge"
```

`doc_title`부터 `created_at`까지 새 칸과 `guideline_knowledge_source_chunk_key`, `idx_guideline_knowledge_hnsw`
인덱스가 보이면 완료입니다. (논문·요약 데이터는 건드리지 않습니다.)

> `guideline_knowledge`에 이미 행이 있으면 "기존 행이 있어 NOT NULL 칸을 추가할 수 없습니다"라며 멈춥니다.
> 지금까지 이 테이블에 저장하는 기능이 없었으므로 보통은 비어 있습니다.

### 팀원 데이터 맞추기 (팀 기준 논문 1,000편 + 임베딩)

팀은 **같은 논문 1,000편 + bge-m3 임베딩 1,000개**를 기준 데이터로 공유합니다.
논문 덤프는 용량이 커서 **레포에 넣지 않고 팀 단톡(카톡)에서 받습니다**
(`mindcare_papers_bge-m3.dump`, custom 형식·데이터만).

> 같은 덤프로 복원하면 모든 팀원의 `papers.id`(= AI가 쓰는 `paper_id`)가 **똑같아집니다.**
> 그래서 임베딩·요약·검색 결과를 팀원끼리 그대로 주고받을 수 있습니다.

1. 팀 단톡(카톡)에서 `mindcare_papers_bge-m3.dump`를 받습니다. (레포 밖, 예: 바탕화면)
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

#### 요약·근거 등급 데이터 받기 (`paper_analysis`)

논문별 **쉬운 3칸 요약 + 한국어 제목(`title_ko`) + 근거 등급**도 덤프로 공유합니다
(`paper_analysis_YYYYMMDD.sql`, 일반 SQL 형식·`paper_analysis` 데이터만, 팀 단톡(카톡)에서 받습니다).
각자 요약 배치를 돌리면 LLM 비용이 들고 문장도 사람마다 달라지므로, 이 덤프로 맞춥니다.

> **위의 논문 덤프를 먼저 복원한 상태여야 합니다.** `paper_analysis.paper_id`가 `papers.id`를 가리키기 때문에,
> 논문 `id`가 다르면 복원이 실패합니다. 또 논문 덤프 복원(`TRUNCATE papers CASCADE`)을 다시 하면
> `paper_analysis`도 같이 비워지므로, **순서는 항상 논문 덤프 → 이 덤프**입니다.

1. `007_paper_analysis_title_ko.sql`을 적용합니다 (위의 "007도 직접 적용해야 합니다" 참고. 이미 했으면 건너뜁니다).
2. 기존 `paper_analysis`를 비우고 덤프를 복원합니다. 한글이 깨지지 않게 **`docker cp`로 컨테이너에 넣고
   그 안에서 `psql -f`** 로 실행하세요 (PowerShell 파이프로 넘기면 한글이 `?`로 바뀔 수 있습니다).

```powershell
# Windows PowerShell — 덤프 경로와 날짜는 각자 받은 파일로 바꾸세요 (macOS / Linux도 경로만 바꾸면 같습니다)
docker cp "$HOME\Desktop\paper_analysis_20261014.sql" mindcare-db:/tmp/paper_analysis.sql
docker exec mindcare-db psql -U mindcare -d mindcare -v ON_ERROR_STOP=1 --single-transaction -c "TRUNCATE paper_analysis;" -f /tmp/paper_analysis.sql
docker exec mindcare-db rm /tmp/paper_analysis.sql
```

`--single-transaction`이라 중간에 실패하면 비우기까지 전부 되돌아갑니다 (기존 데이터가 그대로 남습니다).
Git Bash에서는 `/tmp/...` 경로가 Windows 경로로 바뀌므로 명령 앞에 `MSYS_NO_PATHCONV=1`을 붙이세요.

3. 확인:

```powershell
docker exec mindcare-db psql -U mindcare -d mindcare -c "SELECT count(*) AS rows, count(summary_finding) AS summarized, count(title_ko) AS titled, count(study_type) AS graded FROM paper_analysis;"
```

`paper_analysis_20261014.sql` 기준으로 `rows 1000 / summarized 998 / titled 998 / graded 1000`이면 완료입니다.
(요약이 없는 2편은 자동 검사를 통과하지 못해 저장하지 않은 논문입니다.)

### 개발용 샘플 사용자 넣기

피드·추천·챗봇을 개발할 때 쓸 예시 간병인 4명(환자 5명)을 넣습니다. 실제 사람 정보가 아닙니다.
006까지 적용한 DB가 떠 있는 상태로, 가상환경을 켜고 `backend/` 에서 실행하세요.

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
