import type { RecommendationResponse } from '@/types';

/** /recommendations가 한 번에 주는 최대 편수 (k 최대값). AI는 관련성이 충분한 논문만 골라 더 적게 줄 수 있다 */
export const MAX_RECOMMENDATIONS = 5;

/** "오늘 만든 맞춤 추천" / "3일 전에 만든 맞춤 추천" (날짜 기준) */
function generatedLabel(iso: string): string | null {
  const created = new Date(iso);
  if (Number.isNaN(created.getTime())) return null;
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((startOfDay(new Date()) - startOfDay(created)) / 86_400_000);
  return days <= 0 ? '오늘 만든 맞춤 추천' : `${days}일 전에 만든 맞춤 추천`;
}

/** 피드 위쪽 한 줄 설명 */
export function feedCaption(feed: RecommendationResponse): string {
  if (feed.mode === 'search') return '돌보시는 분의 증상·관심사와 가까운 연구예요';
  return (feed.generated_at && generatedLabel(feed.generated_at)) || '맞춤 추천 연구예요';
}

/**
 * AI가 최대 5편보다 적게 골랐을 때의 안내. 개수를 채우려고 관련성이 낮은 논문을 넣지 않기 때문이다 (10/25 웅 님).
 * 5편이거나 검색 방식이면 null.
 */
export function fewItemsNotice(feed: RecommendationResponse): string | null {
  const count = feed.items.length;
  if (feed.mode !== 'ai' || count === 0 || count >= MAX_RECOMMENDATIONS) return null;
  return `돌보시는 분께 꼭 맞는 연구만 골라 이번에는 ${count}편을 추천해요.`;
}
