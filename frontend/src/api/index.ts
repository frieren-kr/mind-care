/**
 * 백엔드 API 호출 함수. 주소·응답 형식은 docs/api.md, docs/users.md 기준.
 */
import { ApiError, request } from '@/api/client';
import { DEV_USER_ID } from '@/api/config';
import type {
  FeedSettings,
  PaperDetail,
  RecommendationResponse,
  UserProfileContext,
} from '@/types';

export { ApiError } from '@/api/client';
export { API_URL } from '@/api/config';

/** 지금 사용자 id. 로그인 전에는 EXPO_PUBLIC_DEV_USER_ID */
export function currentUserId(): string {
  if (!DEV_USER_ID) {
    throw new ApiError(
      '사용자 id가 설정되지 않았어요. frontend/.env.local에 EXPO_PUBLIC_DEV_USER_ID를 넣고 앱을 다시 실행해 주세요.',
    );
  }
  return DEV_USER_ID;
}

/** GET /recommendations/{user_id} — 간병인(관리 환자 전체) 기준 논문 최대 k편 */
export function getRecommendations(
  userId: string,
  options: { patientId?: string | null; refresh?: boolean; k?: number } = {},
): Promise<RecommendationResponse> {
  return request(`/recommendations/${userId}`, {
    query: { patient_id: options.patientId, k: options.k, refresh: options.refresh || undefined },
    // AI 피드는 LLM 호출, 검색 방식은 첫 요청에 임베딩 모델 로딩이 있어 오래 걸릴 수 있다
    timeoutMs: 90_000,
  });
}

/** GET /papers/{paper_id} — 논문 상세 + 요약 3칸 */
export function getPaper(paperId: string): Promise<PaperDetail> {
  return request(`/papers/${paperId}`);
}

/** GET /feed/{user_id} — 간병인 + 담당 환자 전원의 현재 상태 */
export function getUserProfile(userId: string): Promise<UserProfileContext> {
  return request(`/feed/${userId}`);
}

/** PATCH /users/{user_id}/feed-settings — 피드 갱신 주기(1~7일) 저장 */
export function updateFeedSettings(userId: string, feedRefreshDays: number): Promise<FeedSettings> {
  return request(`/users/${userId}/feed-settings`, {
    method: 'PATCH',
    body: { feed_refresh_days: feedRefreshDays },
  });
}
