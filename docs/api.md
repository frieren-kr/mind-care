# API

> 주소별 담당은 아래 표의 "담당" 설명을 따른다 (`papers.py`·`recommendations.py`는 박주현, `users.py`는 김한슬).
> 공통 규칙은 루트 [`AGENTS.md`](../AGENTS.md), DB 함수 계약은 [`db.md`](db.md).

**프론트 연결 상태 (2026-10-24)**: 피드(/recommendations), 논문 상세(/papers/{id}), 내 정보(/feed/{user_id}), 피드 갱신 주기(PATCH /users/{id}) API 연결 완료. 검색 모드 기준으로 응답 형식 확인, AI 모드 화면(personal_reason·category)은 AI 연결 후 확인 예정.
개발 환경(`APP_ENV=development`)에서만 `backend/main.py`가 CORS를 허용한다 (출처 `http://localhost:8081`·`http://127.0.0.1:8081`, 웹 화면 테스트용).

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
| `GET /users/{user_id}/feed-settings` | `api/users.py` | 피드 갱신 주기 조회(1~7일, 기본 3일). 없는 사용자는 404 | `FeedSettingsOut` |
| `PATCH /users/{user_id}/feed-settings` | `api/users.py` | JSON `{"feed_refresh_days": 5}` 저장. 정수 1~7만 허용, 잘못된 입력은 422, 없는 사용자는 404 | `FeedSettingsOut` |
| `GET /recommendations/{user_id}?patient_id=&k=5&refresh=false` | `api/recommendations.py` | **간병인(관리 환자 전체)** 기준 논문 최대 `k`편. 응답의 `mode`가 `ai`(FeedWorkflow) 또는 `search`(검색 방식, 폴백). AI 결과는 저장 기간(기본 3일) 동안 저장본을 다시 준다(`refresh=true`면 새로 만든다). `k` 1~5(기본 5), 범위를 벗어나면 422. `patient_id`는 선택 — 주면 검색 방식 응답만 그 환자로 좁힌다. 환자가 0명이어도 200. 없는 사용자·이 사용자의 환자가 아닌 `patient_id`는 404 | `RecommendationResponse` |

- `/feed…` 주소 3개(담당: 김한슬)는 [`users.md`](users.md) 참고.
- `/feed/{user_id}`와 `/feed_user/{user_id}` 응답에는 최상위 `feed_refresh_days`를 포함한다.
  피드 설정 API의 응답은 `{user_id, feed_refresh_days}`다. 설정 변경 시 기존 피드는 유지하고
  다음 AI 피드 요청부터 기존 생성 시각에 새 기간을 적용한다. 설정 저장 자체는 피드를 생성하지 않는다.
