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
- `backend/app/db/migrations/` — **DB 스키마 정본.** `001_init.sql`(테이블 정의, 팀 리뷰 후 확정)
  + `002_add_paper_metadata.sql`(논문 메타데이터 컬럼) + `003_embedding_dim_1024.sql`(임베딩 `vector(1024)` + HNSW).
- `backend/app/schemas.py` — 함수 입출력 스키마(pydantic). 이 계약의 단일 출처.
- `backend/app/collectors/pubmed.py` — PubMed 수집(주 1회 배치).
- `backend/app/api/` — API 라우터. 기능별 파일(`papers.py` …)에 `router`를 두고 `main.py`에서 등록.

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
| `backend/app/api/` | 백엔드 (API 라우터) — 기능별 파일. `papers.py`는 박주현 | |
| `backend/app/ai/` 검색·RAG *(예정)* | AI-검색 (임베딩, `search_similar` 활용, 챗봇) | |
| `backend/app/ai/` 분류·요약 *(예정)* | AI-요약 (관련성/근거 분류, 구조화 요약) | |
| `frontend/` | 프론트 (React Native + Expo) | |
| 루트 문서 (`AGENTS.md`, `README.md`, `CLAUDE.md`) | 전원 — **변경 전 팀 공지** | |

---

## 데이터 접근 계약

팀원이 이 함수들을 통해서만 DB에 접근한다. 테이블은 `backend/app/db/migrations/001_init.sql`,
함수 입출력은 `backend/app/schemas.py`가 정본.
**아래 표를 바꿀 때는 항상 코드와 이 문서를 동시에 수정한다.**

### 데이터 접근 함수 담당 나누기

`backend/app/db/functions.py`는 팀 공용 관문이고, **DB는 이 함수들로만 접근한다**는
규칙은 그대로다(직접 SQL 금지). 다만 도메인별로 함수 담당을 아래처럼 나눈다.

| 도메인 | 다루는 테이블 | 담당 |
|---|---|---|
| 논문·임베딩·요약·검색 | `papers`, `paper_embeddings`, `paper_analysis` | 박주현 |
| 사용자·환자 관련 | `users`, `patient_profiles`, `caregiver_profiles`, `clinical_assessments`, `safety_events`, `medications`, `medical_visits` | 김한슬 |

`paper_analysis`는 논문당 한 줄인데 **칸마다 채우는 사람이 다르다** (2026-10-09, 김현서 님과 합의).
서로의 칸을 지우지 않도록 저장 함수도 나뉜다.

| `paper_analysis` 칸 | 채우는 사람 | 저장 함수 | 대상 고르는 함수 |
|---|---|---|---|
| `study_type`, `evidence_level` | 김현서 (근거 등급) | `save_evidence()` | `get_papers_without_evidence()` |
| `summary_finding` / `summary_comparison` / `summary_limitation`, `guideline_relation`, `tags` | 허웅 (요약) | `save_summary()` | `get_new_papers()` |

**함수를 추가·변경한 사람이 위 "데이터 접근 계약" 표(와 `schemas.py`)도 같이 고친다.**
(계약 표를 바꾸기 전에는 팀에 공지한다 — "담당 영역" 규칙과 동일.)

