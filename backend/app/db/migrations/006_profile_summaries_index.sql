-- ============================================================
-- 006. profile_summaries 인덱스 보강 (월간 요약 조회 + upsert)
-- 담당: 박주현
-- ============================================================
-- 001에서 profile_summaries에는 PK(id) 인덱스만 있었다. 그래서
--   1) 피드 조회(get_feed_context_snapshot의 월간 요약 1건 고르기)가
--      user_id로 찾을 인덱스가 없어 테이블 전체를 읽는다.
--   2) save_profile_summary()가 "같은 기간이면 갱신"(upsert)을 하려는데
--      ON CONFLICT로 지목할 UNIQUE 제약이 없어서, 같은 달 요약을 두 번 돌리면
--      행이 2개 쌓인다.
-- 이 두 가지를 인덱스 2개로 해결한다.
--
-- 001은 이미 적용된 파일이라 수정하지 않고 이 파일로 추가한다.
-- 모든 문장을 IF NOT EXISTS로 써서 여러 번 실행해도 안전하다.
-- ============================================================

-- ------------------------------------------------------------
-- 1) 월간 요약 조회용 인덱스
--    get_feed_context_snapshot()은 user_id로 찾아 period_end 내림차순으로 1건만 고른다
--    (WHERE user_id = ? AND period_end < ? ORDER BY period_end DESC, generated_at DESC, id).
--    period_end를 DESC로 담아 두면 정렬까지 인덱스로 처리된다.
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_profile_summaries_user_period
    ON profile_summaries (user_id, period_end DESC);

-- ------------------------------------------------------------
-- 2) 월간 요약 upsert용 UNIQUE 인덱스
--    save_profile_summary()의 ON CONFLICT (user_id, period_start, period_end) 대상이다.
--    "사용자 1명 + 같은 기간 = 요약 1건"이라는 규칙을 DB가 보장한다.
--    UNIQUE 제약(ALTER TABLE ADD CONSTRAINT) 대신 UNIQUE INDEX로 두는 이유는
--    IF NOT EXISTS를 쓸 수 있어 재실행이 안전하기 때문이다 (004의 users_phone_number_key와 같은 방식).
--
--    ※ 이미 같은 기간 요약이 중복으로 쌓여 있으면 이 인덱스 생성이 실패한다.
--      그 경우 중복을 먼저 정리해야 한다 (개발용 DB라면 아래로 확인할 수 있다):
--        SELECT user_id, period_start, period_end, count(*)
--          FROM profile_summaries GROUP BY 1,2,3 HAVING count(*) > 1;
-- ------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS profile_summaries_user_period_key
    ON profile_summaries (user_id, period_start, period_end);
