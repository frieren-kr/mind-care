/** 'YYYY-MM-DD' → 'YYYY.MM.DD' (신문 지면 표기). 형식이 다르면 그대로 */
export function formatDate(value: string | null | undefined): string | null {
  if (!value) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  return m ? `${m[1]}.${m[2]}.${m[3]}` : value;
}

/** 오늘 날짜 머리말: '11월 7일 토요일' */
export function todayLabel(now: Date = new Date()): string {
  const days = ['일', '월', '화', '수', '목', '금', '토'];
  return `${now.getMonth() + 1}월 ${now.getDate()}일 ${days[now.getDay()]}요일`;
}
