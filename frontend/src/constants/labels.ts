import type { Category } from '@/types';

export const CategoryLabel: Record<Category, string> = {
  treatment: '치료',
  care: '돌봄',
  prevention: '예방',
  diagnosis: '진단',
};

/**
 * 연구 유형(study_type) 화면 이름. 10/24 주현 님 제안 표 그대로.
 * 키는 백엔드 app/ai/retrieval/evidence.py의 LEVEL과 같다.
 */
const StudyTypeLabel: Record<string, string> = {
  meta_analysis: '여러 연구를 합쳐 분석한 연구',
  systematic_review: '여러 연구를 모아 정리한 연구',
  guideline: '진료 지침',
  rct: '무작위로 나눠 비교한 시험',
  clinical_trial: '임상시험',
  observational: '관찰 연구',
  qualitative: '면담 연구',
  review: '연구 정리 글',
  case_report: '환자 사례 보고',
  expert_opinion: '전문가 의견',
  protocol: '연구 계획(아직 결과 없음)',
  animal: '동물·세포 연구',
};

/** 화면에 보여줄 연구 유형. other·없음·표에 없는 값은 "연구 유형 확인 중" */
export function studyTypeLabel(studyType: string | null): string {
  return (studyType && StudyTypeLabel[studyType]) || '연구 유형 확인 중';
}
