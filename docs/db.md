# 데이터(DB) 계약

> 담당: **박주현** (사용자·환자 함수는 김한슬 → [`users.md`](users.md)).
> 공통 규칙·폴더 담당은 루트 [`AGENTS.md`](../AGENTS.md). API 응답 모델은 [`api.md`](api.md).

## 아키텍처 (데이터 흐름)

데이터는 한 방향으로 흐른다:

```
PubMed(주1회 배치) → papers 테이블 → AI 파이프라인(근거분류/구조화 요약)
                                        → paper_analysis
논문 본문 → 임베딩 배치 → paper_embeddings
사용자 질문 → 임베딩 → search_similar() → RAG → LLM → 챗봇 응답(chat_messages)
돌봄 기록(care_logs) → 월1회 배치 → profile_summaries → 개인화 피드(feed_items)
```

## 마이그레이션 (`backend/app/db/migrations/` — DB 스키마 정본)

- `001_init.sql`(테이블 정의, 팀 리뷰 후 확정)
  + `002_add_paper_metadata.sql`(논문 메타데이터 컬럼) + `003_embedding_dim_1024.sql`(임베딩 `vector(1024)` + HNSW)
  + `004`·`005`(환자 관리·진료 상세) + `006_profile_summaries_index.sql`(월간 요약 조회 인덱스 + upsert용 UNIQUE)
  + `007_paper_analysis_title_ko.sql`(`paper_analysis.title_ko` — 논문 제목의 한국어 번역,
    `summary_comparison` → `summary_background` 이름 변경)
  + `008_guideline_embedding.sql`(`guideline_knowledge`를 가이드라인 조각 + 임베딩 `vector(1024)` + HNSW로 확장)
  + `009_feed_items_cache.sql`(`feed_items`에 `batch_id`·`rank`·`category` + 인덱스 — AI 피드 저장본, 2026-10-15).
  + `010_user_feed_refresh_days.sql`(`users.feed_refresh_days`, 기본 3일, 1~7일 제약 + 진료·복약 날짜 무결성 트리거).
    사용자 요청으로 010에 트리거를 추가했으므로 기존 적용 DB도 재실행한다. 기존 갱신 주기는 유지하며, 상세 정책은 [`users.md`](users.md) 참고.
- 함수 입출력 스키마(pydantic)는 `backend/app/schemas.py`가 단일 출처다.

## 주요 경로
- `backend/main.py` — FastAPI 앱 진입점. 팀원 라우터는 여기에 등록.
- `backend/app/config.py` — 환경 변수(pydantic-settings).
- `backend/app/db/functions.py` — **데이터 접근 함수 (팀 공용 계약, 데이터 담당).**
- `backend/app/collectors/pubmed.py` — PubMed 수집(주 1회 배치).
- `backend/scripts/summarize_papers.py` — 논문 3칸 요약(연구 배경·새로 밝혀진 것·한계) + 한국어 제목 생성 배치 (OpenAI, 담당 박주현).
- `backend/app/api/` — API 라우터. 기능별 파일(`papers.py` …)에 `router`를 두고 `main.py`에서 등록.

## 데이터 접근 계약

팀원이 이 함수들을 통해서만 DB에 접근한다. 테이블은 `backend/app/db/migrations/001_init.sql`,
함수 입출력은 `backend/app/schemas.py`가 정본.
**아래 표를 바꿀 때는 항상 코드와 이 문서를 동시에 수정한다.**

`paper_analysis`는 논문당 한 줄인데 **칸마다 채우는 사람이 다르다** (2026-10-09, 김현서 님과 합의).
서로의 칸을 지우지 않도록 저장 함수도 나뉜다.

| `paper_analysis` 칸 | 채우는 사람 | 저장 함수 | 대상 고르는 함수 |
|---|---|---|---|
| `study_type`, `evidence_level` | 김현서 (근거 등급) | `save_evidence()` | `get_papers_without_evidence()` |
| `title_ko`, `summary_background` / `summary_finding` / `summary_limitation` | 박주현 (요약 배치 `scripts/summarize_papers.py`) | `save_summary()` | `get_new_papers()` |
| `guideline_relation`, `tags` | 허웅 — **지금은 채우는 배치가 없다.** `save_summary()`가 보낸 값으로 덮어쓰므로 요약 배치를 돌리면 비워진다 | `save_summary()` | |

**함수를 추가·변경한 사람이 위 "데이터 접근 계약" 표(와 `schemas.py`)도 같이 고친다.**
(계약 표를 바꾸기 전에는 팀에 공지한다 — "담당 영역" 규칙과 동일.)

| 함수 | 입력 | 반환 |
|------|------|------|
| `save_papers(papers: list[PaperIn])` | `papers`: 수집기가 만든 `PaperIn` 목록 | `SavePapersResult` |
| `get_new_papers(since=None, limit=100, source=None)` | `since`: ISO8601 날짜/시각(선택), `limit`: int, `source`: `'pubmed'` 등(선택) | `list[PaperOut]` |
| `save_summary(analysis: AnalysisIn)` | `AnalysisIn` (임베딩 없음) | 저장된 `paper_analysis` id (`UUID`) |
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

