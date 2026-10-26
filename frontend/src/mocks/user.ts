// 목업 데이터. 화면 확인용 예시이며, 백엔드 API가 준비되면 교체한다.
import type { ChatMessage } from '@/types';

export const mockChat: ChatMessage[] = [
  { id: 'm1', role: 'user', text: '어머니가 같은 질문을 반복해요' },
  {
    id: 'm2',
    role: 'assistant',
    text: '반복 질문은 기억 저하와 함께 불안에서 오는 경우가 많아요[1]. 다시 설명하기보다 "걱정 마세요, 제가 챙길게요"처럼 안심시키는 말을 먼저 해보세요[2].',
    citations: [
      { no: 1, title: '중앙치매센터 치매 돌봄 가이드', type: 'guideline', evidence_level: 'guideline', url: 'https://www.nid.or.kr/' },
      { no: 2, title: 'Kim et al., JAMA 2026', type: 'paper', evidence_level: 'A', url: 'https://pubmed.ncbi.nlm.nih.gov/' },
    ],
  },
];

export const suggestedQuestions = ['밤에 잠을 안 주무세요', '식사를 거부하세요', '간병이 너무 지쳐요'];