| 함수 | 입력 | 반환 |
|------|------|------|
| `save_papers(papers: list[PaperIn])` | `papers`: 수집기가 만든 `PaperIn` 목록 | `SavePapersResult` |
| `get_new_papers(since=None, limit=100, source=None)` | `since`: ISO8601 날짜/시각(선택), `limit`: int, `source`: `'pubmed'` 등(선택) | `list[PaperOut]` |
| `save_summary(analysis: AnalysisIn)` | `AnalysisIn` | 저장된 `paper_analysis` id (`UUID`) |
| `save_evidence(items)` | `items`: `(paper_id, study_type, evidence_level)` 목록 (`EvidenceIn`도 가능). `evidence_level`은 1~6 또는 `None` | `SaveEvidenceResult` |
| `get_papers_without_evidence(limit=100)` | `limit`: int | `list[PaperDetail]` |
| `search_similar(embedding, top_k=5, model_name=None)` | `embedding`: `list[float]`(길이=`EMBEDDING_DIM`=**1024**), `top_k`: int, `model_name`: 임베딩 모델 필터(선택) | `list[SearchResult]` — `paper_id` + `score`만 |
| `get_papers_by_ids(paper_ids: list[UUID])` | `paper_ids`: `papers.id` 목록 (`search_similar()`가 돌려주는 `paper_id`와 같은 종류) | `list[PaperDetail]` — **입력 순서 그대로** |
| `update_paper_metadata(papers: list[PaperIn])` | `papers`: 수집기가 만든 `PaperIn` 목록 | `UpdateMetadataResult` |
| `get_papers_missing_metadata(limit=500, source=None)` | `limit`: int, `source`: `'pubmed'` 등(선택) | `list[PaperOut]` |
| `get_papers_without_embedding(model_name, limit=100)` | `model_name`: 기준 임베딩 모델(예: `'bge-m3'`), `limit`: int | `list[PaperOut]` |
| `save_embeddings(items, model_name)` | `items`: `(paper_id, 벡터)` 쌍 목록 (`EmbeddingIn`도 가능), `model_name`: str(필수) | `SaveEmbeddingsResult` |
| `list_papers(limit=20, offset=0)` | `limit`: int, `offset`: int | `PaperListResponse` — `total` + `items` |
| `get_paper_detail(paper_id: UUID)` | `paper_id`: `papers.id` | `PaperDetailResponse` 또는 `None`(없는 id) |
| `get_paper_details_by_pmids(pmids: list[str])` | `pmids`: PubMed PMID 목록 (`papers.external_id`, `source='pubmed'`) | `dict[str, PaperDetailResponse]` — 키가 PMID |

스키마 (요약) — **모든 id는 `UUID`**:
- **PaperIn** (`papers` 입력): `source, external_id, title, abstract?, published_date?, url?, journal?, doi?, publication_types, mesh_terms` — `id`/`collected_at`은 DB가 채운다.
- **SavePapersResult**: `total, inserted, skipped, inserted_ids`
- **PaperOut** (`papers`): `id, source, external_id, title, abstract?, published_date?, url?, collected_at`
- **AnalysisIn** (`paper_analysis` + 선택적 `paper_embeddings`): `paper_id, study_type?, evidence_level?, guideline_relation?, summary_finding?, summary_comparison?, summary_limitation?, tags, model_name?, embedding?, embedding_model?`
  - `study_type` / `evidence_level`은 **비워 보내면 기존 값을 유지한다** (등급 저장은 `save_evidence()`).
- **EvidenceIn** (`save_evidence` 입력): `paper_id, study_type`(필수, 빈 값 불가), `evidence_level?`(int 1~6, 등급 없음은 `None`)
- **SaveEvidenceResult**: `total, saved, not_found`(papers에 없던 `paper_id` 목록)
- **SearchResult** (`search_similar` 반환): `paper_id, score` — ★2026-10-01 축소. 제목·요약 등은 `get_papers_by_ids()`로 붙인다.
- **PaperDetail** (`get_papers_by_ids` 반환): `paper_id, external_id, title, abstract?, journal?, published_date?, publication_types, mesh_terms, doi?`
- **UpdateMetadataResult**: `total, updated, not_found`(papers에 없던 `external_id` 목록)
- **EmbeddingIn** (`paper_embeddings` 입력): `paper_id, embedding` — `embedding` 길이는 반드시 `EMBEDDING_DIM`(=1024).
- **SaveEmbeddingsResult**: `total, saved, not_found`(papers에 없던 `paper_id` 목록)
- **PaperListItem** (`list_papers` 목록 한 줄): `id, title, published_at?, journal?, evidence_level?, summary_finding?` — 초록 없음.
- **PaperListResponse**: `total`(전체 논문 수), `limit, offset, items`(`PaperListItem` 목록)
- **PaperDetailResponse** (`get_paper_detail` 반환): `id, title, abstract?, published_at?, journal?, pubmed_url?, doi?, publication_types, mesh_terms` + 요약 `study_type?, evidence_level?, summary_finding?, summary_comparison?, summary_limitation?`
  - 이 세 모델은 **화면 이름**(`frontend/src/types/index.ts`)을 쓴다: `papers.id`→`id`, `published_date`→`published_at`, `url`→`pubmed_url`.
