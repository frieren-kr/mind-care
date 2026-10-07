-- ============================================================
-- 004. 환자 관리 스키마 확장
-- 담당: 김한슬
-- ============================================================
-- users.phone_number 컬럼 + 환자 관리 테이블 4개(치매 평가 이력 / 안전·행동
-- 이벤트 / 복약 정보 / 병원 방문 기록)를 추가한다.
--
-- 원래 001_init.sql에 직접 들어가 있었으나, 001은 DB를 처음 만들 때(볼륨이 비어
-- 있을 때)만 자동 실행돼서 이미 DB를 띄워 둔 팀원에게는 반영되지 않는다. 그래서
-- 별도 마이그레이션으로 분리했다. (001은 183eb32 시점 구조로 되돌렸다.)
--
-- 모든 문장을 IF NOT EXISTS로 써서 여러 번 실행해도 안전하다.
-- ============================================================

-- ------------------------------------------------------------
-- users.phone_number 추가
--    휴대폰 번호, '-' 없이 숫자만 (예: 01012345678). 선택 입력, 입력 시 중복 불가.
--    중복 금지는 UNIQUE 제약 대신 UNIQUE INDEX로 둔다(IF NOT EXISTS로 재실행 안전).
--    NULL은 여러 개 허용되므로 '선택 입력'과도 맞는다.
-- ------------------------------------------------------------
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_number TEXT;  -- 휴대폰 번호, '-' 없이 숫자만 (예: 01012345678). 선택 입력, 입력 시 중복 불가
CREATE UNIQUE INDEX IF NOT EXISTS users_phone_number_key ON users (phone_number);

-- ------------------------------------------------------------
-- 2-1. 치매 평가 이력 (CDR, K-IADL, K-QDRS 등)
--      환자 1명이 여러 번 평가받을 수 있다 (patient_profiles 1 : N clinical_assessments).
--      평가 1회 = 1행. 같은 날 여러 척도로 평가했으면 척도마다 1행씩 넣는다.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clinical_assessments (
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
CREATE INDEX IF NOT EXISTS idx_clinical_assessments_patient
    ON clinical_assessments (patient_id, assessment_type, assessed_at DESC);

-- ------------------------------------------------------------
-- 2-2. 안전·행동 이벤트 (낙상, 배회, 실종 등)
--      환자별로 하루 1행. 그날 각 이벤트가 있었는지를 TRUE/FALSE로 기록한다.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS safety_events (
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
CREATE INDEX IF NOT EXISTS idx_safety_events_patient_date ON safety_events (patient_id, event_date DESC);

-- ------------------------------------------------------------
-- 2-3. 복약 정보
--      환자가 복용 중이거나 복용했던 약. 약 1종 = 1행.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS medications (
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
CREATE INDEX IF NOT EXISTS idx_medications_patient ON medications (patient_id, is_taking);

-- ------------------------------------------------------------
-- 2-4. 병원 방문 기록
--      진료 1회 = 1행. 예약만 해둔 방문은 is_visited = false로 넣고, 다녀오면 true로 바꾼다.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS medical_visits (
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
CREATE INDEX IF NOT EXISTS idx_medical_visits_patient_date ON medical_visits (patient_id, visit_date DESC);
