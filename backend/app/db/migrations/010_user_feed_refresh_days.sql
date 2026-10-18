-- ============================================================
-- 010. 사용자별 AI 피드 갱신 주기 + 진료·복약 날짜 무결성
-- ============================================================
-- users는 간병인 계정이다. 자가점검(caregiver_profiles)과 분리된 계정 설정으로 둔다.
-- 기존 사용자와 새 사용자 모두 기본값은 3일이다.
-- 이 컬럼을 조회하는 새 코드를 실행하기 전에 적용한다.
--
-- 연결할 코드:
--   user_functions.get_user_feed_settings() / update_user_feed_settings()
--   recommendations.get_feed_cache_days(user_id)
-- 만료 기준은 마지막 피드 생성 시각 + 설정 일수다.
-- 설정 변경 자체는 피드를 생성하지 않으며, 만료 후 다음 요청에서 갱신한다.
-- 검색 방식 피드는 현재 저장하지 않는다. 자동 실행 스케줄러도 별도로 필요하다.
--
-- 사용자 요청에 따라 이미 적용된 010에 날짜 제약을 함께 추가한다.
-- 기존 010 적용 DB에서도 이 파일을 다시 실행해야 트리거가 생긴다.
-- 재실행해도 기존 feed_refresh_days 값은 유지한다.
-- psql -v ON_ERROR_STOP=1 --single-transaction -f ... 형태로 파일 전체를 적용한다.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS feed_refresh_days SMALLINT NOT NULL DEFAULT 3
    CONSTRAINT users_feed_refresh_days_check CHECK (feed_refresh_days BETWEEN 1 AND 7);

COMMENT ON COLUMN users.feed_refresh_days IS
    'AI 피드 재사용 기간(일), 1~7일. 마지막 생성 시각 기준, 만료 후 다음 요청에서 갱신';

-- '오늘'은 DB 세션 시간대가 아니라 한국 시간으로 계산한다.
-- transaction 시작일이 아닌 SQL 문장 시작일을 사용해 긴 트랜잭션의 자정 경계도 처리한다.
CREATE OR REPLACE FUNCTION enforce_medical_visit_date_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.is_visited
       AND NEW.visit_date > (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date THEN
        RAISE EXCEPTION '미래 날짜의 진료는 방문 완료(is_visited=true)로 기록할 수 없습니다.'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'medical_visits_completed_date_check';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION enforce_medication_end_date_state()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NOT NEW.is_taking
       AND NEW.end_date > (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date THEN
        RAISE EXCEPTION '종료일이 미래인 약은 복용 중단(is_taking=false)으로 기록할 수 없습니다.'
            USING ERRCODE = '23514',
                  CONSTRAINT = 'medications_stopped_end_date_check';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    -- 기존 데이터 검사와 트리거 설치 사이에 모순 기록이 들어오지 않게 쓰기를 잠근다.
    LOCK TABLE medical_visits, medications IN SHARE ROW EXCLUSIVE MODE;

    -- 기존 기록은 임의로 바꾸지 않는다. 모순이 있으면 전체 적용을 실패시켜 먼저 정정하게 한다.
    IF EXISTS (
        SELECT 1 FROM medical_visits
         WHERE is_visited
           AND visit_date > (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date
    ) THEN
        RAISE EXCEPTION '미래 날짜인데 방문 완료인 기존 진료 기록을 정정한 뒤 010을 다시 적용하세요.'
            USING ERRCODE = '23514', CONSTRAINT = 'medical_visits_completed_date_check';
    END IF;
    IF EXISTS (
        SELECT 1 FROM medications
         WHERE NOT is_taking
           AND end_date > (statement_timestamp() AT TIME ZONE 'Asia/Seoul')::date
    ) THEN
        RAISE EXCEPTION '종료일이 미래인데 복용 중단인 기존 복약 기록을 정정한 뒤 010을 다시 적용하세요.'
            USING ERRCODE = '23514', CONSTRAINT = 'medications_stopped_end_date_check';
    END IF;

    -- 모든 INSERT/UPDATE의 최종 행을 검사한다. 직접 SQL과 날짜/상태만의 부분 수정도 포함한다.
    CREATE OR REPLACE TRIGGER medical_visits_date_state_guard
        AFTER INSERT OR UPDATE ON medical_visits
        FOR EACH ROW EXECUTE FUNCTION enforce_medical_visit_date_state();

    CREATE OR REPLACE TRIGGER medications_date_state_guard
        AFTER INSERT OR UPDATE ON medications
        FOR EACH ROW EXECUTE FUNCTION enforce_medication_end_date_state();
END;
$$;