- **RecommendationItem** (`GET /recommendations` 항목 한 줄): `id, title, published_at?, journal?, pubmed_url?, relevance_score, personal_reason?`(아직 항상 `None`) + 요약 `study_type?, evidence_level?, summary_finding?, summary_comparison?, summary_limitation?`
- **RecommendationResponse**: `user_id, patient_id, query`(이번에 쓴 검색어), `items`(`RecommendationItem` 목록, 관련도 높은 순)
- **EMBEDDING_DIM** = `1024` (`schemas.py` 상수). DB의 `vector(1024)`와 같아야 한다 — 바꿀 때는 새 마이그레이션 + 이 상수 + `config.py`의 `embedding_dim`을 **함께** 고친다.

동작 메모:
- `save_papers()`는 `(source, external_id)` UNIQUE + `ON CONFLICT DO NOTHING`으로 이미 있는 논문을
  건너뛴다(기존 행은 갱신하지 않는다). 배치 안의 중복도 먼저 제거해 집계를 맞춘다.
- **★ 계약 변경 (2026-10-09)** `get_new_papers()` = `papers` 중 **`paper_analysis` 줄이 없거나
  `summary_finding`이 비어 있는** 논문. (이전: "줄이 없는 논문". 근거 등급이 먼저 들어가 줄이 생기면
  요약 대상에서 빠지던 문제 때문에 바꿨다.) 이제 "요약이 아직 없는 논문"이라는 뜻이고,
  등급이 있는지는 보지 않는다.
  정렬은 `published_date DESC NULLS LAST` → `collected_at DESC` → `id`. 한 배치는 `collected_at`이
  모두 같아서, 마지막 `id` 키가 있어야 `limit`을 준 결과 순서가 호출마다 흔들리지 않는다.
  `since`/`source`는 `None`이면 해당 조건을 적용하지 않고, `limit <= 0`이면 빈 목록을 돌려준다.
- `save_summary()`는 `paper_analysis`에 upsert 하고, `embedding`을 함께 주면 `paper_embeddings`에도 upsert 한다.
  **(2026-10-09 변경)** `study_type` / `evidence_level`은 값을 보냈을 때만 바꾸고, 비워 보내면 기존 값을
  유지한다(`COALESCE`). 요약 칸·`guideline_relation`·`tags`·`model_name`은 예전처럼 보낸 값으로 덮어쓴다.
- **근거 등급 전용 함수 2개는 요약 칸을 전혀 건드리지 않는다.** 등급 배치와 요약 배치는 어느 쪽을
  먼저 돌려도 서로의 값을 지우지 않는다.
  - `save_evidence(items)`는 줄이 없으면 `study_type` / `evidence_level`만 채운 줄을 새로 만들고,
    있으면 그 두 칸만 바꾼다(`generated_at`도 그대로). 한 번의 INSERT로 넣으므로 1,000편도 한 번에 된다.
    `evidence_level`은 `evidence.py` 기준 숫자 **1~6을 문자열(`'1'`~`'6'`)로** 저장하고(칸이 `TEXT`),
    등급 없음(동물 연구·프로토콜 등)은 `NULL`이다. 1~6 밖의 값이나 빈 `study_type`이 한 건이라도 있으면
    **DB에 보내기 전에** `ValueError`를 던지고 아무것도 저장하지 않는다. 배치 내 중복 `paper_id`는 첫 건만 쓰고,
    `papers`에 없는 `paper_id`는 건너뛰어 `not_found`로 보고한다(`save_embeddings()`와 같은 규칙).
  - `get_papers_without_evidence(limit)` = **`study_type`이 비어 있는** 논문(줄이 없는 논문 포함).
    `evidence_level`이 아니라 `study_type`을 기준으로 삼는 이유: 판단은 했지만 등급이 없는(`NULL`) 논문이
    매번 다시 나오지 않게 하기 위해서다. 등급 판단에 쓸 `title` / `abstract` / `publication_types` /
    `mesh_terms`가 든 `PaperDetail`을 돌려주고, 정렬은 `get_new_papers()`와 같으며 `limit <= 0`이면 빈 목록.