**사용자 피드 설정** — `app/db/user_functions.py`, 입출력 정본은 `app/user_schemas.py`:

| 함수 | 입력 | 반환 |
|---|---|---|
| `get_user_feed_settings(user_id)` | UUID 또는 UUID 문자열 | `FeedSettingsOut(user_id, feed_refresh_days)` 또는 None |
| `update_user_feed_settings(user_id, data)` | UUID 또는 UUID 문자열, `FeedSettingsUpdate` 또는 dict | 저장된 `FeedSettingsOut` 또는 None. 잘못된 설정은 ValueError |

`users.feed_refresh_days`는 SMALLINT NOT NULL DEFAULT 3, CHECK 1~7이다. 기존·신규 사용자 기본값은 3일.
사용자·간병인 조회 모델(`UserOut`, `UserSummary`, `CaregiverContext`, `UserProfileContext`)에도 이 값을 포함한다.
변경 시 계정의 `updated_at`만 갱신하고 피드 생성 시각은 바꾸지 않는다. 다음 AI 피드 요청부터 새 기간으로
저장본 만료를 판단한다. 자세한 API·검증 정책은 [`users.md`](users.md) 참고.

**가이드라인(기반 지식층)** — `backend/app/db/functions.py` (담당 박주현, 2026-10-14 추가, `008`).
**역할: 조각 나누기와 임베딩 생성은 AI(김현서), 저장·검색 함수는 DB(박주현).**

| 함수 | 입력 | 반환 |
|------|------|------|
| `save_guidelines(items: list[GuidelineChunkIn])` | 조각(본문 + 메타 + 임베딩) 목록. 같은 `(source_url, chunk_index)`면 **갱신(upsert)** | 저장·갱신한 조각 수 (`int`) |
| `search_similar_guidelines(query_embedding, k=5)` | `query_embedding`: `list[float]`(길이=`EMBEDDING_DIM`=**1024**), `k`: int | `list[GuidelineSearchResult]` — `id` + `score`만 |
| `get_guidelines_by_ids(ids: list[UUID])` | `guideline_knowledge.id` 목록 (`search_similar_guidelines()`가 돌려주는 `id`) | `list[GuidelineChunkOut]` — **입력 순서 그대로** |

**피드 개인화 조회·쓰기** — `backend/app/db/feed_read.py` (담당 박주현, 입출력 정본은 `app/feed_schemas.py`):

| 함수 | 입력 | 반환 |
|------|------|------|
| `get_feed_context_snapshot(*, user_id, reference_time, reference_date, recent_period_start, recent_period_end)` | 전부 키워드 전용. `reference_time`/`recent_period_*`는 **tz-aware** `datetime`, `reference_date`는 `date` | `FeedReadSnapshot` |
| `save_care_log(user_id, log_type, content=None, mood_tag=None, patient_id=None, logged_at=None)` | `log_type`: `patient_care` / `caregiver_selfcare`(필수), `patient_id`: 선택(그 간병인의 환자여야 함), `logged_at`: `None`이면 DB `now()` | 저장된 `care_logs` id (`UUID`) |
| `save_profile_summary(user_id, period_start, period_end, summary=None, updated_tags=None)` | 월간 요약 1건. 같은 `(user_id, period_start, period_end)`면 **갱신(upsert)** | `profile_summaries` id (`UUID`) |
| `save_feed_batch(user_id, items: list[FeedItemIn])` | AI 피드 한 묶음 (`paper_id, personal_reason?, category?, relevance_score?`). 목록 순서대로 `rank` 1부터. **트랜잭션 하나** — 없는 사용자·논문이 있으면 `ValueError`, 아무것도 저장 안 됨. 빈 목록도 `ValueError` | `SavedFeedBatch(batch_id, generated_at)` |
| `get_latest_feed_batch(user_id, *, max_age: timedelta, reference_time)` | `reference_time`은 **tz-aware**. `created_at > reference_time - max_age`인 가장 최근 묶음 | `FeedBatch(batch_id, generated_at, items)` — items는 `rank` 순, 항목마다 `personal_reason, category, relevance_score, is_saved, paper: PaperDetailResponse`. 없으면 `None` |
| `get_recent_feed_papers(user_id, *, days=30, limit=50, reference_time=None)` | 읽기 전용. 기간은 `(reference_time - days, reference_time]` — 미래 시각의 행은 빠진다. `reference_time`은 **tz-aware**, `None`이면 지금. `days`·`limit`은 1 이상 정수(아니면 `ValueError`) | `list[FeedRecentPaper(paper_id, pmid?, title, last_recommended_at, times_recommended)]` — **논문별로 묶음**(같은 논문이 여러 묶음에 있어도 1개), 최근 추천 순, 최대 `limit`개. `pmid`는 `source='pubmed'`일 때만 값(아니면 `None`). 이력 없음·없는 사용자는 `[]` |

**`get_recent_feed_papers()` 사용 예시** (Feed Agent가 같은 논문을 반복 추천하지 않게):

