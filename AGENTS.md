# AGENTS.md

이 저장소에서 작업하는 **모든 팀원과 AI 코딩 도구가 공통으로 읽는 문서**다.
프로젝트 설명, 팀 협업 규칙, 데이터 접근 계약이 여기 한 곳에 있다.
(`CLAUDE.md`는 이 파일을 불러오기만 한다. 같은 내용을 두 곳에 적지 않는다.)

## 프로젝트 개요

**마인드 케어(Mind Care)** — 치매·경도인지장애 가족 간병인을 위한 연구 요약·개인화 앱.
PubMed에서 논문을 주기적으로 수집하고, LLM + RAG로 관련성/근거를 분류·요약해 간병인에게 쉬운 말로 전달하고 개인화된 챗봇으로 답한다.

팀: **온기억** (산학프로젝트2, Fall 2026)

## 기술 스택

- **백엔드**: Python + FastAPI (`backend/`)
- **프론트엔드**: React Native + Expo (`frontend/`)
- **DB**: PostgreSQL + pgvector
- **AI**: 범용 LLM API(GPT/Gemini) + RAG + Embedding
- **논문 수집**: PubMed(NCBI E-utilities) API, 주 1회 배치(Celery/cron)

## 명령어

### 백엔드 (`backend/`)
```bash
python -m venv .venv
.venv\Scripts\Activate.ps1        # Windows PowerShell
pip install -r requirements.txt
cp .env.example .env              # 값 채우기 (.env는 커밋 금지)
docker compose up -d              # 로컬 DB(PostgreSQL 17 + pgvector) 기동
uvicorn main:app --reload         # http://127.0.0.1:8000 , API 문서: /docs
```

### 프론트엔드 (`frontend/`)
```bash
npm install
npm start                         # Expo 개발 서버 (npm run android / ios / web)
```

## 아키텍처

데이터는 한 방향으로 흐른다:

```
PubMed(주1회 배치) → papers 테이블 → AI 파이프라인(근거분류/구조화 요약)
                                        → paper_analysis (+ paper_embeddings)
사용자 질문 → 임베딩 → search_similar() → RAG → LLM → 챗봇 응답(chat_messages)
돌봄 기록(care_logs) → 월1회 배치 → profile_summaries → 개인화 피드(feed_items)
```

핵심 원칙: **DB 접근은 데이터 접근 함수 계층 한 곳으로만 통한다.**
`backend/app/db/functions.py`가 유일한 관문이고, `backend/app/db/connection.py`(원시 커넥션)는
이 계층 내부에서만 쓴다. AI/백엔드 코드는 raw SQL이나 커넥션을 직접 만지지 않는다.

### 주요 경로
- `backend/main.py` — FastAPI 앱 진입점. 팀원 라우터는 여기에 등록.
- `backend/app/config.py` — 환경 변수(pydantic-settings).
- `backend/app/db/functions.py` — **데이터 접근 함수 (팀 공용 계약, 데이터 담당).**
- `backend/app/db/migrations/001_init.sql` — **DB 스키마 정본(테이블 정의). 팀 리뷰 후 확정.**
- `backend/app/schemas.py` — 함수 입출력 스키마(pydantic). 이 계약의 단일 출처.
- `backend/app/collectors/pubmed.py` — PubMed 수집(주 1회 배치).

---

## 팀 협업 규칙

> 초안(2026-09-29). **팀 합의 전**이며, 합의 후 확정한다.
> 팀원 모두가 AI 도구로 코드를 짜기 때문에, 사람과 AI 모두 아래 규칙을 지킨다.

### 브랜치

- 팀원은 **`develop` 브랜치에 push**해서 작업한다.
- **`main`에는 직접 push하지 않는다.** `main`은 `develop`에서 **PR로만** 합치고,
  **팀장 승인 후** 병합한다.
- `git push --force`, `git reset --hard`, 브랜치 삭제 등 **기록을 덮어쓰거나 지우는 명령은 쓰지 않는다.**
- 작업 시작 전에 `develop` 최신 내용을 **pull** 한다.

### 담당 영역

