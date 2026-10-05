-- ============================================================
-- 마인드 케어 (Mind Care) DB 스키마 초안
-- PostgreSQL + pgvector
-- 담당: 박주현(데이터) — 팀 리뷰 후 확정
-- ============================================================

-- uuid_generate_v4() 로 모든 테이블의 id(UUID)를 만든다
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
-- vector 타입과 유사도 연산자(<=> 코사인 거리 등)를 쓰기 위한 pgvector 확장
CREATE EXTENSION IF NOT EXISTS vector;

-- ------------------------------------------------------------
-- 1. 사용자 (간병인)
--    앱에 가입한 간병인 계정. 다른 사용자 관련 테이블이 모두 여기를 참조한다.
-- ------------------------------------------------------------
CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),  -- 사용자 고유 id
    name          TEXT NOT NULL,                                -- 이름(표시용)
    email         TEXT UNIQUE NOT NULL,                         -- 로그인 이메일, 중복 불가
    phone_number  TEXT UNIQUE,                                  -- 휴대폰 번호, '-' 없이 숫자만 (예: 01012345678). 선택 입력, 입력 시 중복 불가
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),           -- 가입 시각
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()            -- 마지막 정보 수정 시각
);

-- ------------------------------------------------------------
-- 2. 환자 프로필 (간병인이 대신 입력)
--    간병인 1명이 여러 환자를 등록할 수 있다 (users 1 : N patient_profiles).
-- ------------------------------------------------------------
CREATE TABLE patient_profiles (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),            -- 환자 프로필 고유 id
    caregiver_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- 담당 간병인 (users.id), 간병인 탈퇴 시 함께 삭제
    name            TEXT,                       -- 별칭 가능 (예: "어머니")
    dementia_stage  TEXT,                       -- 경도 / 중등도 / 중증
    diagnosis_date  DATE,                       -- 진단 받은 날짜
    symptoms        TEXT[] DEFAULT '{}',         -- 예: 수면장애, 배회, 공격성
    interests       TEXT[] DEFAULT '{}',         -- 관심 치료·관리 분야
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 등록 시각
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()   -- 마지막 수정 시각
);

-- ------------------------------------------------------------
-- 2-1. 치매 평가 이력 (CDR, K-IADL, K-QDRS 등)
--      환자 1명이 여러 번 평가받을 수 있다 (patient_profiles 1 : N clinical_assessments).
--      평가 1회 = 1행. 같은 날 여러 척도로 평가했으면 척도마다 1행씩 넣는다.
-- ------------------------------------------------------------
CREATE TABLE clinical_assessments (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),                       -- 평가 기록 고유 id
    patient_id        UUID NOT NULL REFERENCES patient_profiles(id) ON DELETE CASCADE,  -- 평가 대상 환자 (patient_profiles.id)
    assessment_type   TEXT NOT NULL,              -- 평가 척도: CDR / CDR-SB / K-IADL / K-QDRS / K-MMSE 등
    score             NUMERIC(5, 2),              -- 점수 (예: CDR 0·0.5·1·2·3, K-IADL 평균 0~3, K-QDRS 0~30)
    result_detail     TEXT,                       -- 세부 결과·판정 (예: "CDR 0.5, 기억력 영역 저하")
    assessed_at       DATE NOT NULL,              -- 평가 받은 날짜
    assessed_by       TEXT,                       -- 평가 주체 (예: 병원명, 치매안심센터, 보호자 자가평가)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()  -- 기록 입력 시각
);
-- 환자별·척도별 평가 추이 조회용 인덱스
CREATE INDEX idx_clinical_assessments_patient
    ON clinical_assessments (patient_id, assessment_type, assessed_at DESC);