```python
from app.db.feed_read import get_recent_feed_papers

recent = get_recent_feed_papers(user_id, days=30, reference_time=reference_time)  # 피드와 같은 기준 시각

# (a) Agent 입력(1단계 context)에 넣는다 — LLM이 "이미 추천한 논문"을 알게
context["recently_recommended"] = [
    {"pmid": r.pmid, "title": r.title, "times_recommended": r.times_recommended} for r in recent
]

# (b) 후보를 고를 때 pmid로 뺀다 — LLM에 맡기지 않고 코드로 확실히
seen = {r.pmid for r in recent if r.pmid}
candidates = [c for c in candidates if c.pmid not in seen]
```

스키마 (요약) — **모든 id는 `UUID`**:
- **PaperIn** (`papers` 입력): `source, external_id, title, abstract?, published_date?, url?, journal?, doi?, publication_types, mesh_terms` — `id`/`collected_at`은 DB가 채운다.
- **SavePapersResult**: `total, inserted, skipped, inserted_ids`
- **PaperOut** (`papers`): `id, source, external_id, title, abstract?, published_date?, url?, collected_at`
- **AnalysisIn** (`paper_analysis`): `paper_id, study_type?, evidence_level?, guideline_relation?, title_ko?, summary_background?, summary_finding?, summary_limitation?, tags, model_name?`
  — ★2026-10-12 `embedding?` / `embedding_model?` **제거**. 임베딩 저장은 `save_embeddings()`로만 한다.
  - `study_type` / `evidence_level`은 **비워 보내면 기존 값을 유지한다** (등급 저장은 `save_evidence()`).
  - ★2026-10-13 `title_ko` / `summary_background` / `summary_finding` / `summary_limitation`에 **빈 문자열이나
    공백뿐인 값을 주면 `ValueError`** (비워 두려면 `None`). `save_summary()`도 저장 직전에 같은 검사를 한다.
    공백만 든 요약이 "요약 있음"으로 읽히던 문제 때문이다 (허웅 님 제보).
- **EvidenceIn** (`save_evidence` 입력): `paper_id, study_type`(필수, 빈 값 불가), `evidence_level?`(int 1~6, 등급 없음은 `None`)
- **SaveEvidenceResult**: `total, saved, not_found`(papers에 없던 `paper_id` 목록)
- **SearchResult** (`search_similar` 반환): `paper_id, score` — ★2026-10-01 축소. 제목·요약 등은 `get_papers_by_ids()`로 붙인다.
- **PaperDetail** (`get_papers_by_ids` 반환): `paper_id, external_id, title, abstract?, journal?, published_date?, publication_types, mesh_terms, doi?`
- **UpdateMetadataResult**: `total, updated, not_found`(papers에 없던 `external_id` 목록)
- **EmbeddingIn** (`paper_embeddings` 입력): `paper_id, embedding` — `embedding` 길이는 반드시 `EMBEDDING_DIM`(=1024).
- **SaveEmbeddingsResult**: `total, saved, not_found`(papers에 없던 `paper_id` 목록)
- **GuidelineChunkIn** (`save_guidelines` 입력, 가이드라인 문서의 한 조각): `doc_title`(가이드라인 이름), `section?`(장·절 제목),
  `chunk_index`(문서 안 조각 순서, 0 이상), `content`(조각 본문, **공백뿐이면 거부**), `topic`(주제), `source?`(발행 기관),
  `source_url`(원문 링크), `published_year?`, `language?`(`"ko"`/`"en"`), `tags`, `embedding`(길이 반드시 1024), `embedding_model`
  — `id`/`created_at`/`updated_at`은 DB가 채운다. **`(source_url, chunk_index)`가 조각을 가리키는 키다.**
- **GuidelineSearchResult** (`search_similar_guidelines` 반환): `id, score` — 논문의 `SearchResult`와 같은 모양.
- **GuidelineChunkOut** (`get_guidelines_by_ids` 반환): `id, doc_title, section?, chunk_index, content, source?, source_url,
  published_year?, language?, topic, tags` — 임베딩은 돌려주지 않는다.
- 화면용 응답 모델(`PaperListItem` / `PaperListResponse` / `PaperDetailResponse` / `RecommendationItem` /
  `RecommendationResponse`)과 `evidence_grade` 규칙은 [`api.md`](api.md)의 "응답 형식".
- **EMBEDDING_DIM** = `1024` (`schemas.py` 상수). DB의 `vector(1024)`와 같아야 한다 — 바꿀 때는 새 마이그레이션 + 이 상수 + `config.py`의 `embedding_dim`을 **함께** 고친다.


피드 개인화 스키마 (`app/feed_schemas.py`) — AI 쪽 `Source*` DTO와 **필드 이름·타입이 1:1**이다:
- **FeedReadSnapshot**: `caregiver_profile?`, `managed_patients`, `recent_care_logs`, `long_term_summary?`
- **FeedReadCollection[T]**: `items`(실제로 담은 기록 = I) + `total_count`(적격 조건을 모두 적용한 뒤,
  **상한으로 자르기 전** 건수 = E). `len(items) <= total_count`를 모델이 검증하고,
  `is_truncated`로 잘렸는지 알 수 있다. `total_count`는 **항상 별도 집계 쿼리**로 센다
  (`len(items)`로 대체하지 않는다 — 조회 실패와 '데이터 없음'을 구분할 수 없게 되므로).
