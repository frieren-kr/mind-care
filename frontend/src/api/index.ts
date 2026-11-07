/**
 * 백엔드 API 호출 함수. 주소·응답 형식은 docs/api.md, docs/users.md 기준.
 */
import { ApiError, request } from '@/api/client';
import { clearSession, getSession, saveSession, updateSessionUser } from '@/api/session';
import type {
  AuthUser,
  ChatResponse,
  FeedSettings,
  LoginResponse,
  PaperDetail,
  Patient,
  PatientInput,
  RecommendationResponse,
  UserProfileContext,
} from '@/types';

export { ApiError } from '@/api/client';
export { API_URL } from '@/api/config';

/** 지금 로그인한 사용자 id. 로그인 화면을 지나야 탭 화면이 열리므로 보통은 항상 있다 */
export function currentUserId(): string {
  const session = getSession();
  if (!session) throw new ApiError('로그인이 필요해요. 다시 로그인해 주세요.', 401);
  return session.user.id;
}

/** POST /auth/login — 이메일·비밀번호로 로그인하고 세션을 저장한다. 틀리면 401 */
export async function login(email: string, password: string): Promise<AuthUser> {
  const res = await request<LoginResponse>('/auth/login', {
    method: 'POST',
    body: { email, password },
    auth: false,
  });
  return saveSession(res).user;
}

/** 회원가입 비밀번호 길이. ★ 백엔드 가입 API가 아직 없어 한슬 님과 확정 필요 (로그인 API는 최대 128자) */
export const PASSWORD_MIN_LENGTH = 8;
export const PASSWORD_MAX_LENGTH = 128;

export type SignupInput = { name: string; email: string; password: string; phone_number: string | null };

/**
 * 계정 만들기 — POST /auth/signup (본문 SignupInput), 성공하면 로그인 응답(LoginOut)을 받아 바로 로그인한다.
 * ★ 백엔드에 아직 없는 API다 (2026-11-07, 한슬 님께 요청 예정). DB에는 user_functions.create_user()가 있다.
 * 이메일 중복은 409로 제안했다. 만들어지기 전에는 "서버에 아직 이 기능이 없어요" 오류가 난다.
 */
export async function signup(data: SignupInput): Promise<AuthUser> {
  const res = await request<LoginResponse>('/auth/signup', { method: 'POST', body: data, auth: false });
  return saveSession(res).user;
}

/** GET /auth/me — 저장된 토큰이 아직 유효한지 확인하고 사용자 정보를 갱신한다 */
export async function getMe(): Promise<AuthUser> {
  const user = await request<AuthUser>('/auth/me');
  updateSessionUser(user);
  return user;
}

/**
 * POST /auth/logout — 서버 세션을 폐기하고 기기의 토큰을 지운다.
 * 서버 요청이 실패해도(만료 401, 연결 안 됨) 기기에서는 로그아웃한다 (AUTH_CHECK.md: 401이면 로컬 토큰만 지우면 된다).
 */
export async function logout(): Promise<void> {
  try {
    await request('/auth/logout', { method: 'POST', timeoutMs: 10_000 });
  } catch {
    // 무시: 아래에서 기기 세션은 지운다
  } finally {
    clearSession();
  }
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

/**
 * 챗봇에게 질문 — POST /chat/{user_id}, 본문 {message, patient_id?}.
 * 응답은 answer + sources(인용 순서). 스트리밍 없이 한 번에 오고 최대 90초 걸릴 수 있다 (docs/api.md).
 * 서버가 같은 사용자·같은 환자 범위의 최근 대화 5턴과 하루 요약을 읽어 이어서 답한다 (7일 보관).
 */
export function sendChatMessage(
  userId: string,
  message: string,
  patientId: string | null = null,
): Promise<ChatResponse> {
  return request(`/chat/${userId}`, {
    method: 'POST',
    body: { message, patient_id: patientId },
    // LLM + 검색 도구를 여러 번 부를 수 있어 오래 걸린다
    timeoutMs: 90_000,
  });
}

/** 이름(닉네임) 최대 길이. 서버(UserUpdate)는 길이 제한이 없어 화면에서만 막는다 */
export const NAME_MAX_LENGTH = 30;

/**
 * 이름(닉네임) 수정 — PATCH /users/{user_id}, 본문 {name} (user_schemas.UserUpdate, 보낸 칸만 바뀐다).
 * ★ 백엔드에 아직 없는 API다 (2026-11-07, 한슬 님께 요청 예정). DB에는 user_functions.update_user()가 있다.
 * 성공하면 로그인 세션의 이름도 바꾼다.
 */
export async function updateUserName(userId: string, name: string): Promise<{ id: string; name: string }> {
  const saved = await request<{ id: string; name: string }>(`/users/${userId}`, { method: 'PATCH', body: { name } });
  const session = getSession();
  if (session) updateSessionUser({ ...session.user, name: saved.name });
  return saved;
}

/** PATCH /users/{user_id}/feed-settings — 피드 갱신 주기(1~7일) 저장 */
export function updateFeedSettings(userId: string, feedRefreshDays: number): Promise<FeedSettings> {
  return request(`/users/${userId}/feed-settings`, {
    method: 'PATCH',
    body: { feed_refresh_days: feedRefreshDays },
  });
}