- `/recommendations`(담당: 박주현)는 **두 방식**으로 응답하고, 어느 쪽인지 응답의 `mode`에 적는다.
  **★ 2026-10-13 정식(간병인 기준) 경로로 전환** — 추천 기준은 환자 한 명이 아니라 간병인의 관리 환자 전체다.
  사용자·환자 확인(404 판정)과 검색어 재료는 `user_functions.get_user_profile_context(user_id)` 한 번으로 읽는다.
  - **`mode="ai"`** — 허웅 님 `FeedWorkflow` 3단계. `AgentContext(request_id=uuid4, user_id, patient_id=None)`를 만들어
    `context_prepare()` → `feed_execution`(LLM + 검색) → `backend_handoff()` 순서로 돈다.
    - `context_prepare(context)`는 **정식 Stage 1**이다 (`app/ai/agent/README.md`의 Production 조립 예시 그대로):
      `ProductionFeedContextSource(read_snapshot=get_feed_context_snapshot)` →
      `DefaultFeedContextLoader(source, clock, service_timezone=APP_TIMEZONE)` → `prepare_production_feed_input(context, loader)`.
      `clock`은 `datetime.now(APP_TIMEZONE)`(tz-aware)이다. 데모 경로(`prepare_demo_feed_input`)는 더 이상 쓰지 않는다.
    - **`patient_id`를 줘도 AI 방식은 관리 환자 전체(0~20명)가 기준이다** (`feed_scope="caregiver_all"`).
      환자가 0명이면 빈 환자 목록으로 그대로 돈다(cold start).
    - `backend_handoff(result)`는 순수 함수다. `result.output.items`와 `result.sources`를 짝지어
      `sources[].pmid`로 `get_paper_details_by_pmids()`를 한 번 부르고, `personal_reason` / `category`만 AI 결과에서,
      나머지(`id`, `title`, `title_ko`, … 요약 3칸)는 DB에서 채운다.
      **`pmid`가 없거나 DB에 없는 항목은 뺀다.** `relevance_score`는 `null`.
  - **`mode="search"`** — `search_recommendations()`. 환자마다 `build_queries()`로 **영어 검색어를 여러 줄** 만들어
    현서 님 `search_by_queries(검색어 목록, k)`로 합친다(검색어별 상위 논문을 번갈아 뽑고 중복은 한 번만) →
    `get_paper_details_by_pmids()`. `relevance_score`와 응답의 `queries`(이번에 쓴 검색어 목록, 데모·디버깅용)가
    채워지고 AI 쪽 2칸(`personal_reason`·`category`)은 `null`이다.
    - ★2026-10-21 한국어 한 줄 → 영어 여러 줄. 현서 님 점검에서 한국어 한 줄은 관련 0/5, 영어로 증상별로 쪼개면 4/5.
      영어 검색어에 caregiver/family를 넣으면 보호자 본인 논문이 끌려와 2/5 → **단어표 값 말고는 이 단어를 넣지 않는다.**
    - 단어는 `app/api/search_terms.py` 단어표(`STAGE_EN`·`SYMPTOM_EN`·`INTEREST_EN`·`INTEREST_EXPAND`)로 바꾼다.
      띄어쓰기는 무시하고 찾으며("수면장애" = "수면 장애"), 표에 없는 단어는 한국어 그대로 둔다.
    - 증상 1개당 `"{증상} in {단계}: interventions and management"`, 관심사 1개당 `"{관심사} for people with {단계}"`.
      관심사가 `INTEREST_EXPAND`(약물 치료·비약물 치료)에 있으면 한 줄 대신 펼친 줄들. 단계가 없거나 표에 없으면 `dementia`.
      약물 치료의 항아밀로이드 항체 줄은 **경도인지장애·경도일 때만** 넣는다(다른 단계면 첫 줄만).
      증상·관심사 줄을 번갈아 넣고 **환자 1명당 최대 4줄**. 둘 다 없으면 `"{단계} care: interventions and management"` 한 줄.
    - 여러 환자는 "환자별 1번째 줄 → 2번째 줄 …" 순서로 섞고, 같은 줄은 한 번만 쓴다. 섞은 뒤 **전체 `k`줄까지만** 쓴다.
    - `patient_id`를 주면 **그 환자 검색어만** 쓴다.
    - 환자가 0명이면 일반 검색어 하나(`"dementia care: interventions and management"`)를 쓴다.
    - 요청의 `interests`(간병인 관심사)는 `"{관심사} for people with dementia"` 줄로 바꿔 **"환자 한 명 더"처럼 섞기 순서에 넣는다**
      (환자가 여러 명이어도 관심사 1번째 줄이 첫 바퀴에 들어간다).
  - **`patient_id`는 선택 값이다.** 주면 그 간병인의 환자인지 확인하고(아니면 404), 응답의 `patient_id`에 그대로 적는다.
    안 주면 `null`. (이전: 환자가 2명 이상인데 생략하면 400, 환자 0명이면 404 → **둘 다 없앴다**.)
  - **폴백 규칙** — 아래 경우에는 `search` 방식으로 대신 응답한다(항상 200).
    - `get_feed_execution()`이 `None`일 때
    - `FeedWorkflowError`나 다른 예외가 났을 때 (`stage`와 예외 종류를 warning 로그로 남긴다. 메시지는 남기지 않는다)
    - AI 결과를 걸러낸 뒤 남은 항목이 0개일 때
  - **AI 연결: `get_feed_execution()`** (`api/recommendations.py`의 FastAPI 의존성).
    `backend/.env`의 `OPENAI_API_KEY`와 `OPENAI_SUMMARY_MODEL`로
    `OpenAIClient` + `build_evidence_registry()` + `FeedExecutionV1`을 조립한다.
    모델은 논문 요약 배치와 같은 설정을 쓰며 기본값은 없다.
    GPT-5.6 Luna를 사용하려면 `OPENAI_SUMMARY_MODEL=gpt-5.6-luna`로 설정한다.
    실제 검색 도구는 `RagEvidenceRetriever`를 통해 기존 RAG 검색을 호출한다.
    키/모델이 비어 있거나 초기화에 실패하면 `None`을 반환하여 검색 방식으로 응답한다.
    실행기(초기화 실패 시 `None` 포함)는 프로세스 안에서 재사용하므로 **설정 변경 후 서버를 재시작한다**.
    `.env`는 실행 디렉터리와 관계없이 `backend/.env`에서 읽고, 프로세스 환경 변수가 우선한다.
    테스트는 `app.dependency_overrides[get_feed_execution]` 또는 가짜 HTTP 응답으로 외부 호출을 막는다.
    - 실제 검증 담당자: 키와 모델을 `.env`에 설정하고 서버를 재시작한 뒤
      `GET /recommendations/{user_id}?k=5&refresh=true`를 호출한다 (`interests`는 생략).
      HTTP 200만으로 성공을 판단하지 말고 `mode="ai"`와 `items[].personal_reason`을 확인한다.
      `generated_at`은 저장 성공 시 채워진다. `mode="search"`이면 서버의 AI 초기화/실행 경고 로그를 확인한다.
      DB·논문 임베딩·사용자 데이터가 필요하며, 이 연결 변경의 자동 테스트는 실제 Provider를 호출하지 않는다.
  - **AI 피드 저장 기간** (2026-10-15, 10/14 회의) — `mode="ai"` 결과는 `feed_items`에 한 묶음으로 저장한다
    (`feed_read.save_feed_batch()`, `k`와 상관없이 AI 결과 전부). 다음 요청부터 **저장 기간 안이면 실행기를 부르지 않고**
    저장본(`get_latest_feed_batch()` + 논문 상세·근거 등급)을 돌려준다. 기간이 지나면 새로 만든다.
    - 기간은 `get_feed_cache_days(user_id)`가 `get_user_feed_settings()`로 읽은 `users.feed_refresh_days`다.
      기본 3일, 사용자 설정은 1~7일. 만료 기준은 마지막 생성 시각 + 설정 일수이며, 정확히 만료된 시각부터 새로 만든다.
      요청 시 검사하는 방식이며 백그라운드 자동 갱신 작업은 없다.
    - `refresh=true` — 기간과 상관없이 새로 만들고 저장한다 (이후 요청은 이 새 묶음을 받는다).
      실행기가 없거나 실패하면 지금처럼 검색 방식으로 응답한다.
    - **검색 방식 결과는 저장하지 않는다.** 실행기가 `None`이면(지금 기본값) 저장본도 보지 않는다.
    - 저장본 읽기·저장이 실패해도(예: `009` 미적용) 경고 로그만 남기고, 새로 만들거나 저장 없이 응답한다.
  - `k`는 1~5 (AI 결과가 최대 5개). 404 판정은 두 방식 공통이고 AI를 부르기 전에 끝난다.
  - bge-m3는 **서버 시작 때 미리 불러온다**(`main.py` lifespan, 걸린 시간은 시작 로그에). `PRELOAD_EMBEDDING_MODEL=0`이면 건너뛰고 첫 요청 때 불러온다(테스트는 `tests/__init__.py`가 끈다). 예열이 실패해도 서버는 뜨고 경고만 남긴다.
  - 테스트: `backend/`에서 `python -m unittest tests.test_recommendations` (실 DB 필요. 저장 기간은 `FeedCacheTests`가 DB 없이 확인).

