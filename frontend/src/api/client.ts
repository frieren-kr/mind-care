import { API_URL } from '@/api/config';
import { clearSession, getSession } from '@/api/session';

/** 화면에 그대로 보여줄 수 있는 오류. status가 없으면 서버에 닿지 못한 경우다. */
export class ApiError extends Error {
  status: number | null;

  constructor(message: string, status: number | null = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  return new ApiError('알 수 없는 오류가 났어요. 잠시 후 다시 시도해 주세요.');
}

type RequestOptions = {
  method?: 'GET' | 'POST' | 'PATCH';
  query?: Record<string, string | number | boolean | null | undefined>;
  body?: unknown;
  /** 첫 추천 요청은 임베딩 모델을 불러오느라 오래 걸려서 넉넉하게 둔다 */
  timeoutMs?: number;
  /** false면 토큰을 보내지 않고, 401이 와도 로그아웃하지 않는다 (로그인 요청: 401 = 비밀번호 틀림) */
  auth?: boolean;
};

function buildUrl(path: string, query: RequestOptions['query']): string {
  const params = Object.entries(query ?? {})
    .filter(([, value]) => value !== null && value !== undefined)
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
  return `${API_URL}${path}${params.length ? `?${params.join('&')}` : ''}`;
}

/** FastAPI 오류 본문({detail: string | [...]})에서 보여줄 문장을 꺼낸다 */
async function errorMessage(response: Response): Promise<string> {
  try {
    const body = await response.json();
    // 서버에 아직 없는 주소 (FastAPI 기본 응답) — 백엔드 API가 만들어지기 전
    if (body?.detail === 'Not Found' || body?.detail === 'Method Not Allowed') {
      return '서버에 아직 이 기능이 없어요. 백엔드 API가 준비되면 사용할 수 있어요.';
    }
    if (typeof body?.detail === 'string') return body.detail;
    // 입력 검증 실패(422): 첫 번째 이유를 보여 준다 (백엔드 검증 문구는 한국어)
    const first = Array.isArray(body?.detail) ? body.detail[0]?.msg : null;
    if (typeof first === 'string') return first.replace(/^Value error, /, '');
  } catch {
    // 본문이 JSON이 아니면 상태 코드로 안내한다
  }
  if (response.status === 401) return '로그인이 필요해요. 다시 로그인해 주세요.';
  if (response.status === 403) return '본인의 정보만 볼 수 있어요.';
  if (response.status === 404) return '요청한 정보를 찾을 수 없어요.';
  if (response.status === 409) return '이미 가입된 이메일이나 휴대폰 번호예요.';
  if (response.status === 429) return '요청이 너무 많아요. 잠시 후 다시 시도해 주세요.';
  if (response.status === 422) return '요청 형식이 올바르지 않아요.';
  if (response.status >= 500) return '서버에 문제가 생겼어요. 잠시 후 다시 시도해 주세요.';
  return `요청에 실패했어요. (${response.status})`;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', query, body, timeoutMs = 30_000, auth = true } = options;
  const token = auth ? getSession()?.accessToken : undefined;
  const headers: Record<string, string> = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (token) headers.Authorization = `Bearer ${token}`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let response: Response;
  try {
    response = await fetch(buildUrl(path, query), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
  } catch {
    throw new ApiError(
      controller.signal.aborted
        ? '서버 응답이 너무 늦어요. 잠시 후 다시 시도해 주세요.'
        : `서버에 연결할 수 없어요. 서버가 켜져 있는지, 주소(${API_URL})가 맞는지 확인해 주세요.`,
    );
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    // 토큰 만료·로그아웃된 세션: 저장된 세션을 지우면 _layout이 로그인 화면으로 보낸다.
    // 요청 도중 다시 로그인했으면(토큰이 바뀌었으면) 새 세션은 지우지 않는다
    if (response.status === 401 && token && getSession()?.accessToken === token) clearSession();
    throw new ApiError(await errorMessage(response), response.status);
  }
  return (await response.json()) as T;
}