- **자기 담당 폴더만 수정한다.** 다른 영역을 고쳐야 하면 담당자에게 먼저 확인한다.
- **공용 약속 파일**(아래 "데이터 접근 계약" 표, `backend/app/schemas.py`,
  `backend/app/db/migrations/`)을 바꾸기 전에 **팀에 공지**한다.
  계약(함수 이름·입출력 형식)이 바뀌면 코드와 이 문서를 **같이** 고친다.
- **DB는 데이터 담당이 만든 함수로만 접근한다** (직접 SQL 금지).
  `get_new_papers()`, `save_summary()`, `search_similar()` 등을 통해서만 데이터에 접근한다.
- **이미 적용된 마이그레이션 파일은 수정하지 않고, 새 번호 파일로 추가한다**
  (`001_init.sql` 수정 ❌ → `002_xxx.sql` 추가 ⭕).

### 보안

- **`.env`, API 키, 비밀번호는 커밋하지 않는다.** (`.env.example`에 키 이름만 남긴다.)

### AI 도구 사용

- **AI는 사용자가 명시적으로 요청하기 전에는 커밋·push하지 않는다.**
- **AI는 요청받지 않은 담당 외 파일 수정, 대량 이름 변경, 전체 포맷 변경을 하지 않는다.**
- **새 패키지를 추가하면**(`requirements.txt`, `package.json` 변경) **팀에 공지한다.**

### 커밋

- 커밋은 **작게** 나눈다.
- 메시지는 **`feat:` / `fix:` / `docs:` 형식**으로, 한글로 무엇을 바꿨는지 간단히 적는다.
  - 예: `feat: get_new_papers 구현`, `docs: 팀 협업 규칙 추가`

### 폴더별 담당

| 폴더 / 파일 | 역할 | 담당자 |
|---|---|---|
| `backend/app/collectors/` | 데이터 (PubMed 수집) | |
| `backend/app/db/` (`functions.py`, `connection.py`) | 데이터 (DB 접근 함수 계층) | |
| `backend/app/db/migrations/` | 데이터 (스키마) — **변경 전 팀 공지** | |
| `backend/app/schemas.py` | 데이터 (공용 계약) — **변경 전 팀 공지** | |
| `backend/main.py`, `backend/app/config.py` | 백엔드 (앱 진입점·설정) — 공용, 라우터 등록만 추가 | |
| `backend/app/api/` *(예정)* | 백엔드 (API 라우터) | |
| `backend/app/ai/` 검색·RAG *(예정)* | AI-검색 (임베딩, `search_similar` 활용, 챗봇) | |
| `backend/app/ai/` 분류·요약 *(예정)* | AI-요약 (관련성/근거 분류, 구조화 요약) | |
| `frontend/` | 프론트 (React Native + Expo) | |
| 루트 문서 (`AGENTS.md`, `README.md`, `CLAUDE.md`) | 전원 — **변경 전 팀 공지** | |

---

## 데이터 접근 계약

팀원이 이 함수들을 통해서만 DB에 접근한다. 테이블은 `backend/app/db/migrations/001_init.sql`,
함수 입출력은 `backend/app/schemas.py`가 정본.
**아래 표를 바꿀 때는 항상 코드와 이 문서를 동시에 수정한다.**

| 함수 | 입력 | 반환 |
|------|------|------|
| `save_papers(papers: list[PaperIn])` | `papers`: 수집기가 만든 `PaperIn` 목록 | `SavePapersResult` |
| `get_new_papers(since=None, limit=100, source=None)` | `since`: ISO8601 날짜/시각(선택), `limit`: int, `source`: `'pubmed'` 등(선택) | `list[PaperOut]` |
| `save_summary(analysis: AnalysisIn)` | `AnalysisIn` | 저장된 `paper_analysis` id (`UUID`) |
| `search_similar(embedding, top_k=5, model_name=None)` | `embedding`: `list[float]`(길이=`001_init.sql`의 `vector(N)`), `top_k`: int, `model_name`: 임베딩 모델 필터(선택) | `list[SearchResult]` |
| `get_papers_by_ids(paper_ids: list[UUID])` | `paper_ids`: `papers.id` 목록 (`search_similar()`가 돌려주는 `paper_id`와 같은 종류) | `list[PaperDetail]` — **입력 순서 그대로** |
| `update_paper_metadata(papers: list[PaperIn])` | `papers`: 수집기가 만든 `PaperIn` 목록 | `UpdateMetadataResult` |
| `get_papers_missing_metadata(limit=500, source=None)` | `limit`: int, `source`: `'pubmed'` 등(선택) | `list[PaperOut]` |

