# AGENTS.md

이 저장소에서 작업하는 **모든 팀원과 AI 코딩 도구가 공통으로 읽는 문서**다.
여기에는 **모두가 지킬 규칙과 담당 표만** 둔다. 함수 계약·API·기록 같은 상세 내용은 `docs/`의 파트별 문서에 있다.
(`CLAUDE.md`는 이 파일을 불러오기만 한다. 같은 내용을 두 곳에 적지 않는다.)

## 프로젝트 개요

**마인드 케어(Mind Care)** — 치매·경도인지장애 가족 간병인을 위한 연구 요약·개인화 앱.
PubMed에서 논문을 주기적으로 수집하고, LLM + RAG로 관련성/근거를 분류·요약해 간병인에게 쉬운 말로 전달하고 개인화된 챗봇으로 답한다.
팀: **온기억** (산학프로젝트2, Fall 2026)

- **백엔드**: Python + FastAPI (`backend/`) · **프론트엔드**: React Native + Expo (`frontend/`)
- **DB**: PostgreSQL + pgvector · **AI**: 범용 LLM API(GPT/Gemini) + RAG + Embedding
- **논문 수집**: PubMed(NCBI E-utilities) API, 주 1회 배치(Celery/cron)
- 설치·실행 명령(백엔드 venv·uvicorn, 프론트 npm start, Docker DB)은 `README.md`.

핵심 원칙: **DB 접근은 데이터 접근 함수 계층 한 곳으로만 통한다.**
`backend/app/db/functions.py`가 유일한 관문이고, `backend/app/db/connection.py`(원시 커넥션)는
이 계층 내부에서만 쓴다. AI/백엔드 코드는 raw SQL이나 커넥션을 직접 만지지 않는다.

## 자세한 내용은 어디를 보나

**작업하는 파트의 문서만 읽는다.** 전부 읽을 필요 없다.

| 문서 | 내용 | 읽을 때 |
|---|---|---|
| [`docs/db.md`](docs/db.md) | 아키텍처·마이그레이션 목록, 데이터 접근 함수 계약 표, 스키마 요약, `feed_read.py` 정책, 요약 배치·가이드라인 저장 규칙 | DB 함수를 부르거나 고칠 때 |
| [`docs/api.md`](docs/api.md) | API 주소 목록(담당 표시), 응답 형식, `/recommendations` 동작, 프론트 연결 상태 | API를 만들거나 화면에서 부를 때 |
| [`docs/users.md`](docs/users.md) | 사용자·환자 함수·API (김한슬) | 사용자·환자 데이터를 다룰 때 |
| [`docs/ai.md`](docs/ai.md) | AI·검색 문서 안내 (김현서·허웅) | AI 파이프라인·검색·에이전트 작업 |
| [`docs/deploy.md`](docs/deploy.md) | 서버 배포·주기 배치 (박주현) | 서버에 올리거나 cron·배치를 다룰 때 |
| [`docs/history.md`](docs/history.md) | 날짜별 상태·변경 기록, 해결된 이슈 | 과거 결정의 이유가 궁금할 때만 |

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
- **공용 약속 파일**(`backend/app/schemas.py`, `backend/app/db/migrations/`, `AGENTS.md`와 `docs/`의
  데이터 접근 계약 표)을 바꾸기 전에 **팀에 공지**한다.
  계약(함수 이름·입출력 형식)이 바뀌면 코드와 문서(`docs/db.md` 등)를 **같이** 고친다.
- **DB는 데이터 담당이 만든 함수로만 접근한다** (직접 SQL 금지).
  `get_new_papers()`, `save_summary()`, `search_similar()` 등을 통해서만 데이터에 접근한다.

### 마이그레이션

- **이미 적용된 마이그레이션 파일은 수정하지 않고, 새 번호 파일로 추가한다**
  (`001_init.sql` 수정 ❌ → `002_xxx.sql` 추가 ⭕).
- **새 번호는 팀 단톡에서 먼저 잡는다** (두 사람이 같은 번호를 쓰지 않게).

### 보안

- **`.env`, API 키, 비밀번호, DB 덤프는 레포에 올리지 않는다.** (`.env.example`에 키 이름만 남긴다.
  덤프는 팀 단톡으로 주고받는다.)

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
| `backend/app/db/` (`functions.py`, `connection.py`) | 데이터 (DB 접근 함수 계층) | 아래 "함수 담당" 표 |
| `backend/app/db/feed_read.py`, `backend/app/feed_schemas.py` | 데이터 (피드 개인화 조회·쓰기) | 박주현 |
| `backend/app/db/user_functions.py`, `backend/app/user_schemas.py` | 사용자·환자 조회 | 김한슬 |
| `backend/app/db/migrations/` | 데이터 (스키마) — **변경 전 팀 공지** | |
| `backend/app/schemas.py` | 데이터 (공용 계약) — **변경 전 팀 공지** | |
| `backend/main.py`, `backend/app/config.py` | 백엔드 (앱 진입점·설정) — 공용, 라우터 등록만 추가 | |
| `backend/app/api/` | 백엔드 (API 라우터) — 기능별 파일. `papers.py`·`recommendations.py`·`evidence_grade.py`는 박주현, `users.py`는 김한슬 | |
| `backend/scripts/summarize_papers.py` | 논문 요약 배치 | 박주현 |
| `backend/scripts/classify_papers.py` | 근거 등급 배치 | 김현서 |
| `backend/app/ai/` 검색·RAG | AI-검색 (임베딩, `search_similar` 활용, 챗봇) | |
| `backend/app/ai/` 분류·요약 | AI-요약 (관련성/근거 분류, 구조화 요약) | |
| `frontend/` | 프론트 (React Native + Expo) | |
| 루트 문서 (`AGENTS.md`, `README.md`, `CLAUDE.md`) | 전원 — **변경 전 팀 공지** | |
| `docs/` | 파트별 상세 문서 — 각 문서 맨 위의 담당자가 고친다 | |

**DB 함수 담당** — `functions.py`는 팀 공용 관문이고 직접 SQL 금지 규칙은 그대로다. 도메인별로 함수 담당만 나눈다.

| 도메인 | 다루는 테이블 | 담당 |
|---|---|---|
| 논문·임베딩·요약·검색 | `papers`, `paper_embeddings`, `paper_analysis` | 박주현 |
| 가이드라인(기반 지식층) 저장·검색 | `guideline_knowledge` | 박주현 (조각 나누기·임베딩 생성은 김현서) |
| 사용자·환자 관련 | `users`, `patient_profiles`, `caregiver_profiles`, `clinical_assessments`, `safety_events`, `medications`, `medical_visits` | 김한슬 |

`paper_analysis`는 칸마다 채우는 사람이 다르다 — 칸별 담당과 저장 함수는 [`docs/db.md`](docs/db.md) 참고.
