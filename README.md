# 마인 케어 (Mind Care)

치매·경도인지장애 가족 간병인을 위한 연구 요약·개인화 앱. PubMed 논문을 주기적으로 수집해 LLM + RAG로 관련성·근거를 분류·요약하고, 간병인에게 쉬운 말로 전달하며 개인화 챗봇으로 답합니다. (팀 온기억, 산학프로젝트2 Fall 2026)

## 기술 스택

- **백엔드**: Python + FastAPI
- **프론트엔드**: React Native + Expo
- **DB**: PostgreSQL + pgvector
  - 논문 관련성은 수집 단계 키워드 필터(전역 관련 여부 보장) + `feed_items.relevance_score`(사용자별 관련도) 2단계로 처리하며, 논문 테이블에 전역 관련성 플래그는 두지 않습니다.
- **AI**: 범용 LLM API(GPT/Gemini) + RAG + Embedding
- **논문 수집**: PubMed(NCBI E-utilities) API — 주 1회 배치

## 폴더 구조

```
mine-care/
├─ backend/     FastAPI 백엔드
│  ├─ main.py           앱 진입점
│  ├─ app/
│  │  ├─ config.py      환경 변수
│  │  ├─ schemas.py     데이터 접근 함수 입출력 스키마
│  │  ├─ db/            DB 접근 함수 계층 (팀 공용 관문) + migrations/
│  │  └─ collectors/    PubMed 수집
│  └─ requirements.txt
├─ frontend/    React Native(Expo) 앱
└─ CLAUDE.md    아키텍처·역할·코딩 규칙 문서
```

자세한 개발 환경 설정과 실행 방법은 [`backend/README.md`](backend/README.md), 아키텍처·팀 규칙은 [`CLAUDE.md`](CLAUDE.md)를 참고하세요.