-- ------------------------------------------------------------
-- 2-2. 안전·행동 이벤트 (낙상, 배회, 실종 등)
--      환자별로 하루 1행. 그날 각 이벤트가 있었는지를 TRUE/FALSE로 기록한다.
-- ------------------------------------------------------------
CREATE TABLE safety_events (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),                       -- 이벤트 기록 고유 id
    patient_id        UUID NOT NULL REFERENCES patient_profiles(id) ON DELETE CASCADE,  -- 대상 환자 (patient_profiles.id)
    event_date        DATE NOT NULL,              -- 기록 대상 날짜
    has_fall          BOOLEAN NOT NULL DEFAULT false,  -- 낙상 여부
    has_wandering     BOOLEAN NOT NULL DEFAULT false,  -- 배회 여부
    has_missing       BOOLEAN NOT NULL DEFAULT false,  -- 실종 여부
    note              TEXT,                       -- 상황 메모 (예: "새벽 3시 현관 밖에서 발견")
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 기록 입력 시각
    UNIQUE (patient_id, event_date)               -- 환자당 하루 1건
);
-- 환자별 최근 이벤트 조회용 인덱스
CREATE INDEX idx_safety_events_patient_date ON safety_events (patient_id, event_date DESC);

-- ------------------------------------------------------------
-- 2-3. 복약 정보
--      환자가 복용 중이거나 복용했던 약. 약 1종 = 1행.
-- ------------------------------------------------------------
CREATE TABLE medications (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),                       -- 복약 정보 고유 id
    patient_id        UUID NOT NULL REFERENCES patient_profiles(id) ON DELETE CASCADE,  -- 복용 환자 (patient_profiles.id)
    drug_name         TEXT NOT NULL,              -- 약품명 (예: 아리셉트정 5mg, 도네페질)
    dosage            TEXT,                       -- 1회 복용량 (예: 1정, 5mg)
    frequency         TEXT,                       -- 복약 주기 (예: 1일 1회 취침 전, 1일 2회 식후)
    is_taking         BOOLEAN NOT NULL DEFAULT true,   -- 현재 복용 여부 (중단하면 false)
    start_date        DATE,                       -- 복용 시작일
    end_date          DATE,                       -- 복용 종료일 (복용 중이면 NULL)
    note              TEXT,                       -- 메모 (부작용, 처방 병원 등)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 등록 시각
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()   -- 마지막 수정 시각
);
-- 환자별 복용 중인 약 조회용 인덱스
CREATE INDEX idx_medications_patient ON medications (patient_id, is_taking);

-- ------------------------------------------------------------
-- 2-4. 병원 방문 기록
--      진료 1회 = 1행. 예약만 해둔 방문은 is_visited = false로 넣고, 다녀오면 true로 바꾼다.
-- ------------------------------------------------------------
CREATE TABLE medical_visits (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),                       -- 방문 기록 고유 id
    patient_id        UUID NOT NULL REFERENCES patient_profiles(id) ON DELETE CASCADE,  -- 진료 받은 환자 (patient_profiles.id)
    visit_date        DATE NOT NULL,              -- 방문(예정)일
    is_visited        BOOLEAN NOT NULL DEFAULT false,  -- 실제 방문 여부
    hospital_name     TEXT,                       -- 병원명
    department        TEXT,                       -- 진료과 (예: 신경과, 정신건강의학과)
    visit_content     TEXT,                       -- 진료 내용 (의사 소견, 검사, 처방 변경 등)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 기록 입력 시각
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()   -- 마지막 수정 시각
);
-- 환자별 진료 이력 조회용 인덱스
CREATE INDEX idx_medical_visits_patient_date ON medical_visits (patient_id, visit_date DESC);

-- ------------------------------------------------------------
-- 3. 간병인 자가점검 (소진·생활습관)
--    사용자당 1행 (user_id가 PK). 개인화 피드·챗봇에서 간병인 상태를 반영할 때 쓴다.
-- ------------------------------------------------------------
CREATE TABLE caregiver_profiles (
    user_id         UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,  -- 해당 간병인 (users.id)
    relationship    TEXT,                       -- 자녀 / 배우자 등
    burden_score    SMALLINT,                   -- Zarit 부담척도 간이 점수
    mood_score      SMALLINT,                   -- PHQ-9 간이 우울 셀프체크
    lifestyle_tags  TEXT[] DEFAULT '{}',         -- 예: 직장병행, 수면부족
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()  -- 마지막 자가점검 시각
);

