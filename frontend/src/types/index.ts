/**
 * 화면별 데이터 타입. 설계서 9장 "화면별 필요 데이터"와 같은 필드명을 쓴다.
 * API 응답 타입의 정본은 backend/app/schemas.py, backend/app/user_schemas.py 이다.
 * 자가점검·챗봇·대시보드 타입은 아직 API가 없어 목업 기준이다.
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

/** 화면 표시용 근거 등급. 논문 응답에는 A~D만 온다 */
export type EvidenceGrade = Exclude<EvidenceLevel, 'guideline'>;

/** 논문 공통 칸. 날짜는 'YYYY-MM-DD' 문자열 */
type PaperBase = PaperSummary & {
  id: string;
  /** 원제 (영어) */
  title: string;
  published_at: string | null;
  journal: string | null;
  pubmed_url: string | null;
  study_type: string | null;
  /** DB 원래 값 '1'~'6'. 화면에는 evidence_grade를 쓴다 */
  evidence_level: string | null;
  /** 화면 표시용 근거 등급 A~D. 등급이 없으면 null (배지 숨김) */
  evidence_grade: EvidenceGrade | null;
};

/** GET /recommendations 의 카드 한 장 (RecommendationItem) */
export type FeedItem = PaperBase & {
  /** mode="search"일 때만. ai면 null */
  relevance_score: number | null;
  /** 왜 이 사용자에게 관련 있는지 (개인화 설명). mode="ai"일 때만, 아니면 null */
  personal_reason: string | null;
  /** mode="ai"일 때만, 아니면 null */
  category: Category | null;
};

/** GET /recommendations/{user_id} (RecommendationResponse) */
export type RecommendationResponse = {
  user_id: string;
  /** 요청에 준 patient_id 그대로. 없으면 null = 관리 환자 전체 기준 */
  patient_id: string | null;
  /** ai: AI 피드, search: 검색 방식(폴백) */
  mode: 'ai' | 'search';
  /** 이번에 쓴 검색어 목록. search일 때만 */
  queries: string[] | null;
  items: FeedItem[];
  /** 저장된 AI 피드의 처음 생성 시각. search거나 저장 실패면 null */
  generated_at: string | null;
};

/** GET /papers/{id} (PaperDetailResponse) */
export type PaperDetail = PaperBase & {
  abstract: string | null;
  doi: string | null;
  publication_types: string[];
  mesh_terms: string[];
};

/** 치매 단계 (user_schemas.DEMENTIA_STAGES) */
export type DementiaStage = '경도인지장애' | '경도' | '중등도' | '중증';

export type Patient = {
  id: string;
  caregiver_id: string;
  /** 이름 또는 별칭 (예: 어머니) */
  name: string | null;
  dementia_stage: DementiaStage | null;
  diagnosis_date: string | null;
  symptoms: string[];
  interests: string[];
};

/**
 * 돌보는 분 추가·수정 때 보내는 값 (user_schemas.PatientIn / PatientUpdate).
 * null이면 값을 비운다. symptoms·interests는 목록 전체를 새 목록으로 바꾼다.
 */
export type PatientInput = {
  name: string | null;
  dementia_stage: DementiaStage | null;
  /** 'YYYY-MM-DD', 미래 날짜 불가 */
  diagnosis_date: string | null;
  symptoms: string[];
  interests: string[];
};

export type Medication = {
  id: string;
  drug_name: string;
  dosage: string | null;
  frequency: string | null;
  is_taking: boolean;
};

export type MedicalVisit = {
  id: string;
  visit_date: string;
  is_visited: boolean;
  hospital_name: string | null;
  department: string | null;
};

export type Assessment = {
  id: string;
  assessment_type: string;
  score: number | null;
  result_detail: string | null;
  assessed_at: string;
};

/** 간병인 자가점검 (caregiver_profiles). 저장한 적이 없으면 null */
export type CaregiverProfile = {
  user_id: string;
  /** 환자와의 관계 (예: 자녀, 배우자) */
  relationship: string | null;
  /** Zarit 부담척도 0~88 */
  burden_score: number | null;
  /** PHQ-9 우울 0~27 */
  mood_score: number | null;
  lifestyle_tags: string[];
  /** 마지막 자가점검 시각 */
  updated_at: string;
};

/** UserProfileContext.patients 한 항목 */
export type PatientContext = {
  patient: Patient;
  latest_assessments: Assessment[];
  current_medications: Medication[];
  recent_visits: MedicalVisit[];
  upcoming_visits: MedicalVisit[];
};

/** GET /feed/{user_id} — 간병인 + 담당 환자 전원 */
export type UserProfileContext = {
  user_id: string;
  name: string;
  /** 피드 갱신 주기(일) 1~7 */
  feed_refresh_days: number;
  caregiver_profile: CaregiverProfile | null;
  patients: PatientContext[];
  generated_at: string;
};

/** GET·PATCH /users/{user_id}/feed-settings */
export type FeedSettings = {
  user_id: string;
  feed_refresh_days: number;
};

export type SelfCheckQuestion = {
  id: string;
  text: string;
  /** 문항 아래 작은 글씨 설명 (예시·제외 조건) */
  note?: string;
  options: { label: string; score: number }[];
};

/**
 * 챗봇 답변의 근거 자료 한 건. 서버가 검색 원본에서 복사한 값이다 (AI 쪽 outputs.ResolvedEvidence).
 * LLM이 만든 값이 아니며, 없는 값은 null 그대로 온다 (가짜 링크를 만들지 않는다).
 */
export type ChatSource = {
  /** new_research: 최신 연구(논문), guideline: 공식 지침 */
  evidence_type: 'new_research' | 'guideline';
  title: string;
  pmid: string | null;
  doi: string | null;
  journal: string | null;
  publication_year: number | null;
  study_type: string | null;
  /** 지침을 낸 기관 (guideline일 때) */
  organization: string | null;
  source_url: string | null;
  full_text_available: boolean;
};

/**
 * 챗봇 API 응답 (제안 형식). answer = ChatAnswerV1.answer,
 * sources = 인용 순서(citation_refs 순서)대로 서버가 조립한 근거. 0개도 정상이다.
 */
export type ChatResponse = {
  answer: string;
  sources: ChatSource[];
};

export type ChatMessage = {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  /** assistant 답변의 근거. 화면에서 [1]부터 번호를 붙인다 (답변 본문 속 번호와는 연결하지 않는다) */
  sources?: ChatSource[];
  /** 답변을 받지 못한 경우 (오류 안내 말풍선) */
  failed?: boolean;
};

