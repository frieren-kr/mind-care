import { API_URL } from '@/api/config';

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
  if (response.status === 404) return '요청한 정보를 찾을 수 없어요.';
  if (response.status === 422) return '요청 형식이 올바르지 않아요.';
  if (response.status >= 500) return '서버에 문제가 생겼어요. 잠시 후 다시 시도해 주세요.';
  return `요청에 실패했어요. (${response.status})`;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', query, body, timeoutMs = 30_000 } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let response: Response;
  try {
    response = await fetch(buildUrl(path, query), {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
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
    throw new ApiError(await errorMessage(response), response.status);
  }
  return (await response.json()) as T;
}