- **FeedReadCaregiverProfile**: `user_id, relationship?, burden_score?, mood_score?, lifestyle_tags, updated_at`
- **FeedReadManagedPatient**: `patient_id, caregiver_id, dementia_stage?, diagnosis_date?, symptoms, interests, updated_at`
  + 자식 묶음 4개 `clinical_assessments / safety_events / medications / medical_visits`
- **FeedReadClinicalAssessment**: `patient_id, assessment_type, score?`(**`Decimal`** 그대로), `result_detail?, assessed_at`
- **FeedReadSafetyEvent**: `patient_id, event_date, has_fall, has_wandering, has_missing, note?`
- **FeedReadMedication**: `patient_id, drug_name, is_taking, start_date?, end_date?, note?` + **추가 칸** `dosage?, frequency?`
- **FeedReadMedicalVisit**: `patient_id, visit_date, is_visited, department?, visit_content?`(= **기타 자유 메모**)
  + **추가 칸** `visit_reason?, diagnosis?, treatment_content?, test_summary?, medication_change?, doctor_note?, follow_up_plan?`
- **FeedReadCareLog**: `user_id, patient_id?`(**`None`이면 환자 미연결 = 간병인 본인 기록**), `log_type, content?, mood_tag?, logged_at`
- **FeedReadLongTermSummary**: `user_id, period_start, period_end, summary?, updated_tags, generated_at`
- **일부러 넣지 않은 칸**: `hospital_name`, `assessed_by`, 환자 이름, 사용자 이름·이메일·전화번호
  (LLM에 보낼 이유가 없는 식별 정보라서 조회 단계에서 아예 뺀다.)
- **MAX_MANAGED_PATIENTS** = `20`. 넘으면 **자르지 않고** `FeedPatientLimitExceeded`를 던진다.


동작 메모:
- `save_papers()`는 `(source, external_id)` UNIQUE + `ON CONFLICT DO NOTHING`으로 이미 있는 논문을
  건너뛴다(기존 행은 갱신하지 않는다). 배치 안의 중복도 먼저 제거해 집계를 맞춘다.
- **★ 계약 변경 (2026-10-09)** `get_new_papers()` = `papers` 중 **`paper_analysis` 줄이 없거나
  `summary_finding`이 비어 있는** 논문. (이전: "줄이 없는 논문". 근거 등급이 먼저 들어가 줄이 생기면
  요약 대상에서 빠지던 문제 때문에 바꿨다.) **(2026-10-13)** 공백만 들어 있는 `summary_finding`도 비어 있는 것으로
  본다(`btrim`). 이제 "요약이 아직 없는 논문"이라는 뜻이고,
  등급이 있는지는 보지 않는다.
  정렬은 `published_date DESC NULLS LAST` → `collected_at DESC` → `id`. 한 배치는 `collected_at`이
  모두 같아서, 마지막 `id` 키가 있어야 `limit`을 준 결과 순서가 호출마다 흔들리지 않는다.
  `since`/`source`는 `None`이면 해당 조건을 적용하지 않고, `limit <= 0`이면 빈 목록을 돌려준다.
- `save_summary()`는 `paper_analysis`에만 upsert 한다 (`paper_id` UNIQUE → 재분석하면 기존 행을 갱신하고
  `generated_at`을 다시 찍는다. id는 그대로). `summary_finding`을 채워 저장한 논문은 `get_new_papers()` 목록에서 빠진다.
  **`paper_embeddings`는 전혀 건드리지 않는다** (2026-10-12 정리 — 임베딩 저장은 `save_embeddings()` 한 경로로만 한다.
  요약 배치와 임베딩 배치를 서로 기다리지 않고 따로 돌릴 수 있다).
  **(2026-10-09 변경)** `study_type` / `evidence_level`은 값을 보냈을 때만 바꾸고, 비워 보내면 기존 값을
  유지한다(`COALESCE`). 요약 칸·`title_ko`·`guideline_relation`·`tags`·`model_name`은 예전처럼 보낸 값으로 덮어쓴다.
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


가이드라인 함수 동작 (2026-10-14):
- `guideline_knowledge`의 한 줄 = 가이드라인 문서의 **한 조각(장·절 단위) + 그 조각의 임베딩**. 논문과 달리 본문과 임베딩을
  한 테이블에 두고 항상 같이 저장한다. 임베딩 모델·차원은 논문과 같다(bge-m3 / 1024).
- `save_guidelines()`는 INSERT 한 번(= 한 트랜잭션)으로 넣는다. `(source_url, chunk_index)`가 같으면 본문·메타·임베딩을
  모두 새 값으로 갱신하고 `id`는 그대로 둔다(`updated_at`만 다시 찍는다). 임베딩이 1024차원이 아니거나 본문이 공백뿐이면
  **DB에 가기 전에** `ValueError`(`GuidelineChunkIn`을 만들 때)가 나고, 그 배치는 아무것도 저장되지 않는다.
  배치 안의 중복 키는 첫 건만 쓴다. **조각을 지우지는 않는다** — 문서를 다시 나눠 조각 수가 줄면 뒤쪽 `chunk_index`의
  옛 조각이 남는다(지우는 함수는 아직 없다).
