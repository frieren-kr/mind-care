-- ============================================================
-- 003: 임베딩 차원 768 → 1024 + IVFFlat → HNSW 인덱스 교체
-- 담당: 박주현(데이터) / AI 담당(김현서)과 합의 (2026-10-01)
--
-- 배경: 임베딩 모델을 bge-m3(1024차원)로 확정했다.
--       001_init.sql은 vector(768), config.py의 embedding_dim은 1536이라
--       서로 어긋나 있었다. 이 마이그레이션으로 DB를 1024에 맞추고,
--       config.py / .env.example의 EMBEDDING_DIM도 1024로 함께 고친다.
--
-- 왜 001_init.sql을 고치지 않는가:
--       마이그레이션 자동 실행은 DB를 처음 만들 때(볼륨이 비어 있을 때)만 일어난다.
--       이미 DB를 띄워 둔 팀원에게는 001 수정이 반영되지 않으므로, 새 번호 파일로 추가한다.
--       (AGENTS.md "이미 적용된 마이그레이션 파일은 수정하지 않는다" 규칙)
--
-- 왜 IVFFlat 대신 HNSW인가:
--       IVFFlat은 데이터가 어느 정도 쌓인 뒤에 만들어야 클러스터(lists)가 제대로 잡힌다.
--       지금 paper_embeddings는 비어 있어서 IVFFlat을 만들어도 정확도가 나오지 않는다.
--       HNSW는 빈 테이블에 만들어 두고 행이 들어올 때마다 점진적으로 쌓여서 이 상황에 맞다.
--
-- 적용: 새 DB는 docker compose up -d 시 001 → 002 → 003 순서로 자동 실행된다.
--       이미 떠 있는 DB에는 루트 README.md "이미 DB를 띄워 둔 사람은 003을..." 참고.
--       (여러 번 실행해도 안전하다 — 이미 1024면 변경 단계를 건너뛴다.)
-- ============================================================

-- ------------------------------------------------------------
-- 1) 기존 IVFFlat 인덱스 제거
--    차원을 바꾸기 전에 지운다. 아래 3)에서 HNSW로 새로 만든다.
-- ------------------------------------------------------------
DROP INDEX IF EXISTS idx_paper_embeddings_vector;

-- ------------------------------------------------------------
-- 2) paper_embeddings.embedding: vector(768) → vector(1024)
--
--    ⚠️ 768차원 벡터는 1024차원으로 변환할 수 없다(없는 값을 만들어낼 수 없다).
--    그래서 기존 행이 있으면 지우고 차원을 바꾼다. 지금 이 테이블은 비어 있고,
--    설령 들어 있어도 검증되지 않은 테스트 임베딩이라 bge-m3로 다시 만들어야 한다.
--    삭제할 행이 있으면 NOTICE로 건수를 출력하므로 조용히 사라지지 않는다.
--    (papers / paper_analysis 는 건드리지 않는다. 임베딩만 다시 만들면 된다.)
-- ------------------------------------------------------------
DO $$
DECLARE
    current_type text;
    leftover     bigint;
BEGIN
    SELECT format_type(atttypid, atttypmod) INTO current_type
      FROM pg_attribute
     WHERE attrelid = 'paper_embeddings'::regclass
       AND attname = 'embedding'
       AND NOT attisdropped;

    IF current_type = 'vector(1024)' THEN
        RAISE NOTICE '003: paper_embeddings.embedding 이 이미 vector(1024) 입니다 — 차원 변경을 건너뜁니다.';
        RETURN;
    END IF;

    SELECT count(*) INTO leftover FROM paper_embeddings;
    IF leftover > 0 THEN
        RAISE NOTICE '003: 기존 임베딩 %건을 삭제합니다 (% → vector(1024) 로는 변환이 불가능합니다). bge-m3로 다시 생성하세요.',
            leftover, current_type;
        DELETE FROM paper_embeddings;
    END IF;

    ALTER TABLE paper_embeddings ALTER COLUMN embedding TYPE vector(1024);
    RAISE NOTICE '003: paper_embeddings.embedding 을 % → vector(1024) 로 변경했습니다.', current_type;
END $$;

-- ------------------------------------------------------------
-- 3) HNSW 인덱스 (코사인 거리)
--    search_similar()가 ORDER BY embedding <=> 질의벡터 로 검색하므로
--    연산자 클래스는 vector_cosine_ops 를 쓴다.
--    m / ef_construction 은 pgvector 기본값(16 / 64)을 그대로 쓴다 —
--    논문 수가 수만 건 단위로 늘어난 뒤에 측정해서 조정한다.
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_paper_embeddings_hnsw
    ON paper_embeddings USING hnsw (embedding vector_cosine_ops);

COMMENT ON COLUMN paper_embeddings.embedding IS '논문 임베딩 — bge-m3 (1024차원). 변경 시 새 마이그레이션 + config.py EMBEDDING_DIM 동시 수정';
