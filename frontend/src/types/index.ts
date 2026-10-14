/**
 * 화면별 데이터 타입. 설계서 9장 "화면별 필요 데이터"와 같은 필드명을 쓴다.
 * 백엔드 응답 형식이 확정되면 이 파일을 기준으로 맞춘다.
 */

/** A~D: 연구 근거 등급, guideline: 기반 지식층(공식 가이드라인) */
export type EvidenceLevel = 'A' | 'B' | 'C' | 'D' | 'guideline';

export type Category = 'treatment' | 'care' | 'prevention' | 'diagnosis';

/** 화면에 보여줄 논문 제목. 한국어 제목이 없으면 원제 */
export function displayTitle(paper: { title: string; title_ko: string | null }): string {
  return paper.title_ko || paper.title;
}

/** 설명 난이도 (쉽게 / 자세하게) */
export type ReadingLevel = 'easy' | 'detail';

export type UserType = 'caregiver' | 'self';

/** 논문 요약 (paper_analysis). 요약 전이면 모두 null */
export type PaperSummary = {
  /** 한국어 제목. null이면 원제(title)를 보여준다 */
  title_ko: string | null;
  /** ① 연구 배경 (왜 이 연구를 했나) */
  summary_background: string | null;
  /** ② 새로 밝혀진 것 */
  summary_finding: string | null;
  /** ③ 한계 */
  summary_limitation: string | null;
};

/** GET /recommendations 의 카드 한 장 */
export type FeedItem = PaperSummary & {
  id: string;
  /** 원제 */
  title: string;
  category: Category;
  /** 화면 표시용 근거 등급 A~D. 등급이 없으면 null */
  evidence_grade: EvidenceLevel | null;
  /** 왜 이 사용자에게 관련 있는지 한 줄 (개인화 설명). AI 추천이 아니면 null */
  personal_reason: string | null;
  journal: string;
  published_at: string;
};

export type Feed = {
  week_range: string;
  items: FeedItem[];
  next_cursor: string | null;
};

export type Paragraph = {
  text: string;
  /** 본문에 붙는 출처 번호 */
  citation?: number;
};

/** GET /papers/{id}?level= */
export type PaperDetail = PaperSummary & {
  id: string;
  /** 원제 */
  title: string;
  /** 화면 표시용 근거 등급 A~D. 등급이 없으면 null */
  evidence_grade: EvidenceLevel | null;
  study_type: string;
  journal: string;
  published_at: string;
  authors: string;
  body: Record<ReadingLevel, Paragraph[]>;
  /** "우리 상황에서는?" 개인화 설명. 추천 카드의 personal_reason을 그대로 쓴다 */
  personal_reason: string | null;
  limitations: string[];
  glossary: { term: string; meaning: string }[];
  pubmed_url: string;
  doi?: string;
};

export type SelfCheckQuestion = {
  id: string;
  text: string;
  options: { label: string; score: number }[];
};

export type SelfCheckResult = {
  date: string;
  score: number;
  level: 'normal' | 'caution' | 'visit';
  message: string;
  recommend_visit: boolean;
};

export type Citation = {
  no: number;
  title: string;
  type: 'guideline' | 'paper';
  evidence_level: EvidenceLevel;
  url: string;
};

export type ChatMessage = {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  citations?: Citation[];
};

/** GET /dashboard */
export type Dashboard = {
  user_name: string;
  new_papers_count: number;
  top_paper: { id: string; title: string };
  last_self_check: { date: string; level: SelfCheckResult['level'] } | null;
  next_check_date: string;
  today_log_written: boolean;
};

export type Profile = {
  user_name: string;
  user_type: UserType;
  reading_level: ReadingLevel;
  patient: { relation: string; stage: string; symptoms: string[] };
};
