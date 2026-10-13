-- ============================================================
-- 008. guideline_knowledge를 "가이드라인 문서의 조각 + 임베딩" 테이블로 확장
-- 담당: 박주현 (요청: 김현서 — 가이드라인 검색)
-- ============================================================
-- 001의 guideline_knowledge는 topic / content / source / tags / updated_at만 있었다.
-- 이제 한 줄 = 가이드라인 문서의 한 조각(장·절 단위)이고, 조각마다 임베딩을 같이 둔다
-- (논문은 papers + paper_embeddings 두 테이블이지만, 가이드라인은 본문과 임베딩을
--  항상 함께 저장하므로 한 테이블에 둔다).
--
--   조각 나누기 · 임베딩 생성 = AI(김현서)
--   저장 · 검색 함수          = DB(박주현)
--     save_guidelines() / search_similar_guidelines() / get_guidelines_by_ids()
--
-- 001은 이미 적용된 파일이라 수정하지 않고 이 파일로 추가한다.
-- 모든 문장을 IF NOT EXISTS로 써서 여러 번 실행해도 안전하다.
-- ============================================================

-- ------------------------------------------------------------
-- 0) NOT NULL 칸을 기본값 없이 추가하므로 테이블이 비어 있어야 한다.
--    (지금까지 이 테이블에 저장하는 함수가 없어서 팀 DB는 모두 비어 있다.)
--    행이 있는데 아직 008 전이라면 알아보기 쉬운 메시지로 멈춘다.
-- ------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM guideline_knowledge)
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'guideline_knowledge' AND column_name = 'doc_title')
    THEN
        RAISE EXCEPTION '008: guideline_knowledge에 기존 행이 있어 NOT NULL 칸을 추가할 수 없습니다. 먼저 비우세요: TRUNCATE guideline_knowledge;';
    END IF;
END $$;

-- ------------------------------------------------------------
-- 1) 칸 추가
--    기존 칸은 그대로 쓴다: topic(주제), content(조각 본문), source(발행 기관), tags, updated_at
-- ------------------------------------------------------------
ALTER TABLE guideline_knowledge
    ADD COLUMN IF NOT EXISTS doc_title       TEXT NOT NULL,          -- 가이드라인(문서) 이름
    ADD COLUMN IF NOT EXISTS section         TEXT,                   -- 장·절 제목
    ADD COLUMN IF NOT EXISTS chunk_index     INT NOT NULL,           -- 문서 안에서 조각의 순서
    ADD COLUMN IF NOT EXISTS source_url      TEXT NOT NULL,          -- 원문 링크 (문서를 구분하는 키)
    ADD COLUMN IF NOT EXISTS published_year  SMALLINT,               -- 발행 연도
    ADD COLUMN IF NOT EXISTS language        TEXT,                   -- 'ko' / 'en'
    ADD COLUMN IF NOT EXISTS embedding       vector(1024),           -- 조각 임베딩 — bge-m3 (논문과 같은 모델·차원)
    ADD COLUMN IF NOT EXISTS embedding_model TEXT,                   -- 임베딩을 만든 모델 이름
    ADD COLUMN IF NOT EXISTS created_at      TIMESTAMPTZ NOT NULL DEFAULT now();  -- 처음 저장한 시각

-- ------------------------------------------------------------
-- 2) 조각을 가리키는 키: (원문 링크, 조각 순서)
--    save_guidelines()의 ON CONFLICT 대상이다 — 같은 조각을 다시 저장하면 갱신된다.
--    UNIQUE 제약 대신 UNIQUE INDEX로 두는 이유는 IF NOT EXISTS를 쓸 수 있기 때문이다 (006과 같은 방식).
-- ------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS guideline_knowledge_source_chunk_key
    ON guideline_knowledge (source_url, chunk_index);

-- ------------------------------------------------------------
-- 3) 임베딩 검색용 HNSW 인덱스 (코사인 거리) — paper_embeddings(003)와 같은 방식
--    search_similar_guidelines()의 ORDER BY embedding <=> ... 가 이 인덱스를 탄다.
-- ------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_guideline_knowledge_hnsw
    ON guideline_knowledge USING hnsw (embedding vector_cosine_ops);

COMMENT ON COLUMN guideline_knowledge.embedding IS '가이드라인 조각 임베딩 — bge-m3 (1024차원). 변경 시 새 마이그레이션 + schemas.EMBEDDING_DIM 동시 수정';