- `search_similar()`는 `paper_embeddings`만 코사인 거리(`<=>`)로 검색해 **`paper_id`와 `score`만** 돌려준다
  (`score` = `1 - 코사인 거리`, 1에 가까울수록 유사. `score` 내림차순). 논문 본문·요약과 조인하지 않는다 —
  받은 순서 그대로 `get_papers_by_ids()`에 넣으면 유사도 순서가 보존된 상세가 나온다.
  `model_name`을 주면 같은 모델끼리만 비교한다(모델이 다르면 벡터 공간이 달라 점수 비교가 무의미하다).
  질의 벡터 길이가 `EMBEDDING_DIM`과 다르면 DB에 가기 전에 `ValueError`, `top_k <= 0`이면 빈 목록.
  `paper_id`는 `chat_messages.cited_paper_ids`에 그대로 넣을 수 있다.
- `get_papers_by_ids()`는 **받은 `paper_ids` 순서를 그대로 유지한다**
  (`unnest(...) WITH ORDINALITY`로 입력 순번을 붙여 정렬). AI 쪽이 검색 점수를 순서대로 붙이기 때문이다.
  **DB에 없는 id는 예외 없이 결과에서 빠진다** — 결과 길이가 입력보다 짧을 수 있고, 빠진 id는 경고 로그로 남는다.
- `update_paper_metadata()`는 이미 저장된 논문의 `journal`/`doi`/`publication_types`/`mesh_terms`만 갱신한다.
  제목·초록은 건드리지 않고, 새 값이 비어 있으면 기존 값을 유지하며, papers에 없는 논문은 새로 넣지 않는다.
  (`save_papers()`의 "중복이면 건너뛰기"를 바꾸지 않기 위해 따로 둔 함수다.)
- `get_papers_missing_metadata()`는 위 네 컬럼이 **모두** 비어 있는 논문만 돌려준다 (백필 대상 고르기용).
- **임베딩 전용 함수 2개는 `paper_analysis`(요약)를 전혀 건드리지 않는다.** 요약 배치와 임베딩 배치를
  서로 기다리지 않고 따로 돌릴 수 있게 분리한 경로다. (요약 대상 고르기는 `get_new_papers()`)
  - `get_papers_without_embedding(model_name, limit)` = 임베딩이 **없는** 논문 + **다른 `model_name`으로**
    저장된 논문. 모델을 바꿔 다시 임베딩할 때도 이 함수 하나로 대상이 잡힌다. 정렬은 `get_new_papers()`와
    같고(`published_date DESC NULLS LAST` → `collected_at DESC` → `id`), `limit <= 0`이면 빈 목록.
    임베딩에 쓸 `title`/`abstract`가 들어 있는 `PaperOut`을 돌려준다.
  - `save_embeddings(items, model_name)`는 논문당 임베딩 **1건만** 유지한다
    (`paper_embeddings.paper_id`가 PK → 이미 있으면 덮어쓴다). `model_name`은 기록용이고 빈 값은 받지 않는다.
    벡터 길이가 `EMBEDDING_DIM`(1024)과 다르면 **DB에 보내기 전에** `ValueError`를 던지고,
    배치 안에 한 건이라도 섞여 있으면 **아무것도 저장하지 않는다**(다른 모델 벡터 혼입 방지).
    배치 내 중복 `paper_id`는 첫 건만 쓴다. `papers`에 없는 `paper_id`는 외래키 위반으로 배치가 통째로
    실패하지 않게 건너뛰고 `not_found`로 보고한다.

수집기(`collectors/pubmed.py`) 쪽 함수:
- `fetch_papers_by_pmids(pmids, require_abstract=True)` — PMID 목록으로 논문을 받아온다(검색 단계 없음).
  `FetchByPmidsResult(papers, missing_abstract, not_found)`를 돌려줘서, 초록이 없거나 PubMed에 없는
  PMID를 조용히 버리지 않는다. 저장은 `save_papers()`로 따로 한다.

- `save_summary()`는 `paper_analysis`에 upsert 한다 (`paper_id` UNIQUE → 재분석하면 기존 행을 갱신하고
  `generated_at`을 다시 찍는다. id는 그대로). `summary_finding`을 채워 저장한 논문은 `get_new_papers()` 목록에서 빠진다.
  `embedding`을 함께 주면 `paper_embeddings`에도 같은 트랜잭션으로 upsert 하며, 이때 `embedding_model`이
  없으면 `ValueError`를 던진다(`paper_embeddings.model_name`이 NOT NULL).
  **`save_summary()`의 임베딩 저장 경로는 여전히 미검증이다** (차원은 1024로 정리됐지만 이 경로 자체는
  아직 실 데이터로 확인하지 않았다). 요약 담당 확인 후 따로 처리한다.
  임베딩만 저장할 때는 `save_embeddings()`를 쓰는 것이 권장 경로다.