- 요약(`paper_analysis`)이 아직 없는 논문은 요약 쪽 필드가 `null`로 나온다.
- `evidence_level`은 DB에 저장된 값(`'1'`~`'6'` 또는 `null`)을 그대로 내보내고, 화면 표시용 `'A'`~`'D'`는
  `evidence_grade`에 함께 담는다 (아래 "응답 형식"). 가이드라인(`'guideline'`) 표시 규칙은 아직 없다.

## 응답 형식 (요약)

응답 형식의 정본은 `backend/app/schemas.py`이고, 실행 중인 서버의 `/docs`(Swagger)에서 예시와 함께 볼 수 있다.

- **PaperListItem** (`list_papers` 목록 한 줄): `id, title, published_at?, journal?, evidence_level?, evidence_grade?, title_ko?, summary_finding?` — 초록 없음.
- **PaperListResponse**: `total`(전체 논문 수), `limit, offset, items`(`PaperListItem` 목록)
- **PaperDetailResponse** (`get_paper_detail` 반환): `id, title, abstract?, published_at?, journal?, pubmed_url?, doi?, publication_types, mesh_terms` + 요약 `study_type?, evidence_level?, evidence_grade?, title_ko?, summary_background?, summary_finding?, summary_limitation?`
  - **summary_background** (★2026-10-13 이름 변경, `007`) = 예전 `summary_comparison`. 뜻도 "기존 권고와 비교"에서
    **"연구 배경"**(왜 이 연구를 했는지)으로 바뀌었다. 세 칸 순서는 `summary_background` → `summary_finding` → `summary_limitation`.
  - **title_ko** (★2026-10-13 추가, `007`) = 논문 제목의 한국어 번역. 요약 배치가 채우고, 요약 전이면 `None`.
    목록·상세·추천 항목 응답에 모두 들어간다. 원제는 `title`에 그대로 있다.
  - 이 세 모델은 **화면 이름**(`frontend/src/types/index.ts`)을 쓴다: `papers.id`→`id`, `published_date`→`published_at`, `url`→`pubmed_url`.
