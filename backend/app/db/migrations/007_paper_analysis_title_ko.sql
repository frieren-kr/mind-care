-- ============================================================
-- 007. paper_analysis: title_ko 추가 + summary_comparison → summary_background
-- 담당: 박주현
-- ============================================================
-- 논문 원제(papers.title)는 영어라서 보호자가 목록에서 바로 읽기 어렵다.
-- 요약 배치(scripts/summarize_papers.py)가 요약 3칸과 함께 한국어 제목을 만들어
-- 이 칸에 저장한다. 요약이 아직 없는 논문은 NULL이다.
--
-- 001은 이미 적용된 파일이라 수정하지 않고 이 파일로 추가한다.
-- IF NOT EXISTS라서 여러 번 실행해도 안전하다.
-- ============================================================

ALTER TABLE paper_analysis
    ADD COLUMN IF NOT EXISTS title_ko TEXT;  -- 한국어 제목 (보호자가 읽기 쉬운 말, 40자 안팎)

-- ------------------------------------------------------------
-- summary_comparison → summary_background 이름 변경
--   칸의 뜻이 "기존 권고와 비교"에서 "연구 배경(왜 이 연구를 했나)"으로 바뀌었다.
--   RENAME COLUMN에는 IF EXISTS가 없어서, 옛 이름이 있고 새 이름이 없을 때만 바꾼다.
-- ------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'paper_analysis'
                  AND column_name = 'summary_comparison')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'paper_analysis'
                  AND column_name = 'summary_background')
    THEN
        ALTER TABLE paper_analysis RENAME COLUMN summary_comparison TO summary_background;
    END IF;
END $$;
