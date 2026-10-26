/**
 * 인지기능장애 평가도구(KDSQ-C, Korean Dementia Screening Questionnaire - C).
 * 출처: 「건강검진 실시기준」 [별지 제13호 서식]. 문항·선택지·안내 문구는 서식 원문을 글자 그대로 옮겼다
 * (의료 설문이라 문구를 바꾸지 않는다). 15문항 × 0~2점, 30점 만점.
 */
import type { SelfCheckQuestion } from '@/types';

export const KDSQ_C_SOURCE = '「건강검진 실시기준」 [별지 제13호 서식] 인지기능장애 평가도구(KDSQ-C)';

/** 서식 원문 안내 문구 */
export const KDSQ_C_INSTRUCTION =
  '아래의 각 항목에 대하여, 1년 전과 비교하여 현재 상태에 해당하는 곳에 표시해 주십시오. ' +
  '(동행한 가족이 있으면 가족이 작성하시고, 없으면 본인이 작성하십시오)';

export const KDSQ_C_MAX_SCORE = 30;

/**
 * 이 점수 이상이면 "인지기능 저하 의심"으로 안내한다.
 * 위 서식에는 판정 기준이 없다. 국가건강검진에서 쓰는 6점 기준을 따랐으며, 근거 문서로 확인이 필요하다.
 */
export const KDSQ_C_REFERRAL_SCORE = 6;

const options = [
  { label: '아니다', score: 0 },
  { label: '가끔(조금) 그렇다', score: 1 },
  { label: '자주(많이) 그렇다', score: 2 },
];

const items: { text: string; note?: string }[] = [
  { text: '오늘이 몇 월이고, 무슨 요일인지를 잘 모른다' },
  { text: '자기가 놔둔 물건을 찾지 못 한다' },
  { text: '같은 질문을 반복해서 한다' },
  { text: '약속을 하고서 잊어버린다' },
  { text: '물건을 가지러 갔다가 잊어버리고 그냥 온다' },
  { text: '물건이나, 사람의 이름을 대기가 힘들어 머뭇거린다' },
  { text: '대화 중 내용이 이해되지 않아 반복해서 물어 본다' },
  { text: '길을 잃거나 헤맨 적이 있다' },
  { text: '예전에 비해서 계산능력이 떨어졌다', note: '예: 물건값이나 거스름돈 계산을 못한다' },
  { text: '예전에 비해 성격이 변했다' },
  { text: '이전에 잘 다루던 기구의 사용이 서툴러졌다', note: '세탁기, 전기밥솥, 경운기 등' },
  { text: '예전에 비해 방이나 집안의 정리정돈을 하지 못 한다' },
  { text: '상황에 맞게 스스로 옷을 선택하여 입지 못 한다' },
  {
    text: '혼자 대중교통 수단을 이용하여 목적지에 가기 힘들다',
    note: '신체적인 문제(관절염)로 인한 것은 제외됨',
  },
  { text: '내복이나 옷이 더러워져도 갈아입지 않으려고 한다' },
];

export const KDSQ_C_QUESTIONS: SelfCheckQuestion[] = items.map((item, index) => ({
  id: `kdsq-c-${index + 1}`,
  ...item,
  options,
}));