-- ------------------------------------------------------------
-- 4. 논문 원본 메타데이터 (수집 단계 결과)
--    PubMed 등에서 주 1회 배치로 수집한 논문. (source, external_id)로 중복을 막는다.
-- ------------------------------------------------------------
CREATE TABLE papers (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),  -- 논문 고유 id (내부용)
    source          TEXT NOT NULL,              -- pubmed / semantic_scholar / openalex
    external_id     TEXT NOT NULL,              -- PMID / DOI
    title           TEXT NOT NULL,              -- 논문 제목
    abstract        TEXT,                       -- 초록 (없을 수 있음)
    published_date  DATE,                       -- 출판일
    url             TEXT,                       -- 원문 링크
    collected_at    TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 수집 시각
    UNIQUE (source, external_id)                -- 같은 출처의 같은 논문은 한 번만 저장
);
-- 최신 논문 순 조회용 인덱스
CREATE INDEX idx_papers_published_date ON papers (published_date DESC);

-- ------------------------------------------------------------
-- 5. 논문 임베딩 (관련성 판단용, pgvector)
--    논문당 1건 (paper_id가 PK). 챗봇 RAG의 유사 논문 검색에 쓴다.
-- ------------------------------------------------------------
CREATE TABLE paper_embeddings (
    paper_id        UUID PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,  -- 대상 논문 (papers.id)
    embedding       vector(768),                -- 임베딩 모델 차원에 맞춰 조정
    model_name      TEXT NOT NULL,              -- 임베딩 생성에 쓴 모델 이름
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()  -- 임베딩 생성 시각
);
-- 유사도 검색용 인덱스 (근사 최근접 이웃)
CREATE INDEX idx_paper_embeddings_vector
    ON paper_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ------------------------------------------------------------
-- 6. 논문 분석 결과 (근거분류 + 구조화 요약, AI 배치 산출물)
--    → 이중 지식 구조 중 '최신 연구층'
--    논문당 1건 (paper_id UNIQUE). 이 행이 없는 논문이 '아직 분석 안 된 논문'이다.
-- ------------------------------------------------------------
CREATE TABLE paper_analysis (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),            -- 분석 결과 고유 id
    paper_id            UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,  -- 분석 대상 논문 (papers.id)
    study_type          TEXT,                   -- RCT / 체계적 문헌고찰 / 관찰연구 / 사례보고 / 전문가의견
    evidence_level       TEXT,                  -- GRADE 유사 등급 (Level I ~ VII)
    guideline_relation   TEXT,                  -- 기존 가이드라인과 '일치/보완/상충'
    summary_finding      TEXT,                  -- ① 무엇이 새로 밝혀졌나
    summary_comparison   TEXT,                  -- ② 기존 권고와 비교
    summary_limitation   TEXT,                  -- ③ 한계
    tags                 TEXT[] DEFAULT '{}',    -- 매칭용 태그 (수면장애, 배회 등)
    model_name           TEXT,                  -- 분석·요약에 쓴 LLM 이름
    generated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 분석 생성(재분석) 시각
    UNIQUE (paper_id)                           -- 논문당 분석 결과 1건
);

-- ------------------------------------------------------------
-- 7. 기반 지식층 (확립된 가이드라인 — 최신 연구와 대조 기준)
--    → 이중 지식 구조 중 '기반 지식층'. paper_analysis.guideline_relation 판단의 기준.
-- ------------------------------------------------------------
CREATE TABLE guideline_knowledge (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),  -- 가이드라인 항목 고유 id
    topic           TEXT NOT NULL,              -- 예: 수면장애 관리, 배회 대응
    content         TEXT NOT NULL,              -- 가이드라인 본문(권고 내용)
    source          TEXT,                       -- 출처 (학회 가이드라인 등)
    tags            TEXT[] DEFAULT '{}',         -- 매칭용 태그 (paper_analysis.tags와 같은 체계)
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()  -- 마지막 갱신 시각
);

