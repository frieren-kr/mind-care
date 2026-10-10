-- ============================================================
-- 005. 병원 방문 기록(medical_visits) 진료 내용 세분화
--      + 사용자·환자 외래키 인덱스 보강 (patient_profiles.caregiver_id, care_logs.patient_id)
-- 담당: 김한슬
-- ============================================================
-- 004에서는 진료 내용을 visit_content 한 컬럼에 자유 텍스트로 받았다.
-- 방문 목적·진단·처치·검사·처방 변경·의사 전달사항·다음 진료 계획을 항목별로
-- 나눠 저장해서, 화면에서 항목별로 보여주고 개인화·챗봇에서도 필요한 항목만
-- 골라 쓸 수 있게 한다.
--
-- visit_content는 지우지 않고 "기타 자유 메모" 용도로 남긴다
-- (이미 입력된 값이 있을 수 있고, 위 항목에 맞지 않는 내용을 적을 곳이 필요하다).
--
-- 004는 이미 적용된 파일이라 수정하지 않고 이 파일로 컬럼을 추가한다.
-- 모든 문장을 IF NOT EXISTS로 써서 여러 번 실행해도 안전하다.
-- ============================================================

ALTER TABLE medical_visits
    ADD COLUMN IF NOT EXISTS visit_reason      TEXT,  -- 방문 목적 (예: 정기 진료, 약 조절, 이상 행동 상담)
    ADD COLUMN IF NOT EXISTS diagnosis         TEXT,  -- 진단 / 의사 소견 (예: "알츠하이머형 치매, 경도 → 중등도 진행")
    ADD COLUMN IF NOT EXISTS treatment_content TEXT,  -- 진료 / 처치 내용
    ADD COLUMN IF NOT EXISTS test_summary      TEXT,  -- 검사 내용 요약 (예: "MRI 해마 위축 소견, K-MMSE 21점")
    ADD COLUMN IF NOT EXISTS medication_change TEXT,  -- 약 처방 / 변경 사항 (예: "도네페질 5mg → 10mg 증량")
    ADD COLUMN IF NOT EXISTS doctor_note       TEXT,  -- 의사 전달사항 (보호자에게 당부한 내용)
    ADD COLUMN IF NOT EXISTS follow_up_plan    TEXT;  -- 다음 진료 계획 (예: "3개월 후 재방문, 혈액검사 예정")

-- visit_content의 의미가 "진료 내용"에서 "기타 자유 메모"로 바뀌었으므로 DB에도 남겨 둔다.
COMMENT ON COLUMN medical_visits.visit_content IS '기타 자유 메모 (005에서 진료 내용을 항목별 컬럼으로 분리)';

-- ------------------------------------------------------------
-- 간병인별 환자 조회 인덱스
--    "이 사용자가 담당하는 환자" 조회(get_user의 patient_ids, 간병인별 환자 목록,
--    is_caregiver_of 권한 확인)는 모두 patient_profiles.caregiver_id로 찾는다.
--    001에는 이 컬럼 인덱스가 없어서 조회할 때마다 patient_profiles 전체를 읽는다.
--    (외래키를 걸어도 PostgreSQL은 참조하는 쪽 컬럼에 인덱스를 자동으로 만들지 않는다.)
--    created_at을 뒤에 붙여 "등록 순서대로 환자 목록" 정렬까지 인덱스로 처리한다.
--    이 인덱스는 간병인 탈퇴(users 삭제) 시 ON DELETE CASCADE로 환자를 찾을 때도 쓰인다.
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_patient_profiles_caregiver
    ON patient_profiles (caregiver_id, created_at);

-- ------------------------------------------------------------
-- 환자별 돌봄 기록 조회 인덱스 (care_logs.patient_id)
--    patient_profiles를 참조하는 외래키 중 인덱스가 없는 것은 이 컬럼 하나다.
--    001에는 user_id 인덱스(idx_care_logs_user_time)만 있어서
--      1) "이 환자의 돌봄 기록" 조회가 care_logs 전체를 읽고,
--      2) 환자를 삭제할 때 ON DELETE SET NULL이 바꿀 행을 찾으려고 care_logs 전체를 읽는다.
--    patient_id는 선택 입력(NULL 허용)이라, 환자가 지정된 기록만 담는 부분 인덱스로 만든다
--    (NULL 행은 위 두 경우 어디에도 쓰이지 않으므로 인덱스 크기만 키운다).
--    정렬 컬럼은 기존 idx_care_logs_user_time과 같은 logged_at DESC로 맞춘다.
--
--    참고 — 환자를 참조하는 다른 테이블은 004에서 이미 patient_id로 시작하는 인덱스가 있다:
--      clinical_assessments → idx_clinical_assessments_patient (patient_id, assessment_type, assessed_at DESC)
--      safety_events        → UNIQUE (patient_id, event_date) + idx_safety_events_patient_date
--      medications          → idx_medications_patient (patient_id, is_taking)
--      medical_visits       → idx_medical_visits_patient_date (patient_id, visit_date DESC)
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_care_logs_patient_time
    ON care_logs (patient_id, logged_at DESC)
    WHERE patient_id IS NOT NULL;