> `functions.py`의 함수는 **모두 구현·검증 완료**다 (스텁 없음).

- **화면용 조회 함수 2개** (`list_papers` / `get_paper_detail`)는 `papers LEFT JOIN paper_analysis`로 읽기만 한다.
  요약이 아직 없는 논문은 요약 쪽 필드(`study_type`, `evidence_level`, `summary_*`)가 모두 `None`이다.
  - `list_papers()`는 전체 논문이 대상이다(`get_new_papers()`는 미분석 논문만). 정렬은 `get_new_papers()`와 같고
    (`published_date DESC NULLS LAST` → `collected_at DESC` → `id`), `total`은 `limit`/`offset`과 무관한 전체 개수다.
  - `get_paper_detail()`은 없는 id면 예외 없이 `None`을 돌려준다 (API가 404로 바꾼다).
    `get_papers_by_ids()`(AI용 계약, url·요약 없음)는 그대로 두고 따로 만든 함수다.
- `get_paper_details_by_pmids()`는 `get_paper_detail()`과 같은 형식을 **PMID로 여러 건** 받는다
  (`search()`의 Evidence Package에 `paper_id`가 없고 PMID만 있어서 둔 함수, `/recommendations`가 쓴다).
  읽기만 하고, 요약이 없으면 요약 쪽 필드가 `None`이다. **`papers`에 없는 PMID는 예외 없이 결과에 키가 없다.**
  순서는 보장하지 않으므로 호출한 쪽이 PMID로 찾아 쓴다.

## API 주소 목록

라우터는 `backend/app/api/`에 기능별 파일로 두고 `backend/main.py`에서 `app.include_router(...)`로 등록한다.
응답 형식의 정본은 `backend/app/schemas.py`이고, 실행 중인 서버의 `/docs`(Swagger)에서 예시와 함께 볼 수 있다.
**주소를 추가·변경하면 이 표도 같이 고친다.**

| 주소 | 파일 | 설명 | 응답 |
|---|---|---|---|
| `GET /health` | `main.py` | 헬스 체크 | `{status, env}` |
| `GET /papers?limit=20&offset=0` | `api/papers.py` | 논문 목록, 최신 발행일 순. `limit` 1~100(기본 20), `offset` 0 이상. 범위를 벗어나면 422 | `PaperListResponse` |
| `GET /papers/{paper_id}` | `api/papers.py` | 논문 상세 + 요약 3칸. 없는 id는 404, UUID 형식이 아니면 422 | `PaperDetailResponse` |
| `GET /feed/{user_id}` | `api/users.py` | 간병인 + 담당 환자 전원의 현재 상태. 없는 사용자는 404 | `UserProfileContext` |
| `GET /feed_user/{user_id}` | `api/users.py` | 간병인 정보만 (이름 + 자가점검 + 담당 환자 id 목록). 없는 사용자는 404 | `CaregiverContext` |
| `GET /feed_patient/{user_id}/{patient_id}` | `api/users.py` | 환자 한 명의 현재 상태. 이 간병인의 환자가 아니면 404 | `PatientContext` |
| `GET /recommendations/{user_id}?patient_id=&k=5` | `api/recommendations.py` | 환자 프로필에 맞는 논문 Top-k. `k` 1~20(기본 5), 범위를 벗어나면 422. 환자가 1명이면 `patient_id` 생략 가능, 2명 이상인데 생략하면 400. 없는 사용자·이 사용자의 환자가 아니면 404 | `RecommendationResponse` |

- `/feed…` 주소 3개(담당: 김한슬)는 **간병인·환자 정보 조회(피드·챗봇 개인화 입력용)**다. 이름은 feed지만
  논문을 돌려주지 않는다 — **논문 추천은 `/recommendations`**. 응답 형식의 정본은 `backend/app/user_schemas.py`.
