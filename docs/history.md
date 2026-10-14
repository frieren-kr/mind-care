# 상태·변경 기록

> 날짜별 기록과 해결된 이슈. **지금 규칙·계약은 [`db.md`](db.md) / [`api.md`](api.md)가 정본**이고, 여기는 이유가 궁금할 때만 본다.

## 상태 기록

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
- 아직 AI 파이프라인(분류·요약·실제 임베딩 생성)과 화면이 없다. (당시 기록 — 이후 임베딩·요약·근거 등급 배치가 생겼다.)
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
  **같은 덤프로 복원하면 팀원 모두 `paper_id`가 동일해진다.** 덤프는 레포에 없고 팀 단톡(카톡)에서 받는다
  (복원 방법은 `README.md`의 "팀원 데이터 맞추기").
- **근거 등급·요약 저장 경로 분리 완료** (2026-10-09, 실 DB). `save_evidence()` /
  `get_papers_without_evidence()` 추가, `get_new_papers()` 기준 변경(**계약 변경**), `save_summary()`가
  등급 칸을 비워 보내면 유지하도록 수정. 등급 먼저 → 요약, 요약 먼저 → 등급 두 순서 모두 서로의 칸이
  남는 것, 등급만 넣은 논문이 `get_new_papers()`에 계속 나오는 것, "등급 없음" 논문이 등급 대상에서
  빠지는 것, 1~6 밖의 값 거부를 논문 3편으로 확인하고 테스트 줄은 삭제했다.
- **논문 추천 API 첫 버전 구현·검증 완료** (2026-10-10, 실 DB). `GET /recommendations/{user_id}` +
  `get_paper_details_by_pmids()`. 샘플 간병인 4명(환자 5명) 전부 Top-5 반환, 400·404·422,
  임시 요약 1건이 붙어 나오는 것까지 확인하고 테스트 줄은 삭제했다. 서버를 띄운 뒤 첫 요청은 약 11초
  (bge-m3 로드, CPU), 그 뒤로는 약 0.4초.
- **`/recommendations` ↔ FeedWorkflow 연결(백엔드 쪽) 구현·검증 완료** (2026-10-11, 실 DB). **응답 계약 변경**:
  `mode` 추가, `query`·`relevance_score` null 가능, `headline`/`summary_bullets`/`category` 추가, `k` 최대 20 → 5.
  (`headline`/`summary_bullets`는 2026-10-13에 다시 제거했다 — 아래 참고.)
  가짜 `feed_execution`으로 `mode="ai"`(AI 칸 + DB 칸), 예외·0건일 때 `search` 폴백, 400·404·422를
  `tests/test_recommendations.py`(7건)로 확인했다. **실제 `feed_execution`은 아직 없어서(`get_feed_execution()`이
  `None`) 지금 응답은 항상 `mode="search"`다.**
- **`/recommendations` 정식(간병인 기준) 경로 전환 + 실 DB 통합 검증 완료** (2026-10-13, 실 DB). **응답 계약 변경**:
  `query` → `queries`(목록), `patient_id` null 가능, 환자 2명 이상 + `patient_id` 생략의 400과 환자 0명의 404 제거.
  `feed_read.py`의 진료·복약 적격 조건을 허웅 님 adapter 정책에 맞췄다(위 "조건 변경").
  샘플 간병인 4명(환자 5명) 전부 실 DB로 정식 Stage 1을 통과했고(관리 환자 전원·돌봄 기록 9건·월간 요약 1건 포함,
  이름·이메일·전화번호·병원명·raw id 없음), 가짜 `feed_execution`으로 FeedWorkflow가 `mode="ai"`로 끝까지 도는 것을
  `tests/test_recommendations.py`(12건) · `tests/test_feed_read.py`(44건)로 확인했다.
  **실제 `feed_execution`은 아직 없어서 지금 응답은 항상 `mode="search"`다.**
- **논문 요약 배치 + `title_ko` 추가** (2026-10-13, `007_paper_analysis_title_ko.sql`). **응답 계약 변경**:
  `/papers` 목록·상세와 `/recommendations` 항목에 `title_ko` 추가, `AnalysisIn`에 `title_ko` 추가,
  `summary_comparison` → `summary_background`(연구 배경) 이름 변경.
  ※ `app/ai/agent/README.md`에 옛 이름(`summary_comparison`)이 한 군데 남아 있다 (AI 담당 문서라 건드리지 않음).
  가짜 LLM으로 정상 저장·초록 없는 논문 건너뛰기·형식 오류 미저장·dry-run 미저장·기존 등급 유지를
  `tests/test_summarize_papers.py`(15건, 다듬기·자동 검사·재생성·공백 거부 포함)로 확인했다.
