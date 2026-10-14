# API

> 주소별 담당은 아래 표의 "담당" 설명을 따른다 (`papers.py`·`recommendations.py`는 박주현, `users.py`는 김한슬).
> 공통 규칙은 루트 [`AGENTS.md`](../AGENTS.md), DB 함수 계약은 [`db.md`](db.md).

**프론트 연결 상태 (2026-10-14, `5fa0d3a`)**: `frontend/src/types/index.ts`의 추천 카드·논문 상세 타입은 `/recommendations`
형식(`title_ko`, `summary_finding` 등 요약 3칸, `personal_reason`, `evidence_grade`)에 맞춰졌다. 화면은 아직 API를 부르지 않고 mock 데이터(`frontend/src/mocks/`)를 쓴다.

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
| `GET /recommendations/{user_id}?patient_id=&k=5` | `api/recommendations.py` | **간병인(관리 환자 전체)** 기준 논문 최대 `k`편. 응답의 `mode`가 `ai`(FeedWorkflow) 또는 `search`(검색 방식, 폴백). `k` 1~5(기본 5), 범위를 벗어나면 422. `patient_id`는 선택 — 주면 검색 방식 응답만 그 환자로 좁힌다. 환자가 0명이어도 200. 없는 사용자·이 사용자의 환자가 아닌 `patient_id`는 404 | `RecommendationResponse` |

- `/feed…` 주소 3개(담당: 김한슬)는 [`users.md`](users.md) 참고.
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
  - **`mode="search"`** — `search_recommendations()`. 환자마다 `build_query()`로 검색어를 하나씩 만들어
    현서 님 `search_by_queries(검색어 목록, k)`로 합친다(검색어별 상위 논문을 번갈아 뽑고 중복은 한 번만) →
    `get_paper_details_by_pmids()`. `relevance_score`와 응답의 `queries`(이번에 쓴 검색어 목록, 데모·디버깅용)가
    채워지고 AI 쪽 2칸(`personal_reason`·`category`)은 `null`이다.
    - `patient_id`를 주면 **그 환자 검색어 하나만** 쓴다.
    - 환자가 0명이면 일반 검색어 하나(`"치매 가족 돌봄에 도움이 되는 연구"`)를 쓴다.
    - `build_query()`는 환자 단계·증상·관심사를 문장에 끼워 넣는 검색 방식 전용 함수다.
  - **`patient_id`는 선택 값이다.** 주면 그 간병인의 환자인지 확인하고(아니면 404), 응답의 `patient_id`에 그대로 적는다.
    안 주면 `null`. (이전: 환자가 2명 이상인데 생략하면 400, 환자 0명이면 404 → **둘 다 없앴다**.)
  - **폴백 규칙** — 아래 경우에는 `search` 방식으로 대신 응답한다(항상 200).
    - `get_feed_execution()`이 `None`일 때
    - `FeedWorkflowError`나 다른 예외가 났을 때 (`stage`와 예외 종류를 warning 로그로 남긴다. 메시지는 남기지 않는다)
    - AI 결과를 걸러낸 뒤 남은 항목이 0개일 때
  - **★ 교체 지점: `get_feed_execution()`** (`api/recommendations.py`의 FastAPI 의존성). **지금은 `None`을 돌려줘서
    실제 응답은 항상 `mode="search"`이고 `personal_reason` 등 AI 쪽 칸은 `null`이다.** 허웅 님 `search_evidence`
    핸들러·레지스트리·LLM 클라이언트가 나오면 이 함수만 `(AgentContext, str) -> StructuredAgentResult`를
    돌려주도록 바꾼다(입력은 정식 경로의 `caregiver_all` JSON). 테스트는 `app.dependency_overrides[get_feed_execution]`으로 바꿔 끼운다.
  - `k`는 1~5 (AI 결과가 최대 5개). 404 판정은 두 방식 공통이고 AI를 부르기 전에 끝난다.
  - bge-m3는 **서버 시작 때 미리 불러온다**(`main.py` lifespan, 걸린 시간은 시작 로그에). `PRELOAD_EMBEDDING_MODEL=0`이면 건너뛰고 첫 요청 때 불러온다(테스트는 `tests/__init__.py`가 끈다). 예열이 실패해도 서버는 뜨고 경고만 남긴다.
  - 테스트: `backend/`에서 `python -m unittest tests.test_recommendations` (실 DB 필요).

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
  검색어가 하나면 관련도 높은 순, `ai`면 AI가 정한 순)
- **evidence_grade** (위 세 응답 모델 공통) = 화면 표시용 근거 등급 `"A"`~`"D"`. 등급이 없으면 `None`.
  DB의 `evidence_level`(문자 `"1"`~`"6"`)을 **묶은 값**이고, `evidence_level`은 그대로 함께 돌려준다.
  변환 규칙의 정본은 AI 쪽 `app/ai/retrieval/evidence.py`의 `to_grade()` (`1→A, 2→B, 3·4→C, 5·6→D`).
  **채우는 곳은 API 계층(`app/api/evidence_grade.py`)이다** — `app/db/`는 `app/ai/`를 import 하지 않기
  때문에 조회 함수는 `evidence_level`만 돌려주고, 응답을 만들 때 등급을 덧붙인다.
  `to_grade()`는 **정수**를 받으므로 문자열을 그대로 넘기면 조용히 `None`이 된다 (여기서 정수로 바꿔 넘긴다).