스키마 (요약) — **모든 id는 `UUID`**:
- **PaperIn** (`papers` 입력): `source, external_id, title, abstract?, published_date?, url?, journal?, doi?, publication_types, mesh_terms` — `id`/`collected_at`은 DB가 채운다.
- **SavePapersResult**: `total, inserted, skipped, inserted_ids`
- **PaperOut** (`papers`): `id, source, external_id, title, abstract?, published_date?, url?, collected_at`
- **AnalysisIn** (`paper_analysis` + 선택적 `paper_embeddings`): `paper_id, study_type?, evidence_level?, guideline_relation?, summary_finding?, summary_comparison?, summary_limitation?, tags, model_name?, embedding?, embedding_model?`
- **SearchResult**: `paper_id, title, url?, summary_finding?, evidence_level?, score`
- **PaperDetail** (`get_papers_by_ids` 반환): `paper_id, external_id, title, abstract?, journal?, published_date?, publication_types, mesh_terms, doi?`
- **UpdateMetadataResult**: `total, updated, not_found`(papers에 없던 `external_id` 목록)

동작 메모:
- `save_papers()`는 `(source, external_id)` UNIQUE + `ON CONFLICT DO NOTHING`으로 이미 있는 논문을
  건너뛴다(기존 행은 갱신하지 않는다). 배치 안의 중복도 먼저 제거해 집계를 맞춘다.
- `get_new_papers()` = `papers` 중 `paper_analysis`가 없는 논문 (`paper_analysis`는 `paper_id` UNIQUE).
  정렬은 `published_date DESC NULLS LAST` → `collected_at DESC` → `id`. 한 배치는 `collected_at`이
  모두 같아서, 마지막 `id` 키가 있어야 `limit`을 준 결과 순서가 호출마다 흔들리지 않는다.
  `since`/`source`는 `None`이면 해당 조건을 적용하지 않고, `limit <= 0`이면 빈 목록을 돌려준다.
- `save_summary()`는 `paper_analysis`에 upsert 하고, `embedding`을 함께 주면 `paper_embeddings`에도 upsert 한다.
- `search_similar()`는 `paper_embeddings`(코사인)를 검색해 `papers`/`paper_analysis`를 조인해 돌려준다.
  `paper_id`는 `chat_messages.cited_paper_ids`에 그대로 넣을 수 있다.
- `get_papers_by_ids()`는 **받은 `paper_ids` 순서를 그대로 유지한다**
  (`unnest(...) WITH ORDINALITY`로 입력 순번을 붙여 정렬). AI 쪽이 검색 점수를 순서대로 붙이기 때문이다.
  **DB에 없는 id는 예외 없이 결과에서 빠진다** — 결과 길이가 입력보다 짧을 수 있고, 빠진 id는 경고 로그로 남는다.
- `update_paper_metadata()`는 이미 저장된 논문의 `journal`/`doi`/`publication_types`/`mesh_terms`만 갱신한다.
  제목·초록은 건드리지 않고, 새 값이 비어 있으면 기존 값을 유지하며, papers에 없는 논문은 새로 넣지 않는다.
  (`save_papers()`의 "중복이면 건너뛰기"를 바꾸지 않기 위해 따로 둔 함수다.)
- `get_papers_missing_metadata()`는 위 네 컬럼이 **모두** 비어 있는 논문만 돌려준다 (백필 대상 고르기용).

수집기(`collectors/pubmed.py`) 쪽 함수:
- `fetch_papers_by_pmids(pmids, require_abstract=True)` — PMID 목록으로 논문을 받아온다(검색 단계 없음).
  `FetchByPmidsResult(papers, missing_abstract, not_found)`를 돌려줘서, 초록이 없거나 PubMed에 없는
  PMID를 조용히 버리지 않는다. 저장은 `save_papers()`로 따로 한다.