- 사용자·환자 함수 목록은 `app/db/user_functions.py` 참고 (담당: 김한슬).
- `/recommendations`(담당: 박주현)는 첫 버전이다. 흐름: `user_functions`로 프로필 읽기 → `build_query()`로
  검색어 한 줄 → `search(검색어, k)` → `get_paper_details_by_pmids()`로 `paper_id`·저장된 요약 붙이기.
  - `build_query()`(`api/recommendations.py`)는 환자 단계·증상·관심사를 문장에 끼워 넣는 **임시 함수**다
    (허웅 님 피드 흐름으로 교체 예정). 응답의 `query`에 이번에 쓴 검색어가 그대로 나온다(데모·디버깅용).
  - 항목 이름은 `/papers`와 같다(`id`, `published_at`, `pubmed_url`, `summary_*` …) + `relevance_score`,
    `personal_reason`. **`personal_reason`은 아직 항상 `null`** (피드 흐름 연결 후 채운다).
  - 서버를 띄운 뒤 **첫 요청은 bge-m3 모델을 불러오느라 오래 걸린다** (그 뒤로는 메모리에 남는다).

- 요약(`paper_analysis`)이 아직 없는 논문은 요약 쪽 필드가 `null`로 나온다.
- `evidence_level`은 DB에 저장된 값(`'1'`~`'6'` 또는 `null`)을 그대로 내보낸다. 화면의 `'A'~'D' | 'guideline'` 등급으로 바꾸는 규칙은 **아직 미정**이다.

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
  최초 기동 시 `migrations/` 안의 SQL이 파일명 순서대로 자동 실행된다(테이블 11개 + `vector`/`uuid-ossp` 확장 확인).
  실행 방법은 `backend/README.md` 참고. 스키마를 고쳐 다시 적용할 때는 `docker compose down -v`.
- **`get_new_papers()` 구현·검증 완료** (2026-09-26, 실 DB). 미분석 50건 반환,
  `paper_analysis`를 1건 넣으면 49건으로 줄고, `limit`/`source`/`since` 필터 동작 확인.
- **논문 메타데이터 컬럼 추가 완료** (2026-09-29, `002_add_paper_metadata.sql`).
  `papers`에 `journal` / `doi` / `publication_types` / `mesh_terms` + `mesh_terms` GIN 인덱스.
  기존 50건은 `scripts/backfill_paper_metadata.py`로 채웠다(50/50).
  새 DB는 `docker compose up -d` 시 001 → 002 순서로 자동 실행된다(임시 컨테이너로 확인).
- `get_papers_by_ids()` 구현·검증 완료 (2026-09-29, 실 DB). 입력 순서 유지, 없는 id는 제외 확인.
- **임베딩 차원 1024 확정·적용 완료** (2026-10-01, `003_embedding_dim_1024.sql`).
  AI 담당(김현서)과 **bge-m3 / 1024차원**으로 합의. `paper_embeddings.embedding`을 `vector(1024)`로 바꾸고
  IVFFlat 인덱스를 **HNSW**(`vector_cosine_ops`)로 교체했다(임베딩이 비어 있어 IVFFlat은 클러스터가
  잡히지 않는다). `config.py` / `.env.example`의 `EMBEDDING_MODEL`·`EMBEDDING_DIM`도 함께 맞췄다.
  새 DB는 001 → 002 → 003 순서로 자동 실행되는 것을 임시 컨테이너로 확인했고, 여러 번 실행해도 안전하다.
- **임베딩 전용 함수 2개 구현·검증 완료** (2026-10-01, 실 DB).
  `get_papers_without_embedding()` / `save_embeddings()`. 저장 → 미임베딩 건수 감소, 다른 `model_name`
  기준이면 다시 대상에 포함, 덮어쓰기로 논문당 1건 유지, 768·1536차원 거부, 없는 `paper_id` 건너뛰기,
  `paper_analysis` 무영향까지 확인하고 테스트 임베딩은 삭제했다.
- **`search_similar()` 구현·검증 완료** (2026-10-01, 실 DB). 1024차원 임의 벡터를 논문 5건에 저장하고
  각 벡터로 검색 → 자기 자신이 `score` 1.000000으로 1위, 점수 내림차순, `model_name` 필터 동작 확인.
  실행 계획에서 HNSW 인덱스(`Index Scan using idx_paper_embeddings_hnsw`)를 타는 것도 확인했다.
  테스트 임베딩은 모두 삭제했다. **반환값이 `paper_id` + `score`로 축소된 것이 계약 변경점이다.**