- `search_similar_guidelines()`는 코사인 유사도(`1 - 코사인 거리`) 내림차순으로 **`id`와 `score`만** 돌려준다.
  임베딩이 없는 줄은 빠진다. 질의 벡터가 1024차원이 아니면 `ValueError`, `k <= 0`이면 빈 목록.
  받은 `id`를 순서대로 `get_guidelines_by_ids()`에 넣으면 유사도 순서가 보존된 본문·출처가 나온다.
- `get_guidelines_by_ids()`는 **입력 순서를 유지**하고, DB에 없는 `id`는 예외 없이 빠진다(`get_papers_by_ids()`와 같은 규칙).
- 테스트: `python -m unittest tests.test_guidelines` (가짜 1024차원 임베딩, 실 DB + `008` 필요).


`feed_read.py` 정책 (2026-10-12, AI 담당과 확정):
- `get_feed_context_snapshot()`은 **사용자 확인부터 모든 조회·건수까지를 읽기 전용
  REPEATABLE READ 트랜잭션 하나**에서 처리한다. 모든 값이 같은 DB 상태를 보고, 쓰기는 DB 레벨에서 막힌다.
  **함수 안에서 `now()` / `today()`를 쓰지 않는다** — 시간 기준은 전부 인자로 받는다
  (호출하는 쪽 AI Loader의 시계와 어긋나지 않게 하기 위해서다).
- **실패를 빈 결과로 바꾸지 않는다.** `None` / 빈 목록 / `0`은 **모든 조회가 성공했을 때만** 나온다.
  - `ValueError` — 시각에 시간대가 없음, `recent_period_start >= recent_period_end`,
    `recent_period_end > reference_time` (DB에 가기 전에 검사).
  - `FeedUserNotFound`(`LookupError`) — 없는 사용자. **`except FeedReadError`에 걸리지 않는다.**
  - `FeedPatientLimitExceeded`(`FeedReadError`) — 관리 환자가 20명 초과. 환자 수를 담고 있다.
  - `FeedReadError`(`RuntimeError`) — 조회·건수·DTO 검증 실패. DB 예외를 감싼다.
- 컬렉션별 적격 조건(E)과 상한(I) — `total_count`는 **적격 조건을 모두 적용한 뒤, 상한으로 자르기 전** 건수:
  | 컬렉션 | 적격 조건 (E) | 정렬 | 상한 (I) |
  |---|---|---|---|
  | 관리 환자 | `caregiver_id = user_id` 전부 | `created_at, id` | **없음** (20명 초과면 예외. `total_count == len(items)`) |
  | 평가 | `assessed_at` ∈ `[reference_date - 365일, reference_date]` | `assessed_at DESC, created_at DESC, id` | 환자당 20 |
  | 안전 | `event_date` 최근 180일 **AND** `has_fall`/`has_wandering`/`has_missing` 중 하나라도 true | `event_date DESC` | 환자당 50 |
  | 복약 | 복용 중(`is_taking = true`) **또는** 최근 중단(`is_taking = false` AND `end_date` ∈ `[reference_date - 90일, reference_date]`) | `is_taking DESC, start_date DESC NULLS LAST, created_at DESC, id` | 환자당 30 |
  | 방문 | 지난 진료(`is_visited = true` AND `visit_date` ∈ `[reference_date - 365일, reference_date]`) **또는** 예정 진료(`is_visited = false` AND `visit_date` ∈ `[reference_date, reference_date + 90일]`) | `visit_date DESC, created_at DESC, id` | 환자당 20 |
  | 돌봄 기록 | `user_id` + `logged_at` ∈ `[recent_period_start, recent_period_end)` **AND** (`patient_id IS NULL` **또는** 현재 관리 환자) | `logged_at DESC, id` | 200 |
  | 월간 요약 | `period_end < reference_date` **AND** `generated_at <= reference_time` | `period_end DESC, generated_at DESC, id` | 1건 (없으면 `None`) |
- **안전 이벤트: 플래그가 전부 false인 날(= 확인했고 사건 없음)은 적격이 아니다** (`total_count`에도 안 들어간다).
- **★ 조건 변경 (2026-10-13) — 진료·복약은 상태와 날짜가 맞는 기록만 적격이다.** 허웅 님
  `ProductionFeedContextSource`는 아래 조합이 하나라도 오면 **스냅샷 전체를 거부**한다. 그래서 조회와
  건수(`total_count`) 양쪽에서 똑같이 뺀다.
  - 진료: **미래 + 방문 완료**, **과거 + 미방문**은 적격이 아니다. **오늘 날짜**는 미방문이면 예정 진료,
    방문 완료면 지난 진료다.
  - 복약: **중단인데 `end_date`가 미래이거나 `NULL`**이면 적격이 아니다. 복용 중인 약은 날짜와 무관하게 적격이다.
