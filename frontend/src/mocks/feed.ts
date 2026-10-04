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
      title: "반복 질문엔 '다시 설명'보다 '안심시키기'가 효과",
      summary_bullets: ['간병인 120명 대상 교육 연구', '갈등 상황이 30% 줄었어요'],
      category: 'care',
      evidence_level: 'A',
      personal_reason: '어머니처럼 같은 질문을 반복하는 경우와 관련 있어요',
      journal: 'JAMA',
      published_at: '2026-10-02',
      read_minutes: 3,
      is_bookmarked: false,
    },
    {
      id: 'p2',
      title: '밤잠 설치는 치매 환자, 낮 햇빛 산책이 도움',
      summary_bullets: ['요양시설 환자 86명 관찰', '밤에 깨는 횟수가 줄어든 경향'],
      category: 'care',
      evidence_level: 'B',
      personal_reason: '수면 문제를 기록하셨기 때문에 골랐어요',
      journal: 'Sleep Medicine',
      published_at: '2026-10-01',
      read_minutes: 4,
      is_bookmarked: true,
    },
    {
      id: 'p3',
      title: '혈액검사로 알츠하이머 가능성 확인, 정확도 90%대',
      summary_bullets: ['p-tau217 혈액 지표 연구', '뇌 영상 검사 부담을 줄일 가능성'],
      category: 'diagnosis',
      evidence_level: 'B',
      personal_reason: '진단 방법에 관심을 표시하셨어요',
      journal: 'Nature Medicine',
      published_at: '2026-09-30',
      read_minutes: 3,
      is_bookmarked: false,
    },
    {
      id: 'p4',
      title: '[쥐 실험] 장내 미생물이 기억력에 영향',
      summary_bullets: ['동물 실험 단계 연구', '사람에게 적용하려면 추가 연구 필요'],
      category: 'prevention',
      evidence_level: 'D',
      personal_reason: '예방 분야의 새 소식이에요',
      journal: 'Cell Reports',
      published_at: '2026-09-29',
      read_minutes: 2,
      is_bookmarked: false,
    },
  ],
};

const paperP1: PaperDetail = {
  id: 'p1',
  title: "반복 질문엔 '다시 설명'보다 '안심시키기'가 효과",
  evidence_level: 'A',
  study_type: '무작위 대조 시험',
  journal: 'JAMA',
  published_at: '2026-10-02',
  authors: 'Kim et al.',
  summary_bullets: [
    '간병인 120명을 두 그룹으로 나눠 비교했어요',
    '안심시키는 대화법을 배운 그룹에서 갈등이 30% 줄었어요',
    '간병인의 스트레스도 함께 낮아졌어요',
  ],
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
  personal_meaning:
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
