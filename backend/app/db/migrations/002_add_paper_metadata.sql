-- ============================================================
-- 002: papers 논문 메타데이터 컬럼 추가
-- 담당: 박주현(데이터) / AI 담당(김현서)과 합의 (2026-09-29)
--
-- 목적: AI 파이프라인이 근거 등급 분류와 주제 필터링에 쓸 메타데이터를
--       수집 단계에서 함께 저장한다.
--         - journal            : 학술지명
--         - doi                : DOI (PubMed 외 출처와 대조할 때 쓴다)
--         - publication_types  : PubMed PublicationType 목록
--                                (Randomized Controlled Trial, Review 등 → study_type 판단 근거)
--         - mesh_terms         : MeSH 용어 목록 (주제 필터링)
--
-- 주의: 임베딩 차원(paper_embeddings.embedding vector(768))은 여기서 건드리지 않는다.
--       AI 담당이 모델을 확정한 뒤(2026-09-30 예정) 별도 마이그레이션으로 처리한다.
--
-- 적용: 새 DB는 docker compose up -d 시 001 다음에 파일명 순서로 자동 실행된다.
--       이미 떠 있는 DB에는 아래처럼 직접 적용한다.
--         docker exec -i mindcare-db psql -U mindcare -d mindcare < app/db/migrations/002_add_paper_metadata.sql
--       (IF NOT EXISTS라서 여러 번 실행해도 안전하다.)
-- ============================================================

ALTER TABLE papers
    ADD COLUMN IF NOT EXISTS journal           TEXT,
    ADD COLUMN IF NOT EXISTS doi               TEXT,
    ADD COLUMN IF NOT EXISTS publication_types TEXT[] DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS mesh_terms        TEXT[] DEFAULT '{}';

-- 주제 필터링용: mesh_terms @> ARRAY['Sleep Wake Disorders'] 같은 배열 조건을 빠르게 처리한다.
CREATE INDEX IF NOT EXISTS idx_papers_mesh_terms ON papers USING GIN (mesh_terms);

COMMENT ON COLUMN papers.journal           IS '학술지명 (PubMed Journal/Title)';
COMMENT ON COLUMN papers.doi               IS 'DOI (ArticleId IdType="doi")';
COMMENT ON COLUMN papers.publication_types IS 'PubMed PublicationType 목록 — 근거 등급 분류 입력';
COMMENT ON COLUMN papers.mesh_terms        IS 'MeSH 용어 목록 — 주제 필터링';
