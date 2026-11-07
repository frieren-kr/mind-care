-- ============================================================
-- 013. 챗봇 대화 턴 묶기 + 환자 삭제 시 대화 원문도 삭제 (2026-11-07)
-- 담당: 박주현 (012는 이미 push돼서 고치지 않고 여기서 바꾼다)
-- ============================================================
--   turn_id     질문·답변 두 행을 묶는 id. save_chat_turn()이 uuid4 하나를 두 행에 넣는다.
--               012로 저장된 옛 행은 NULL 그대로 둔다 (시각만으로는 쌍을 정확히 맞출 수 없다).
--               이전 대화 조회(get_recent_chat_turns)는 turn_id가 있는 행만 쓰고, 옛 행은 7일 뒤 지워진다.
--   patient_id  환자를 지우면 SET NULL(012) → CASCADE.
--               NULL로 바꾸면 삭제된 환자 대화가 "환자 미선택" 대화와 섞인다.
--               chat_daily_summaries(012)도 이미 CASCADE라 같은 규칙이 된다.
--
-- 적용 (backend/ 기준, 012 다음에):
-- docker exec -i mindcare-db psql -U mindcare -d mindcare -v ON_ERROR_STOP=1 --single-transaction < app/db/migrations/013_chat_turns.sql
-- 모든 문장을 IF NOT EXISTS / IF EXISTS로 써서 여러 번 실행해도 안전하다.
-- ============================================================

-- 1. 질문·답변 쌍 id. 한 턴에 질문 1행·답변 1행만 (NULL끼리는 겹쳐도 된다 — 옛 행)
ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS turn_id UUID;

CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_messages_turn_role ON chat_messages (turn_id, role);

-- 2. patient_id 외래키를 CASCADE로. 이름은 012의 ADD COLUMN ... REFERENCES가 만든 기본 이름이다
--    (실제 DB에서 확인: chat_messages_patient_id_fkey). 다시 돌려도 지우고 같은 정의로 다시 만든다.
ALTER TABLE chat_messages
    DROP CONSTRAINT IF EXISTS chat_messages_patient_id_fkey,
    ADD CONSTRAINT chat_messages_patient_id_fkey
        FOREIGN KEY (patient_id) REFERENCES patient_profiles(id) ON DELETE CASCADE;