- **`save_summary()` 구현·검증 완료** (2026-09-29, 실 DB). 요약 저장 → 같은 논문 재저장 시
  같은 id로 갱신(행 1개 유지) → `get_new_papers()` 50건에서 49건으로 줄어드는 것까지 확인하고
  테스트 데이터는 삭제했다. 임베딩 경로는 차원 문제 때문에 아직 미검증.
- 아직 AI 파이프라인(분류·요약·실제 임베딩 생성)과 화면이 없다.
- **논문 API 2개 구현·검증 완료** (2026-10-07, 실 DB 1,000편). `GET /papers`, `GET /papers/{paper_id}` +
  조회 함수 `list_papers()` / `get_paper_detail()`. 목록 5건·다음 5건(겹침 없음)·상세·404·422 호출과
  `/docs` 예시를 확인했고, 임시 요약 1건으로 요약이 붙어 나오는 것도 확인한 뒤 삭제했다.
- **라벨링 후보 60편 적재 완료** (2026-10-03, 실 DB). `backend/app/db/seed/labeling_candidates.txt`의
  PMID 60건을 `python -m scripts.seed_labeling_papers`로 적재 → **신규 56건 + 기존 4건**(이미 있던
  논문은 `save_papers()`의 중복 스킵 동작대로 건너뜀).
- **팀 기준 데이터: 논문 1,000편 + bge-m3 임베딩 1,000개** (2026-10-06, 실 DB). AI 담당 김현서 님의
  DB 덤프(`mindcare_papers_bge-m3.dump`, custom·데이터만)로 교체했다. PMID 목록은
  `backend/app/db/seed/team_pmids_1000.txt`, 라벨링 후보 60편도 모두 포함된다.
  검증: `papers`/`paper_embeddings` 각 1,000건, 임베딩 전부 `bge-m3`·1024차원,
  `get_papers_without_embedding('bge-m3')` 0건, `search_similar()`가 질의 논문을 score 1.0 1위로 반환.
  **같은 덤프로 복원하면 팀원 모두 `paper_id`가 동일해진다.** 덤프는 레포에 없고 노션에서 받는다
  (복원 방법은 `README.md`의 "팀원 데이터 맞추기").
- **근거 등급·요약 저장 경로 분리 완료** (2026-10-09, 실 DB). `save_evidence()` /
  `get_papers_without_evidence()` 추가, `get_new_papers()` 기준 변경(**계약 변경**), `save_summary()`가
  등급 칸을 비워 보내면 유지하도록 수정. 등급 먼저 → 요약, 요약 먼저 → 등급 두 순서 모두 서로의 칸이
  남는 것, 등급만 넣은 논문이 `get_new_papers()`에 계속 나오는 것, "등급 없음" 논문이 등급 대상에서
  빠지는 것, 1~6 밖의 값 거부를 논문 3편으로 확인하고 테스트 줄은 삭제했다.
- **논문 추천 API 첫 버전 구현·검증 완료** (2026-10-10, 실 DB). `GET /recommendations/{user_id}` +
  `get_paper_details_by_pmids()`. 샘플 간병인 4명(환자 5명) 전부 Top-5 반환, 400·404·422,
  임시 요약 1건이 붙어 나오는 것까지 확인하고 테스트 줄은 삭제했다. 서버를 띄운 뒤 첫 요청은 약 11초
  (bge-m3 로드, CPU), 그 뒤로는 약 0.4초. 검색어는 임시 `build_query()`이고 `personal_reason`은 아직 `null`.
- **팀 협업 규칙(위 섹션)은 초안 — 팀 합의 대기 중.**

**해결됨 (2026-10-01) — 임베딩 차원은 1024(bge-m3)로 확정.**
`001_init.sql`의 `vector(768)`과 `config.py`의 `embedding_dim=1536`이 어긋나 있던 문제는
`003_embedding_dim_1024.sql`로 정리했다. 이제 세 곳이 모두 1024다 —
DB(`vector(1024)`), `schemas.EMBEDDING_DIM`, `config.embedding_dim`.
**차원을 다시 바꿀 때는 새 마이그레이션 파일 + 이 세 곳을 한 커밋에서 같이 고친다.**
(`001_init.sql`은 이미 적용된 파일이라 수정하지 않는다.)