-- ------------------------------------------------------------
-- 8. 개인화 피드 (실시간 요청 시점 결과 캐시)
--    사용자별로 추천된 논문 목록. relevance_score가 사용자별 관련도다.
-- ------------------------------------------------------------
CREATE TABLE feed_items (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),            -- 피드 항목 고유 id
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,   -- 피드를 받는 사용자 (users.id)
    paper_id                UUID NOT NULL REFERENCES papers(id) ON DELETE CASCADE,  -- 추천된 논문 (papers.id)
    relevance_score         FLOAT,                   -- 이 사용자에 대한 관련도 점수 (높을수록 관련)
    personalized_explanation TEXT,               -- "왜 나와 관련 있는지"
    is_saved                BOOLEAN NOT NULL DEFAULT false,  -- 사용자가 저장(북마크)했는지
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()  -- 피드 생성 시각
);
-- 사용자별 최신 피드 조회용 인덱스
CREATE INDEX idx_feed_items_user ON feed_items (user_id, created_at DESC);

-- ------------------------------------------------------------
-- 9. 돌봄·자기돌봄 기록 (월간 자기 갱신형 개인화의 입력)
--    간병인이 남기는 일지. 월 1회 배치가 이 기록을 모아 profile_summaries를 만든다.
-- ------------------------------------------------------------
CREATE TABLE care_logs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),                     -- 기록 고유 id
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,            -- 기록한 간병인 (users.id)
    patient_id      UUID REFERENCES patient_profiles(id) ON DELETE SET NULL,         -- 관련 환자 (선택), 환자 삭제 시 NULL로
    log_type        TEXT NOT NULL,              -- patient_care / caregiver_selfcare
    content         TEXT,                       -- 기록 내용 (자유 서술)
    mood_tag        TEXT,                       -- 기록 당시 기분 태그
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT now()  -- 기록 시각
);
-- 사용자별 기간 조회(월간 배치)용 인덱스
CREATE INDEX idx_care_logs_user_time ON care_logs (user_id, logged_at DESC);

-- ------------------------------------------------------------
-- 10. 자기 갱신형 개인화 프로필 (월 1회 배치 산출물)
--     care_logs를 기간별로 요약한 결과. 개인화 피드의 입력으로 쓴다.
-- ------------------------------------------------------------
CREATE TABLE profile_summaries (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),            -- 요약 고유 id
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- 대상 사용자 (users.id)
    period_start    DATE NOT NULL,              -- 요약 기간 시작일
    period_end      DATE NOT NULL,              -- 요약 기간 종료일
    summary         TEXT,                       -- 한 달치 기록 AI 요약
    updated_tags    TEXT[] DEFAULT '{}',         -- 갱신된 개인화 태그
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT now()  -- 요약 생성 시각
);

-- ------------------------------------------------------------
-- 11. 챗봇 대화 이력 (RAG 응답 + 출처 인용)
--     사용자 질문과 챗봇 답변을 한 행씩 저장한다.
-- ------------------------------------------------------------
CREATE TABLE chat_messages (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),            -- 메시지 고유 id
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- 대화 주인 (users.id)
    role                TEXT NOT NULL,          -- user / assistant
    content             TEXT NOT NULL,          -- 메시지 본문
    cited_paper_ids     UUID[] DEFAULT '{}',     -- RAG 근거 출처 (papers.id 배열)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()  -- 메시지 작성 시각
);
-- 사용자별 대화 이력 시간순 조회용 인덱스
CREATE INDEX idx_chat_messages_user_time ON chat_messages (user_id, created_at DESC);
