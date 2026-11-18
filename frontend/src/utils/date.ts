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

/** 'YYYY-MM-DD' (폰 시간 기준). 기본은 오늘 */
export function isoDate(date: Date = new Date()): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

/** 'YYYY-MM-DD'가 오늘에서 며칠 뒤인지 (지난 날은 음수, 오늘은 0) */
export function daysFromToday(value: string): number {
  const [y, m, d] = value.slice(0, 10).split('-').map(Number);
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  return Math.round((new Date(y, m - 1, d).getTime() - start.getTime()) / 86_400_000);
}

/** 연·월·일 입력칸 → 'YYYY-MM-DD'. 셋 다 비면 null. 없는 날짜·(허용 안 할 때) 미래 날짜는 오류 문구 */
export function parseDateParts(
  year: string,
  month: string,
  day: string,
  { allowFuture = false }: { allowFuture?: boolean } = {},
): { value: string | null } | { error: string } {
  if (!year.trim() && !month.trim() && !day.trim()) return { value: null };
  const [y, m, d] = [year, month, day].map((v) => Number(v.trim()));
  const date = new Date(y, m - 1, d);
  const valid = [y, m, d].every(Number.isInteger) && y >= 1900 && date.getMonth() === m - 1 && date.getDate() === d;
  if (!valid) return { error: '날짜를 올바르게 입력해 주세요. (예: 2026년 12월 3일)' };
  const value = isoDate(date);
  if (!allowFuture && value > isoDate()) return { error: '오늘 이후 날짜는 고를 수 없어요.' };
  return { value };
}
