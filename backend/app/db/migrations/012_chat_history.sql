-- ============================================================
-- 012. 챗봇 대화 저장 + 하루 단위 요약 (2026-11-06)
-- 담당: 박주현 (팀 결정: 대화 원문은 7일만 보관하고, 그 뒤에는 하루 단위 요약만 남긴다)
-- ============================================================
-- chat_messages는 001에 이미 있다 (user_id·role·content·cited_paper_ids·created_at, 인덱스
-- idx_chat_messages_user_time (user_id, created_at DESC)). 001은 수정하지 않고 여기서 칸을 더한다.
--   patient_id  어느 환자 이야기인지 (없어도 됨). 환자를 지우면 NULL
--   sources     답변의 출처 (ChatSource 목록 JSON). 질문 행은 빈 배열
--   role        'user' / 'assistant'만 허용
-- cited_paper_ids(001)는 쓰지 않는다 (지침 출처는 papers.id가 없어서 sources로 대신한다).
--
--   저장   save_chat_turn()           — 질문·답변 두 행을 트랜잭션 하나로 (app/db/chat_store.py)
--   요약   save_daily_summary()       — 사용자·환자·날짜당 한 행, 다시 돌리면 덮어쓴다
--   삭제   delete_old_chat_messages() — 7일 지난 원문 (scripts/summarize_chats.py가 매일)
--
-- 적용 (backend/ 기준):
-- docker exec -i mindcare-db psql -U mindcare -d mindcare -v ON_ERROR_STOP=1 --single-transaction < app/db/migrations/012_chat_history.sql
-- 모든 문장을 IF NOT EXISTS(또는 존재 확인)로 써서 여러 번 실행해도 안전하다.
-- NULLS NOT DISTINCT는 PostgreSQL 15 이상 (로컬·서버 모두 pg17).
-- ============================================================

-- 1. chat_messages에 칸 추가
ALTER TABLE chat_messages
    ADD COLUMN IF NOT EXISTS patient_id UUID REFERENCES patient_profiles(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS sources    JSONB NOT NULL DEFAULT '[]'::jsonb;

-- 제약은 IF NOT EXISTS가 없어서 이름으로 확인한 뒤 추가한다.
-- 기존 행 중 role이 다른 값이면 여기서 실패한다 (지금까지 이 테이블에 쓰는 코드가 없어 비어 있다).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chat_messages_role_check' AND conrelid = 'chat_messages'::regclass) THEN
        ALTER TABLE chat_messages
            ADD CONSTRAINT chat_messages_role_check CHECK (role IN ('user', 'assistant'));
    END IF;
END;
$$;

-- (user_id, created_at) 인덱스는 001의 idx_chat_messages_user_time을 그대로 쓴다.
-- 7일 지난 원문 삭제는 created_at만 보고 지운다 (테이블에 최대 7~8일치만 남아서 따로 인덱스를 두지 않는다).

-- 2. 하루 단위 대화 요약 (원문이 지워진 뒤 남는 기록)
--    summary_date는 한국 날짜 (00:00~24:00 KST에 오간 대화).
--    환자를 지우면 그 환자의 요약도 지운다 (NULL로 바꾸면 같은 날 "환자 없음" 요약과 겹칠 수 있다).
CREATE TABLE IF NOT EXISTS chat_daily_summaries (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    patient_id    UUID REFERENCES patient_profiles(id) ON DELETE CASCADE,  -- 없으면 환자를 고르지 않은 대화
    summary_date  DATE NOT NULL,
    summary       TEXT NOT NULL,
    model         TEXT NOT NULL,                        -- 요약에 쓴 LLM 모델 이름
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),   -- 마지막으로 저장(덮어쓰기)한 시각
    -- patient_id가 NULL인 행끼리도 같은 날짜는 하나만 (save_daily_summary의 ON CONFLICT 대상)
    CONSTRAINT uq_chat_daily_summaries_user_patient_date
        UNIQUE NULLS NOT DISTINCT (user_id, patient_id, summary_date)
);

COMMENT ON TABLE chat_daily_summaries IS
    '챗봇 대화의 하루 단위 요약. 원문(chat_messages)은 7일 뒤 지우고 이것만 남긴다';
