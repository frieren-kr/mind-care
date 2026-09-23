# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

**마인 케어(Mind Care)** — 치매·경도인지장애 가족 간병인을 위한 연구 요약·개인화 앱.
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
- `backend/app/db/functions.py` — **데이터 접근 함수 (팀 공용 계약, 박주현 담당).**
- `backend/app/db/migrations/001_init.sql` — **DB 스키마 정본(테이블 정의). 팀 리뷰 후 확정.**
- `backend/app/schemas.py` — 함수 입출력 스키마(pydantic). 이 계약의 단일 출처.
- `backend/app/collectors/pubmed.py` — PubMed 수집(주 1회 배치).

## 역할 분담

- **박주현(나) — 데이터 담당**: PubMed 수집, DB 스키마, 저장/조회 함수 설계.
- 다른 팀원: 백엔드 API · AI 파이프라인(관련성/근거분류/요약, 개인화/챗봇) · 프론트엔드 · 기획/평가.

## 코딩 규칙 (팀 합의)

1. **DB 직접 접근 금지.** AI/백엔드는 raw SQL을 쓰지 않고 박주현이 만든 데이터 접근
   함수(`get_new_papers()`, `save_summary()`, `search_similar()` 등)를 통해서만 데이터에 접근한다.
2. **계약 변경은 반드시 기록.** 함수 이름이나 입출력(JSON/스키마) 형식이 바뀌면
   아래 "데이터 접근 계약" 섹션과 `backend/app/schemas.py`를 함께 갱신한다.
3. **커밋 메시지는 한글**로, 무엇을 바꿨는지 간단히 적는다.

## 데이터 접근 계약

팀원이 이 함수들을 통해서만 DB에 접근한다. 테이블은 `backend/app/db/migrations/001_init.sql`,
함수 입출력은 `backend/app/schemas.py`가 정본.
**아래 표를 바꿀 때는 항상 코드와 이 문서를 동시에 수정한다.**

| 함수 | 입력 | 반환 |
|------|------|------|
| `get_new_papers(since=None, limit=100, source=None)` | `since`: ISO8601 날짜/시각(선택), `limit`: int, `source`: `'pubmed'` 등(선택) | `list[PaperOut]` |
| `save_summary(analysis: AnalysisIn)` | `AnalysisIn` | 저장된 `paper_analysis` id (`UUID`) |
| `search_similar(embedding, top_k=5, model_name=None)` | `embedding`: `list[float]`(길이=`001_init.sql`의 `vector(N)`), `top_k`: int, `model_name`: 임베딩 모델 필터(선택) | `list[SearchResult]` |

스키마 (요약) — **모든 id는 `UUID`**:
- **PaperOut** (`papers`): `id, source, external_id, title, abstract?, published_date?, url?, collected_at`
- **AnalysisIn** (`paper_analysis` + 선택적 `paper_embeddings`): `paper_id, study_type?, evidence_level?, guideline_relation?, summary_finding?, summary_comparison?, summary_limitation?, tags, model_name?, embedding?, embedding_model?`
- **SearchResult**: `paper_id, title, url?, summary_finding?, evidence_level?, score`

동작 메모:
- `get_new_papers()` = `papers` 중 `paper_analysis`가 없는 논문 (`paper_analysis`는 `paper_id` UNIQUE).
- `save_summary()`는 `paper_analysis`에 upsert 하고, `embedding`을 함께 주면 `paper_embeddings`에도 upsert 한다.
- `search_similar()`는 `paper_embeddings`(코사인)를 검색해 `papers`/`paper_analysis`를 조인해 돌려준다.
  `paper_id`는 `chat_messages.cited_paper_ids`에 그대로 넣을 수 있다.
> 현재 함수들은 스텁(`NotImplementedError`)이다 — 시그니처/스키마 합의가 먼저, 구현은 그 다음.

### 결정사항: 논문 관련성 판단 (2026-09-23)

논문 관련성 판단은 두 단계로 한다:

1. **수집 단계**에서 치매/MCI 키워드로 필터링 — 전역 관련 여부는 이 단계에서 이미 보장된다.
2. **`feed_items.relevance_score`**로 사용자별 관련도만 계산한다.

논문 테이블에 별도의 전역 관련성 플래그(`is_relevant` 등)는 두지 않는다.

## 상태

초기 스캐폴딩 단계. DB 스키마 초안(`001_init.sql`)은 올라왔고 **팀 리뷰 대기 중**.
아직 함수 구현, AI 파이프라인, 화면이 없다.

**미해결 — AI 담당 확인 후 `vector(768)` 차원 확정 예정.**
`001_init.sql`의 `paper_embeddings.embedding`은 `vector(768)`인데 `config.py`의
`embedding_dim`은 1536(`text-embedding-3-small` 기준)이라 현재 서로 맞지 않는다.
AI 담당이 임베딩 모델을 확정하면 그 차원에 맞춰 `001_init.sql`과 `config.py`를
함께 고친다. **그 전까지는 어느 쪽도 임의로 바꾸지 않는다.**
