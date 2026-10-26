/**
 * 백엔드 API 호출 함수. 주소·응답 형식은 docs/api.md, docs/users.md 기준.
 */
import { ApiError, request } from '@/api/client';
import { DEV_USER_ID } from '@/api/config';
import type {
  FeedSettings,
  PaperDetail,
  Patient,
  PatientInput,
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

/**
 * 돌보는 분 추가 — POST /users/{user_id}/patients (본문 PatientIn).
 * ★ 백엔드에 아직 없는 API다 (2026-10-26 기준, 한슬 님께 요청 예정). 주소는 /feed_patient/{user_id}/{patient_id}처럼
 * 간병인 id를 주소에 넣는 방식으로 제안했다. 만들어지기 전에는 "서버에 아직 이 기능이 없어요" 오류가 난다.
 */
export function createPatient(userId: string, data: PatientInput): Promise<Patient> {
  return request(`/users/${userId}/patients`, {
    method: 'POST',
    body: { caregiver_id: userId, ...data },
  });
}

/** 돌보는 분 정보 수정 — PATCH /users/{user_id}/patients/{patient_id} (본문 PatientUpdate). ★ 아직 없는 API */
export function updatePatient(userId: string, patientId: string, data: PatientInput): Promise<Patient> {
  return request(`/users/${userId}/patients/${patientId}`, {
    method: 'PATCH',
    body: data,
  });
}

/** PATCH /users/{user_id}/feed-settings — 피드 갱신 주기(1~7일) 저장 */
export function updateFeedSettings(userId: string, feedRefreshDays: number): Promise<FeedSettings> {
  return request(`/users/${userId}/feed-settings`, {
    method: 'PATCH',
    body: { feed_refresh_days: feedRefreshDays },
  });
}
