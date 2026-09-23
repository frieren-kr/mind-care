-- ============================================================
-- 마인 케어 (Mind Care) DB 스키마 초안
-- PostgreSQL + pgvector
-- 담당: 박주현(데이터) — 팀 리뷰 후 확정
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- ------------------------------------------------------------
-- 1. 사용자 (간병인)
-- ------------------------------------------------------------
CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name          TEXT NOT NULL,
    email         TEXT UNIQUE NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- 2. 환자 프로필 (간병인이 대신 입력)
-- ------------------------------------------------------------
CREATE TABLE patient_profiles (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    caregiver_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT,                       -- 별칭 가능 (예: "어머니")
    dementia_stage  TEXT,                       -- 경도 / 중등도 / 중증
    diagnosis_date  DATE,
    symptoms        TEXT[] DEFAULT '{}',         -- 예: 수면장애, 배회, 공격성
    interests       TEXT[] DEFAULT '{}',         -- 관심 치료·관리 분야
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- 3. 간병인 자가점검 (소진·생활습관)
-- ------------------------------------------------------------
CREATE TABLE caregiver_profiles (
    user_id         UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    relationship    TEXT,                       -- 자녀 / 배우자 등
    burden_score    SMALLINT,                   -- Zarit 부담척도 간이 점수
    mood_score      SMALLINT,                   -- PHQ-9 간이 우울 셀프체크
    lifestyle_tags  TEXT[] DEFAULT '{}',         -- 예: 직장병행, 수면부족
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- 4. 논문 원본 메타데이터 (수집 단계 결과)
-- ------------------------------------------------------------
CREATE TABLE papers (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source          TEXT NOT NULL,              -- pubmed / semantic_scholar / openalex
    external_id     TEXT NOT NULL,              -- PMID / DOI
    title           TEXT NOT NULL,
    abstract        TEXT,
    published_date  DATE,
    url             TEXT,
    collected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, external_id)
);
CREATE INDEX idx_papers_published_date ON papers (published_date DESC);

-- ------------------------------------------------------------
-- 5. 논문 임베딩 (관련성 판단용, pgvector)
-- ------------------------------------------------------------
CREATE TABLE paper_embeddings (
    paper_id        UUID PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
    embedding       vector(768),                -- 임베딩 모델 차원에 맞춰 조정
    model_name      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- 유사도 검색용 인덱스 (근사 최근접 이웃)
CREATE INDEX idx_paper_embeddings_vector
    ON paper_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ------------------------------------------------------------
-- 6. 논문 분석 결과 (근거분류 + 구조화 요약, AI 배치 산출물)
--    → 이중 지식 구조 중 '최신 연구층'
-- ------------------------------------------------------------
CREATE TABLE paper_analysis (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    paper_id            UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    study_type          TEXT,                   -- RCT / 체계적 문헌고찰 / 관찰연구 / 사례보고 / 전문가의견
    evidence_level       TEXT,                  -- GRADE 유사 등급 (Level I ~ VII)
    guideline_relation   TEXT,                  -- 기존 가이드라인과 '일치/보완/상충'
    summary_finding      TEXT,                  -- ① 무엇이 새로 밝혀졌나
    summary_comparison   TEXT,                  -- ② 기존 권고와 비교
    summary_limitation   TEXT,                  -- ③ 한계
    tags                 TEXT[] DEFAULT '{}',    -- 매칭용 태그 (수면장애, 배회 등)
    model_name           TEXT,
    generated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (paper_id)
);

-- ------------------------------------------------------------
-- 7. 기반 지식층 (확립된 가이드라인 — 최신 연구와 대조 기준)
-- ------------------------------------------------------------
CREATE TABLE guideline_knowledge (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    topic           TEXT NOT NULL,              -- 예: 수면장애 관리, 배회 대응
    content         TEXT NOT NULL,
    source          TEXT,                       -- 출처 (학회 가이드라인 등)
    tags            TEXT[] DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- 8. 개인화 피드 (실시간 요청 시점 결과 캐시)
-- ------------------------------------------------------------
CREATE TABLE feed_items (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    paper_id                UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    relevance_score         FLOAT,
    personalized_explanation TEXT,               -- "왜 나와 관련 있는지"
    is_saved                BOOLEAN NOT NULL DEFAULT false,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_feed_items_user ON feed_items (user_id, created_at DESC);

-- ------------------------------------------------------------
-- 9. 돌봄·자기돌봄 기록 (월간 자기 갱신형 개인화의 입력)
-- ------------------------------------------------------------
CREATE TABLE care_logs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    patient_id      UUID REFERENCES patient_profiles(id) ON DELETE SET NULL,
    log_type        TEXT NOT NULL,              -- patient_care / caregiver_selfcare
    content         TEXT,
    mood_tag        TEXT,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_care_logs_user_time ON care_logs (user_id, logged_at DESC);

-- ------------------------------------------------------------
-- 10. 자기 갱신형 개인화 프로필 (월 1회 배치 산출물)
-- ------------------------------------------------------------
CREATE TABLE profile_summaries (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    period_start    DATE NOT NULL,
    period_end      DATE NOT NULL,
    summary         TEXT,                       -- 한 달치 기록 AI 요약
    updated_tags    TEXT[] DEFAULT '{}',         -- 갱신된 개인화 태그
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------
-- 11. 챗봇 대화 이력 (RAG 응답 + 출처 인용)
-- ------------------------------------------------------------
CREATE TABLE chat_messages (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role                TEXT NOT NULL,          -- user / assistant
    content             TEXT NOT NULL,
    cited_paper_ids     UUID[] DEFAULT '{}',     -- RAG 근거 출처 (papers.id 배열)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_chat_messages_user_time ON chat_messages (user_id, created_at DESC);