- **팀 기준 요약·근거 등급 데이터** (2026-10-13, 실 DB). `paper_analysis` 1,000행:
  **요약 + `title_ko` 996편**(`gpt-5.6-luna`), **근거 등급 판단 1,000편**(현서 님 `scripts/classify_papers.py` —
  A 112 / B 84 / C 408 / D 144 / 등급 없음 252). 등급 배치를 요약 뒤에 돌려도 요약 칸이 그대로인 것을 확인했다.
  요약이 없는 4편(PMID 42302487, 42257639, 41677635, 41114286)은 자동 검사를 두 번 통과하지 못해 저장하지 않았다.
  **(2026-10-14)** 코드명 허용 뒤 2편(42302487, 41677635)을 더 저장해 **로컬 DB는 998편**이다. 남은 2편은
  42257639(본문에 영어 `Botulinum toxin`)와 41114286(본문에 생쥐 모델 이름 `APP`)이다.
  덤프(`paper_analysis_20261014.sql`)는 이 998편 기준이다 (이전 `20261013` 덤프는 996편).
  덤프는 레포에 없고 팀 단톡(카톡)에서 받는다 — 복원 방법은 `README.md`의 "팀원 데이터 맞추기".
- **공백 요약 버그 수정** (2026-10-13, 허웅 님 제보). 공백뿐인 요약 칸은 LLM 응답 검증·`AnalysisIn`·`save_summary()`
  세 곳에서 거부하고, `get_new_papers()`는 공백만 든 `summary_finding`도 "요약 없음"으로 본다(**계약 변경**).
  수정 시점에 DB에는 공백·빈 문자열 행이 0건이었다.
- **추천 응답에서 `headline`·`summary_bullets` 제거** (2026-10-13). **응답 계약 변경**: 허웅 님 `FeedAnswerV2`에서
  AI 결과의 두 칸이 빠진 데 맞춰 `RecommendationItem`에서도 지웠다. AI 결과에서 오는 칸은 `personal_reason`·`category`뿐이고,
  제목(`title_ko`)과 요약 3칸은 DB에서 온다. `frontend/`의 타입·목업에는 아직 `summary_bullets`가 남아 있다(프론트 담당 영역).
  → **(2026-10-14)** 프론트 `5fa0d3a`에서 `/recommendations` 형식으로 바뀌어 해결됐다.
- **가이드라인 저장·검색 함수 3개 구현·검증 완료** (2026-10-14, 실 DB, `008_guideline_embedding.sql`).
  `save_guidelines()` / `search_similar_guidelines()` / `get_guidelines_by_ids()`. 가짜 1024차원 임베딩으로 저장 →
  같은 키 재저장 시 갱신(줄 수·`id` 유지) → 유사도 순 검색 → 입력 순서 유지 조회 → 768·1536차원과 공백 본문 거부를
  `tests/test_guidelines.py`(8건)로 확인하고 테스트 조각은 지웠다. **실제 가이드라인 데이터는 아직 없다**
  (`guideline_knowledge` 0건 — 조각 나누기·임베딩 생성은 현서 님 담당).
- **팀 협업 규칙(위 섹션)은 초안 — 팀 합의 대기 중.**

## 해결된 이슈

**해결됨 (2026-10-01) — 임베딩 차원은 1024(bge-m3)로 확정.**
`001_init.sql`의 `vector(768)`과 `config.py`의 `embedding_dim=1536`이 어긋나 있던 문제는
`003_embedding_dim_1024.sql`로 정리했다. 이제 세 곳이 모두 1024다 —
DB(`vector(1024)`), `schemas.EMBEDDING_DIM`, `config.embedding_dim`.
**차원을 다시 바꿀 때는 새 마이그레이션 파일 + 이 세 곳을 한 커밋에서 같이 고친다.**
(`001_init.sql`은 이미 적용된 파일이라 수정하지 않는다.)

- **(2026-10-14 문서 분리 때 정리)** API 문서의 "`evidence_level`을 화면의 `'A'~'D' | 'guideline'` 등급으로 바꾸는 규칙은
  **아직 미정**"이라는 문장은 `evidence_grade`(`1→A, 2→B, 3·4→C, 5·6→D`)가 생기면서 맞지 않게 되어 고쳤다.
  같은 문서의 "`search`면 AI 쪽 4칸은 `null`"은 `headline`·`summary_bullets` 제거 뒤 2칸(`personal_reason`·`category`)으로 고쳤다.
- **(2026-10-14)** `AGENTS.md`를 공통 규칙·담당 표만 남기고 상세 내용을 `docs/`(`db.md`·`api.md`·`users.md`·`ai.md`·`history.md`)로 옮겼다.
