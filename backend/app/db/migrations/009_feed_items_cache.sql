-- ============================================================
-- 009. feed_items를 "AI 피드 저장본"으로 쓰기 위한 칸 (2026-10-15)
-- 담당: 박주현 (10/14 회의: AI 피드 결과를 저장하고 기간 안에는 저장본을 그대로 돌려준다)
-- ============================================================
-- 한 번에 만든 피드(보통 5편)를 batch_id로 묶고, 묶음 안 순서(rank)와 category를 둔다.
-- personal_reason은 기존 personalized_explanation 칸을 쓴다.
--   저장   save_feed_batch()        — 한 묶음을 트랜잭션 하나로 (app/db/feed_read.py)
--   조회   get_latest_feed_batch()  — 기간 안의 가장 최근 묶음 + 생성 시각(created_at)
--
-- 001은 이미 적용된 파일이라 수정하지 않고 이 파일로 추가한다.
-- 모든 문장을 IF NOT EXISTS로 써서 여러 번 실행해도 안전하다.
-- 기존 행(지금까지 이 테이블에 쓰는 코드가 없어 팀 DB는 비어 있다)은 batch_id가 NULL로 남고 조회에서 빠진다.
-- ============================================================

ALTER TABLE feed_items
    ADD COLUMN IF NOT EXISTS batch_id UUID,   -- 한 번에 만든 피드 묶음 (같은 묶음은 created_at도 같다)
    ADD COLUMN IF NOT EXISTS rank     INT,    -- 묶음 안 순서 (1부터, AI가 정한 순)
    ADD COLUMN IF NOT EXISTS category TEXT;   -- AI가 정한 분류 (treatment / care / prevention / diagnosis)

-- 묶음 안 항목을 순서대로 읽는다 + 같은 묶음에 같은 순번이 두 번 들어가지 않게.
CREATE UNIQUE INDEX IF NOT EXISTS uq_feed_items_batch_rank ON feed_items (batch_id, rank);

-- 새 묶음을 저장할 때 "이 사용자가 이미 북마크한 논문인지"를 찾는다 (is_saved 이어 붙이기).
CREATE INDEX IF NOT EXISTS idx_feed_items_user_saved ON feed_items (user_id, paper_id) WHERE is_saved;

-- 가장 최근 묶음 찾기는 001의 idx_feed_items_user (user_id, created_at DESC)를 그대로 쓴다.