- **방문 선정은 두 갈래다**: 예정 진료(`is_visited = false`)를 **가까운 순으로 최대 5건** 먼저
  잡고, 남은 자리를 지난 진료가 최근 순으로 채운다. 한쪽이 모자라면 다른 쪽이 그 자리를 쓴다.
  최종 반환 순서는 `visit_date DESC, created_at DESC, id`.
- **돌봄 기록의 `patient_id IS NULL`은 그대로 담는다** (간병인 본인 자기돌봄 기록).
  다른 간병인의 환자에 붙은 기록은 적격이 아니다 (`total_count`에도 반영).
- 환자별 자식 기록은 **환자 수와 관계없이 쿼리 수가 고정**이다
  (`patient_id = ANY(...)` + 창 함수로 환자별 상한, `GROUP BY`로 건수. 건수 0인 환자는 `0`으로 채움).
- 배열 칸(`symptoms` / `interests` / `lifestyle_tags` / `updated_tags`)은 DB에서 nullable이라
  **`COALESCE`로 빈 목록**을 만든다 (AI 쪽 DTO는 non-null `list[str]`이다).
- `save_profile_summary()`의 upsert는 `006_profile_summaries_index.sql`의
  `(user_id, period_start, period_end)` UNIQUE 인덱스를 `ON CONFLICT` 대상으로 쓴다.
- 개발용 샘플: `python -m scripts.seed_dev_care_logs` (최근 30일 돌봄 기록 + 지난달 요약. 재실행 안전).
  테스트: `python -m unittest tests.test_feed_read`.

피드 저장본 (`feed_items`, `009`, 2026-10-15 — 10/14 회의):
- AI 피드 한 번 = 한 묶음(`batch_id`). 묶음 안 행은 `created_at`이 같고 이것이 응답의 `generated_at`이다.
  `personal_reason`은 기존 `personalized_explanation` 칸에 저장한다. 검색 방식(폴백) 결과는 저장하지 않는다.
- **예전 묶음은 지우지 않는다** — 북마크(`is_saved`)가 행마다 있어서, 지우면 북마크가 사라진다.
- 새 묶음을 저장할 때 이 사용자가 예전에 북마크한 논문이면 `is_saved=true`로 이어 붙인다
  (안 그러면 같은 논문이 새 묶음에 다시 나올 때 북마크가 풀린 것처럼 보인다).
  **나중에 북마크 API를 만들 때**: 목록은 `(user_id, paper_id)` 기준으로 중복을 없애고,
  해제는 그 사용자·논문의 **모든 행**을 `false`로 바꿔야 한다 (한 행만 바꾸면 다음 묶음에서 다시 살아난다).
- 테스트: `python -m unittest tests.test_feed_cache` (실 DB, `009`가 없거나 논문 3편 미만이면 skip).


요약 배치 (`backend/scripts/summarize_papers.py`, 담당 박주현, 2026-10-13):
- 요약이 없는 논문(`get_new_papers(limit)`)마다 OpenAI로 `title_ko` + 요약 3칸을 만들어
  `save_summary()`로 저장한다. `study_type` / `evidence_level`은 보내지 않으므로 기존 등급이 유지된다.
  저장된 논문은 대상에서 빠지므로 중간에 끊겨도 다시 실행하면 이어서 한다.
- **`.env`에 OpenAI 키와 `OPENAI_SUMMARY_MODEL`이 둘 다 있어야 한다.** 키는 `OPENAI_SUMMARY_API_KEY`(요약 배치 전용, 선택)가
  있으면 그것을, 없으면 `OPENAI_API_KEY`를 쓴다 — 실행할 때 어느 변수의 키를 쓰는지 이름만 출력한다. 모델은 기본값이 없고,
  비어 있으면 안내 메시지를 내고 멈춘다. `paper_analysis.model_name`에 이 모델 이름이 남는다.
- 실행 (`backend/`에서, `007` 적용 후):
  ```bash
  python -m scripts.summarize_papers --limit 20 --dry-run --out ../../summaries.md  # 저장 없이 결과만 파일로
  python -m scripts.summarize_papers --limit 20                                     # 20편 저장
  python -m scripts.summarize_papers --paper-ids <uuid> <uuid>                      # 지정한 논문만 (다시 만들기 포함)
  ```
  `--limit` 기본 20. `--dry-run`은 `--out`(마크다운 결과 파일)이 필요하다 — 결과 파일은 레포 밖에 둔다.
- 초록이 없는 논문은 건너뛰고 건수만 보고한다. 응답은 JSON 형식으로 강제하고(structured output),
  형식이 틀리거나 API가 실패하면 논문당 최대 2번 다시 시도한 뒤 **저장하지 않고 실패로 보고**한다.
  응답이 토큰 한도에서 잘린 경우도 실패로 본다. 요청마다 timeout 120초·최대 4,000 토큰,
  끝나면 총 토큰 사용량과 요청당 최대 출력 토큰(한도 여유 확인용)을 출력한다.
