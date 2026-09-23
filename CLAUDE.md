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
PubMed(주1회 배치) → papers 테이블 → AI 파이프라인(관련성/근거분류/요약)
                                        → summaries 테이블(+임베딩)
사용자 질문 → 임베딩 → search_similar() → RAG → LLM → 챗봇 응답
```

핵심 원칙: **DB 접근은 데이터 접근 함수 계층 한 곳으로만 통한다.**
`backend/app/db/functions.py`가 유일한 관문이고, `backend/app/db/connection.py`(원시 커넥션)는
이 계층 내부에서만 쓴다. AI/백엔드 코드는 raw SQL이나 커넥션을 직접 만지지 않는다.

### 주요 경로
- `backend/main.py` — FastAPI 앱 진입점. 팀원 라우터는 여기에 등록.
- `backend/app/config.py` — 환경 변수(pydantic-settings).
- `backend/app/db/functions.py` — **데이터 접근 함수 (팀 공용 계약, 박주현 담당).**
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

팀원이 이 함수들을 통해서만 DB에 접근한다. 시그니처/스키마는 `backend/app/schemas.py`가 정본.
**아래 표를 바꿀 때는 항상 코드와 이 문서를 동시에 수정한다.**

| 함수 | 입력 | 반환 |
|------|------|------|
| `get_new_papers(since=None, limit=100)` | `since`: ISO8601 날짜(선택), `limit`: int | `list[PaperOut]` |
| `save_summary(summary: SummaryIn)` | `SummaryIn` | 저장된 summary id (`int`) |
| `search_similar(embedding, top_k=5)` | `embedding`: `list[float]`(길이=`EMBEDDING_DIM`), `top_k`: int | `list[SearchResult]` |

스키마 (요약):
- **PaperOut**: `id, pmid, title, abstract?, journal?, published_at?, collected_at`
- **SummaryIn**: `paper_id, is_relevant, evidence_grade, summary_text, embedding`
- **SearchResult**: `summary_id, paper_id, summary_text, score`

> 현재 함수들은 스텁(`NotImplementedError`)이다 — 시그니처/스키마 합의가 먼저, 구현은 그 다음.

## 상태

초기 스캐폴딩 단계. 아직 DB 마이그레이션과 함수 구현, AI 파이프라인, 화면이 없다.
