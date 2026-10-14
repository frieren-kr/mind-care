// 목업 데이터. 실제 논문이 아닌 화면 확인용 예시이며, 백엔드 API가 준비되면 교체한다.
import type { Category, Feed, PaperDetail } from '@/types';

export const CategoryLabel: Record<Category, string> = {
  treatment: '치료',
  care: '돌봄',
  prevention: '예방',
  diagnosis: '진단',
};

export const mockFeed: Feed = {
  week_range: '10/1 ~ 10/7',
  next_cursor: null,
  items: [
    {
      id: 'p1',
      title: 'Reassurance-based communication for repetitive questioning in dementia',
      title_ko: "반복 질문엔 '다시 설명'보다 '안심시키기'가 효과",
      summary_background: '반복 질문에 어떻게 대답하는 게 좋은지 근거가 부족했어요.',
      summary_finding: '안심시키는 대화법을 배운 간병인 그룹에서 갈등이 30% 줄었어요.',
      summary_limitation: '미국 간병인 120명 대상이고 8주 이후 효과는 확인되지 않았어요.',
      category: 'care',
      evidence_grade: 'A',
      personal_reason: '어머니처럼 같은 질문을 반복하는 경우와 관련 있어요',
      journal: 'JAMA',
      published_at: '2026-10-02',
    },
    {
      id: 'p2',
      title: 'Daytime light exposure and nighttime awakenings in nursing home residents with dementia',
      title_ko: '밤잠 설치는 치매 환자, 낮 햇빛 산책이 도움',
      summary_background: '치매 환자의 밤잠 문제는 간병인 부담을 크게 늘려요.',
      summary_finding: '낮에 햇빛을 쬔 환자는 밤에 깨는 횟수가 줄어든 경향을 보였어요.',
      summary_limitation: '요양시설 환자 86명을 관찰한 연구라 원인과 결과로 단정하기 어려워요.',
      category: 'care',
      evidence_grade: 'B',
      personal_reason: '수면 문제를 기록하셨기 때문에 골랐어요',
      journal: 'Sleep Medicine',
      published_at: '2026-10-01',
    },
    {
      id: 'p3',
      title: 'Diagnostic accuracy of plasma p-tau217 for Alzheimer disease pathology',
      title_ko: '혈액검사로 알츠하이머 가능성 확인, 정확도 90%대',
      summary_background: '알츠하이머 확인에는 비용과 부담이 큰 뇌 영상 검사가 필요했어요.',
      summary_finding: 'p-tau217 혈액 지표가 90%대 정확도로 알츠하이머 병변을 가려냈어요.',
      summary_limitation: '진료에 바로 쓰려면 다른 집단에서도 같은 결과가 나와야 해요.',
      category: 'diagnosis',
      evidence_grade: 'B',
      personal_reason: '진단 방법에 관심을 표시하셨어요',
      journal: 'Nature Medicine',
      published_at: '2026-09-30',
    },
    {
      id: 'p4',
      title: 'Gut microbiota modulates memory in a mouse model of Alzheimer disease',
      title_ko: '[쥐 실험] 장내 미생물이 기억력에 영향',
      summary_background: '장내 미생물과 뇌 건강의 관계가 주목받고 있어요.',
      summary_finding: '장내 미생물을 바꾼 쥐에서 기억력 검사 결과가 달라졌어요.',
      summary_limitation: '동물 실험이라 사람에게 그대로 적용할 수 없어요.',
      category: 'prevention',
      evidence_grade: 'D',
      personal_reason: '예방 분야의 새 소식이에요',
      journal: 'Cell Reports',
      published_at: '2026-09-29',
    },
  ],
};

const paperP1: PaperDetail = {
  id: 'p1',
  title: 'Reassurance-based communication for repetitive questioning in dementia',
  title_ko: "반복 질문엔 '다시 설명'보다 '안심시키기'가 효과",
  evidence_grade: 'A',
  study_type: '무작위 대조 시험',
  journal: 'JAMA',
  published_at: '2026-10-02',
  authors: 'Kim et al.',
  summary_background: '반복 질문에 어떻게 대답하는 게 좋은지 근거가 부족했어요.',
  summary_finding: '안심시키는 대화법을 배운 간병인 그룹에서 갈등이 30% 줄고 스트레스도 낮아졌어요.',
  summary_limitation: '미국 간병인 120명 대상이고 8주 이후 효과는 확인되지 않았어요.',
  body: {
    easy: [
      {
        text: '치매 환자가 같은 질문을 반복하는 것은 기억이 사라져서이기도 하지만, 불안한 마음 때문인 경우가 많아요.',
        citation: 1,
      },
      {
        text: '연구팀은 간병인에게 "아까 말했잖아요" 대신 "걱정 마세요, 제가 챙길게요"처럼 마음을 안심시키는 말을 하도록 가르쳤어요.',
        citation: 2,
      },
      {
        text: '8주 뒤, 이 방법을 쓴 가족은 다툼이 30% 줄었고 간병인 스트레스도 낮아졌어요.',
        citation: 2,
      },
    ],
    detail: [
      {
        text: '반복 질문은 일화 기억 저하와 함께 불안 같은 행동심리증상(BPSD)과 관련된다고 알려져 있다.',
        citation: 1,
      },
      {
        text: '연구진은 간병인 120명을 무작위로 배정해, 정서 확인 중심 의사소통 교육군과 일반 정보 제공군을 8주간 비교했다.',
        citation: 2,
      },
      {
        text: '교육군에서 간병인이 보고한 갈등 빈도가 30% 감소했으며, 간병 부담 척도 점수도 유의하게 낮았다.',
        citation: 2,
      },
    ],
  },
  personal_reason:
    '어머니가 같은 질문을 반복하실 때, 다시 설명하기보다 "걱정 마세요"처럼 안심시키는 말을 먼저 해보세요.',
  limitations: [
    '미국 가족 간병인 대상이라 국내 상황과 다를 수 있어요',
    '8주 이후의 장기 효과는 확인되지 않았어요',
  ],
  glossary: [
    { term: '무작위 대조 시험', meaning: '참가자를 무작위로 나눠 효과를 비교하는, 믿을 만한 연구 방법' },
    { term: '행동심리증상', meaning: '치매로 생기는 불안, 반복 행동, 수면 문제 같은 증상' },
  ],
  pubmed_url: 'https://pubmed.ncbi.nlm.nih.gov/',
};

/** 목업에는 p1 상세만 있다. 나머지 카드는 같은 상세를 보여준다. */
export function getMockPaper(id: string): PaperDetail {
  const item = mockFeed.items.find((i) => i.id === id);
  return item ? { ...paperP1, ...item, id } : paperP1;
}