- 작성 규칙의 정본은 스크립트의 `SYSTEM_PROMPT`다. 칸 순서는 ① 연구 배경 → ② 새로 밝혀진 것 → ③ 한계.
  독자 기준은 **의학 지식이 없는 50~60대 부모님께 말로 설명하듯**, 어조는 일반 신문 기사처럼 차분하게.
  치매·알츠하이머병에는 설명을 붙이지 않고, 덜 알려진 병 이름만 논문 하나에서 처음 한 번 짧은 괄호 설명을 붙인다.
  일반 신문에 나오는 말(진단·임상시험·요양시설 등)은 그대로 쓰고, 그 밖의 전문용어는 일상어로 바꾼다.
  통계 수치(오즈비·신뢰구간·p값 등)는 말로 풀어 쓰며, 영어는 약 이름을 처음 쓸 때 괄호 안에 한 번만 쓴다.
  `title_ko`는 30자 안팎, 괄호 설명·영어 없음. ② 칸에는 결과만 쓰고 "더 연구가 필요하다"류는 ③ 한계에 쓴다.
- **두 번째 다듬기는 기본으로 끈다** (2026-10-13). `--polish`를 줄 때만 LLM을 한 번 더 불러(`POLISH_PROMPT`)
  모를 만한 단어를 쉬운 말로 바꾼다(내용·수치·비교 대상은 그대로). 두 프롬프트는 쉬운 말 규칙(`EASY_WORD_RULES`)을 같이 쓴다.
  다듬기에는 초록을 주지 않고 처음 쓴 글만 준다. 다듬기가 적어 준 "바뀐 표현(전 → 후)" 목록은 다듬기를 한 실행의
  `--out` 결과 파일에만 논문마다 실리고, 실제 글에서 찾지 못한 항목에는 "(글에서 확인 안 됨)"이 붙는다.
  시험 20편 기준으로 다듬기를 켜면 토큰이 약 2배(64,518 → 124,155)였고 제목·문장이 과하게 풀어지는 경향이 있었다.
- **자동 검사 `check()`** — 다듬기까지 끝난 뒤, 저장 전에 네 칸을 검사한다: 통계 표기(`p =`, `p<`, `CI`, `OR =`, `β`, `SD`),
  한글 뒤 괄호 밖의 영어 단어(2글자 이상), 한글 사이에 영문자가 낀 깨진 단어,
  **금지 단어**(`BANNED_WORDS`: 초조·항정신병·인지 기능·인지 저하·낙상·상호작용·위약·망상·일차의료·초록 — 제목 포함,
  띄어쓰기 무시, 단순 포함 검사라 "초조해하다"·"초록색" 같은 일상어도 걸린다),
  **약 이름 영어 괄호 빠짐**(LLM이 `drugs`에 적어 준 약 이름이 본문 ①→②→③에 처음 나오는 곳에 `(영어)`가 없을 때.
  영어 이름이 제목·초록에 실제로 있는 약만 검사하고, LLM이 `drugs`에서 빠뜨린 약은 잡지 못한다). 걸리면 그 논문만 이유를 알려 주고
  **한 번만** 다시 생성하고, 또 걸리면 저장하지 않고 실패 목록에 이유와 함께 남긴다.
  괄호 안이 약 이름인지는 구분하지 못한다(`인공지능(AI)`처럼 한글 뒤 괄호 안 영어는 모두 통과).
  **(2026-10-13 완화)** 단위(`mg`, `ml`, `kg` 등)와 숫자에 붙은 단위(`10mg`, `5mg/kg`)는 영어 단어로 보지 않고,
  한글 바로 뒤 괄호 안의 영어는 성분명·여러 단어(`(dextromethorphan-bupropion)`, `(donepezil, memantine)`)도 허용한다.
  LLM 응답의 네 칸은 앞뒤 공백을 지운 뒤 비어 있으면 형식 오류로 보고 다시 시도한다.
  **(2026-10-14)** 약·물질 코드명(영문 대문자 + 숫자: `DSP-2342`, `MW073`)은 영어 단어로 보지 않고,
  본문에 영문 그대로 쓴 약 이름에는 영어 괄호를 요구하지 않는다.
  `--out` 결과 파일 맨 위에 칸별 글자 수 통계, 토큰 사용량, 검사에 걸린 논문 번호와 재생성 결과,
  "치매"·"알츠하이머병" 뒤에 괄호 설명이 붙은 곳의 개수, 금지 단어·약 이름 괄호 검사 결과가 적힌다.
- 테스트: `python -m unittest tests.test_summarize_papers` (가짜 LLM, 실 DB 필요).

통합 배치 (`backend/scripts/update_papers.py`, 담당 박주현, 2026-10-14):
- **수집 → 임베딩 → 근거 등급 → 요약**을 한 번에 돌린다. 단계마다 아직 처리 안 된 논문만 고른다 —
  수집은 `save_papers()`의 중복 건너뛰기, 임베딩은 `get_papers_without_embedding('bge-m3')`, 등급은
  `get_papers_without_evidence()`, 요약은 `get_new_papers(--limit)`. 그래서 여러 번 돌려도 결과가 같고,
  한 단계가 통째로 실패해도 앞 단계 결과는 DB에 남아 다음 실행 때 이어서 처리된다.
