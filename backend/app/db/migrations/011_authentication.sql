-- ============================================================
-- 011. 이메일·비밀번호 로그인 정보 및 세션
-- ============================================================
-- 선행: 001~010. uuid_generate_v4()는 001의 uuid-ossp 확장을 사용한다.
-- users.id와 기존 환자·피드 연결을 유지하고 인증 정보만 별도 테이블로 둔다.
-- 기존 사용자에게 비밀번호나 인증 행을 자동으로 만들지 않는다.
-- user_credentials가 없는 사용자 또는 is_active=false인 사용자는 로그인 불가다.
--
-- 적용 (backend/ 기준, 파일 전체를 한 트랜잭션으로 실행):
-- docker exec -i mindcare-db psql -U mindcare -d mindcare -v ON_ERROR_STOP=1 --single-transaction < app/db/migrations/011_authentication.sql
-- 같은 정의로 재실행할 수 있으며 기존 비밀번호·세션·토큰은 유지한다.
--
-- 인증 구현 시 DB 접근 함수 계층에서 지킬 계약:
--   * 가입은 users와 user_credentials INSERT를 한 트랜잭션으로 처리한다.
--   * 이메일 입력은 기존 normalize_email()로 정규화한다.
--   * password_hash에는 Argon2id 등 비밀번호 전용 해시 결과만 저장한다.
--   * 갱신 토큰 원문은 저장하지 않고 SHA-256 소문자 hex digest만 저장한다.
--   * 갱신은 세션 행을 잠근 뒤 기존 토큰 used_at 갱신과 새 토큰 INSERT를
--     한 트랜잭션으로 수행한다. 사용한 토큰은 세션 만료까지 보관한다.
--   * 재사용 감지·로그아웃은 세션을 폐기한다. 보호 요청마다 계정 활성 여부와
--     세션 소유자·만료·폐기 여부를 확인한다 (JWT 검증만으로 허용하지 않는다).
--   * 토큰 expires_at은 세션 expires_at 이하로 지정하며 갱신해도 세션의
--     절대 만료 시각을 연장하지 않는다. 이 관계는 DB 접근 함수에서 검사한다.
--   * 비밀번호 변경 시 password_changed_at·updated_at을 함께 갱신하고
--     기존 세션을 폐기한다. 해시를 공개 사용자 응답이나 로그에 포함하지 않는다.

-- 기존 이메일을 임의로 바꾸거나 계정을 합치지 않는다.
-- 대소문자·앞뒤 공백을 제외하면 같은 이메일이 있을 경우 전체 적용을 중단한다.
-- 검사와 인덱스 생성 사이에 다른 쓰기가 끼어들지 않게 잠근다.
LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM users
         GROUP BY lower(btrim(email))
        HAVING count(*) > 1
    ) THEN
        RAISE EXCEPTION '정규화 시 중복되는 기존 이메일을 정리한 뒤 011을 다시 적용하세요.'
            USING ERRCODE = '23505', CONSTRAINT = 'users_email_normalized_key';
    END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS users_email_normalized_key
    ON users (lower(btrim(email)));

-- 1. 사용자별 인증 정보 (users 1 : 0..1 user_credentials)
CREATE TABLE IF NOT EXISTS user_credentials (
    user_id             UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    password_hash       TEXT NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT true,
    password_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT user_credentials_password_hash_check
        CHECK (password_hash !~ '^[[:space:]]*$')
);

COMMENT ON TABLE user_credentials IS
    '사용자별 로그인 자격 증명. 인증 행이 없는 기존 계정은 비밀번호 설정 전 로그인 불가';
COMMENT ON COLUMN user_credentials.password_hash IS
    '비밀번호 전용 알고리즘으로 생성한 salt·파라미터 포함 해시. 평문 비밀번호 저장 금지';

-- 2. 로그인 1회 = 세션 1개. 여러 기기의 동시 로그인은 서로 다른 세션이다.
-- expires_at은 애플리케이션 정책으로 명시하며 기본 수명을 DB에 고정하지 않는다.
CREATE TABLE IF NOT EXISTS auth_sessions (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    CONSTRAINT auth_sessions_expiry_check CHECK (expires_at > created_at),
    CONSTRAINT auth_sessions_revoked_at_check CHECK (revoked_at >= created_at)
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires_at ON auth_sessions (expires_at);

COMMENT ON TABLE auth_sessions IS
    '로그인 세션. 만료 또는 폐기 시 연결된 액세스·갱신 토큰 모두 인증 거부';

-- 3. 세션별 갱신 토큰 이력. 교체 전 토큰도 남겨 재사용을 감지한다.
CREATE TABLE IF NOT EXISTS auth_refresh_tokens (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id  UUID NOT NULL REFERENCES auth_sessions(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_at     TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ,
    CONSTRAINT auth_refresh_tokens_hash_check CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT auth_refresh_tokens_expiry_check CHECK (expires_at > created_at),
    CONSTRAINT auth_refresh_tokens_used_at_check CHECK (used_at >= created_at),
    CONSTRAINT auth_refresh_tokens_revoked_at_check CHECK (revoked_at >= created_at)
);

CREATE INDEX IF NOT EXISTS idx_auth_refresh_tokens_session
    ON auth_refresh_tokens (session_id);
CREATE INDEX IF NOT EXISTS idx_auth_refresh_tokens_expires_at
    ON auth_refresh_tokens (expires_at);

-- 새 토큰 INSERT 전에 이전 토큰을 사용 처리하거나 폐기해야 한다.
-- 만료됐어도 used_at/revoked_at이 NULL이면 이 인덱스의 대상이다.
CREATE UNIQUE INDEX IF NOT EXISTS auth_refresh_tokens_one_pending_per_session
    ON auth_refresh_tokens (session_id)
    WHERE used_at IS NULL AND revoked_at IS NULL;

COMMENT ON COLUMN auth_refresh_tokens.token_hash IS
    '충분히 무작위인 갱신 토큰의 SHA-256 소문자 hex digest (64자). 원문 저장 금지';
COMMENT ON COLUMN auth_refresh_tokens.used_at IS
    '토큰 교체 시 소비한 시각. 이미 사용한 토큰 재사용 시 해당 세션 전체 폐기';