- **RecommendationItem** (`GET /recommendations` 항목 한 줄): `id, title, published_at?, journal?, pubmed_url?` + 요약 `study_type?, evidence_level?, evidence_grade?, title_ko?, summary_background?, summary_finding?, summary_limitation?` (여기까지는 두 `mode` 모두 DB에서 온다)
  - `relevance_score?`(float) — `mode="search"`일 때만. **`ai`면 `None`** (AI 결과에는 점수가 없다).
  - `personal_reason?, category?`(`treatment`/`care`/`prevention`/`diagnosis`) — **AI 결과에서 오는 칸은 이 둘뿐이다.**
    `mode="ai"`일 때만 채워지고 `search`면 둘 다 `None`.
  - ★2026-10-13 `headline` / `summary_bullets` **제거** (허웅 님 `FeedAnswerV2`에서 AI 결과의 두 칸이 빠졌다).
    카드 제목은 `title_ko`(없으면 `title`), 요약은 요약 3칸을 쓴다 — 모두 DB(`paper_analysis`)에서 온다.
- **RecommendationResponse**: `user_id`, `patient_id?`(**요청에 준 값 그대로**, 안 줬으면 `None` = 관리 환자 전체 기준),
  `mode`(`"ai"` | `"search"` — 어느 방식으로 만들었는지),
  `queries?`(`list[str]` — 이번에 쓴 검색어 목록. `search`일 때만, `ai`면 `None`. ★2026-10-13 `query: str` → `queries: list[str]`),
  `items`(`RecommendationItem` 목록, 최대 5개. `search`면 검색어(환자)별 상위 논문을 번갈아 뽑은 순 —
  검색어가 하나면 관련도 높은 순, `ai`면 AI가 정한 순),
  `generated_at?`(★2026-10-15 추가, tz 포함 시각) — 저장된 AI 피드 묶음의 생성 시각. 저장본을 다시 줄 때도 **처음 만든 시각**이다.
  `search`이거나 저장에 실패했으면 `None`. 화면에서 "○일 전 추천" 표시에 쓸 수 있다.
- **evidence_grade** (위 세 응답 모델 공통) = 화면 표시용 근거 등급 `"A"`~`"D"`. 등급이 없으면 `None`.
  DB의 `evidence_level`(문자 `"1"`~`"6"`)을 **묶은 값**이고, `evidence_level`은 그대로 함께 돌려준다.
  변환 규칙의 정본은 AI 쪽 `app/ai/retrieval/evidence.py`의 `to_grade()` (`1→A, 2→B, 3·4→C, 5·6→D`).
  **채우는 곳은 API 계층(`app/api/evidence_grade.py`)이다** — `app/db/`는 `app/ai/`를 import 하지 않기
  때문에 조회 함수는 `evidence_level`만 돌려주고, 응답을 만들 때 등급을 덧붙인다.
  `to_grade()`는 **정수**를 받으므로 문자열을 그대로 넘기면 조용히 `None`이 된다 (여기서 정수로 바꿔 넘긴다).