- 임베딩·등급은 현서 님 `embed_papers.py`·`classify_papers.py`가 `main()`만 있어서 부르지 않고, 두 스크립트가 쓰는
  `embed_documents()`·`classify()`와 DB 함수를 같은 순서로 직접 부른다. 요약은 `summarize_papers.run()`을 그대로 쓰고
  키는 `OPENAI_SUMMARY_API_KEY` → `OPENAI_API_KEY` 순서(로그에는 변수 이름만).
- 겹쳐 실행 방지: 잠금 파일(Linux `fcntl.flock`, Windows는 `msvcrt`). 이미 실행 중이면 경고 로그만 남기고 종료 코드 0.
- 종료 코드: **0** 전부 성공 / **1** 일부 실패(다음 단계는 계속 진행) / **2** 실행 불가(DB 연결 실패, 키·`OPENAI_SUMMARY_MODEL` 없음).
- 로그: 시작·끝 시각, 단계별 처리·실패 건수와 걸린 시간. 화면과 `--log-file`에 같이 남긴다.
  PubMed 요청 URL에 `api_key`가 들어가므로 httpx 요청 로그는 끈다.
- 실행 (`backend/`에서):
  ```bash
  python -m scripts.update_papers --log-file logs/update_papers.log   # 전체 (요약 최대 100편)
  python -m scripts.update_papers --skip-collect --limit 20           # 수집 빼고, 요약 20편
  python -m scripts.update_papers --only classify                     # 한 단계만 (collect/embed/classify/summarize)
  python -m scripts.update_papers --dry-run                           # DB에 쓰지 않음 (요약은 LLM을 부르지만 저장 안 함)
  python -m scripts.update_papers --only summarize --retry-skipped    # 요약 3번 실패해 건너뛰던 논문도 다시 시도
  ```
  그 밖의 옵션: `--days-back`(수집 기간, 기본 7일 — cron 주기보다 길게), `--max-results`(수집 최대, 기본 100), `--lock-file`.
- 요약 실패 논문은 저장되지 않아 다음 실행 때 다시 시도되지만, 실패 횟수를 상태 파일(`--state-file`, 기본은 `--log-file`과 같은 폴더의 `update_papers_state.json`)에 논문 id별로 적어 **3번 실패하면 다음 실행부터 건너뛴다**(로그에 "건너뜀 n건 (PMID 목록)", 종료 코드 1의 원인 아님). 상태 파일이 없거나 깨지면 새로 시작하고, `--dry-run`은 기록하지 않는다.
- **자동 실행: AWS 서버(EC2 1대)에서 cron으로 실행 예정.** 예: `0 3 * * 1 cd /path/backend && .venv/bin/python -m scripts.update_papers --log-file logs/update_papers.log`
- 테스트: `python -m unittest tests.test_update_papers` (가짜 단계·가짜 모델, DB 불필요).


수집기(`collectors/pubmed.py`) 쪽 함수:
- `fetch_papers_by_pmids(pmids, require_abstract=True)` — PMID 목록으로 논문을 받아온다(검색 단계 없음).
  `FetchByPmidsResult(papers, missing_abstract, not_found)`를 돌려줘서, 초록이 없거나 PubMed에 없는
  PMID를 조용히 버리지 않는다. 저장은 `save_papers()`로 따로 한다.

> `functions.py`의 함수는 **모두 구현·검증 완료**다 (스텁 없음).

- **화면용 조회 함수 2개** (`list_papers` / `get_paper_detail`)는 `papers LEFT JOIN paper_analysis`로 읽기만 한다.
  요약이 아직 없는 논문은 요약 쪽 필드(`study_type`, `evidence_level`, `summary_*`)가 모두 `None`이다.
  - `list_papers()`는 전체 논문이 대상이다(`get_new_papers()`는 미분석 논문만). 정렬은 `get_new_papers()`와 같고
    (`published_date DESC NULLS LAST` → `collected_at DESC` → `id`), `total`은 `limit`/`offset`과 무관한 전체 개수다.
  - `get_paper_detail()`은 없는 id면 예외 없이 `None`을 돌려준다 (API가 404로 바꾼다).
    `get_papers_by_ids()`(AI용 계약, url·요약 없음)는 그대로 두고 따로 만든 함수다.
- `get_paper_details_by_pmids()`는 `get_paper_detail()`과 같은 형식을 **PMID로 여러 건** 받는다
  (검색 결과 Evidence Package에 `paper_id`가 없고 PMID만 있어서 둔 함수, `/recommendations`가
  검색 방식과 AI 방식의 `backend_handoff()` 양쪽에서 쓴다).
  읽기만 하고, 요약이 없으면 요약 쪽 필드가 `None`이다. **`papers`에 없는 PMID는 예외 없이 결과에 키가 없다.**
  순서는 보장하지 않으므로 호출한 쪽이 PMID로 찾아 쓴다.

## 결정사항: 논문 관련성 판단 (2026-09-23)

논문 관련성 판단은 두 단계로 한다:

1. **수집 단계**에서 치매/MCI 키워드로 필터링 — 전역 관련 여부는 이 단계에서 이미 보장된다.
2. **`feed_items.relevance_score`**로 사용자별 관련도만 계산한다.

논문 테이블에 별도의 전역 관련성 플래그(`is_relevant` 등)는 두지 않는다.