- `save_summary()`는 `paper_analysis`에 upsert 한다 (`paper_id` UNIQUE → 재분석하면 기존 행을 갱신하고
  `generated_at`을 다시 찍는다. id는 그대로). 저장한 논문은 `get_new_papers()` 목록에서 빠진다.
  `embedding`을 함께 주면 `paper_embeddings`에도 같은 트랜잭션으로 upsert 하며, 이때 `embedding_model`이
  없으면 `ValueError`를 던진다(`paper_embeddings.model_name`이 NOT NULL).
  **임베딩 경로는 구현만 해둔 상태로 아직 검증하지 않았다** — 아래 `vector(768)` 차원 문제가 풀린 뒤에 검증한다.

> `search_similar()`만 아직 스텁(`NotImplementedError`)이다 — 시그니처/스키마 합의가 먼저,
> 구현은 그 다음. 나머지 함수는 모두 구현 완료.

### 결정사항: 논문 관련성 판단 (2026-09-23)

논문 관련성 판단은 두 단계로 한다:

1. **수집 단계**에서 치매/MCI 키워드로 필터링 — 전역 관련 여부는 이 단계에서 이미 보장된다.
2. **`feed_items.relevance_score`**로 사용자별 관련도만 계산한다.

논문 테이블에 별도의 전역 관련성 플래그(`is_relevant` 등)는 두지 않는다.

## 상태

초기 스캐폴딩 단계. DB 스키마 초안(`001_init.sql`)은 올라왔고 **팀 리뷰 대기 중**.

- **PubMed 수집 구현 완료** — `collectors/pubmed.py`의 `fetch_papers()` / `collect_and_store()`.
  검색어는 `SEARCH_QUERY` 상수, API 키는 없어도 동작(초당 3회 제한 자동 준수).
  단독 실행: `python -m app.collectors.pubmed --days-back 7 --max-results 20 --dry-run`
- **`save_papers()` 구현·검증 완료** (2026-09-26, 실 DB). PubMed 50건을 저장 → 신규 50건,
  같은 데이터로 재저장 → 신규 0건 / 스킵 50건으로 중복 스킵 동작 확인.
- **로컬 DB는 Docker로 띄운다** — `backend/docker-compose.yml` (pgvector/pgvector:pg17).
  최초 기동 시 `001_init.sql`이 자동 실행된다(테이블 11개 + `vector`/`uuid-ossp` 확장 확인).
  실행 방법은 `backend/README.md` 참고. 스키마를 고쳐 다시 적용할 때는 `docker compose down -v`.
- **`get_new_papers()` 구현·검증 완료** (2026-09-26, 실 DB). 미분석 50건 반환,
  `paper_analysis`를 1건 넣으면 49건으로 줄고, `limit`/`source`/`since` 필터 동작 확인.
- **논문 메타데이터 컬럼 추가 완료** (2026-09-29, `002_add_paper_metadata.sql`).
  `papers`에 `journal` / `doi` / `publication_types` / `mesh_terms` + `mesh_terms` GIN 인덱스.
  기존 50건은 `scripts/backfill_paper_metadata.py`로 채웠다(50/50).
  새 DB는 `docker compose up -d` 시 001 → 002 순서로 자동 실행된다(임시 컨테이너로 확인).
- `get_papers_by_ids()` 구현·검증 완료 (2026-09-29, 실 DB). 입력 순서 유지, 없는 id는 제외 확인.
- **`save_summary()` 구현·검증 완료** (2026-09-29, 실 DB). 요약 저장 → 같은 논문 재저장 시
  같은 id로 갱신(행 1개 유지) → `get_new_papers()` 50건에서 49건으로 줄어드는 것까지 확인하고
  테스트 데이터는 삭제했다. 임베딩 경로는 차원 문제 때문에 아직 미검증.
- 아직 `search_similar()` 구현, AI 파이프라인, 화면이 없다.
- **팀 협업 규칙(위 섹션)은 초안 — 팀 합의 대기 중.**

**미해결 — AI 담당 확인 후 `vector(768)` 차원 확정 예정.**
`001_init.sql`의 `paper_embeddings.embedding`은 `vector(768)`인데 `config.py`의
`embedding_dim`은 1536(`text-embedding-3-small` 기준)이라 현재 서로 맞지 않는다.
AI 담당이 임베딩 모델을 확정하면 그 차원에 맞춰 `001_init.sql`과 `config.py`를
함께 고친다. **그 전까지는 어느 쪽도 임의로 바꾸지 않는다.**
