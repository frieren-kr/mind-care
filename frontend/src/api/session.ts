import AsyncStorage from '@react-native-async-storage/async-storage';

import type { AuthUser, LoginResponse } from '@/types';

/**
 * 로그인 세션(토큰 + 사용자). 화면 밖(api/client.ts)에서도 토큰을 읽어야 해서 모듈 변수로 두고,
 * 화면은 hooks/use-auth.tsx가 subscribe로 변화를 받는다.
 * 토큰은 기본 15분이면 만료되고 갱신 API가 없다 (backend/AUTH_CHECK.md). 만료되면 다시 로그인한다.
 */
export type Session = {
  accessToken: string;
  /** ISO 시각. 지나면 서버가 401을 주므로 앱을 다시 열 때 미리 버린다 */
  expiresAt: string;
  user: AuthUser;
};

const STORAGE_KEY = 'mindcare.session';

let current: Session | null = null;
const listeners = new Set<(session: Session | null) => void>();

export function getSession(): Session | null {
  return current;
}

export function subscribeSession(listener: (session: Session | null) => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function setSession(next: Session | null) {
  current = next;
  listeners.forEach((listener) => listener(next));
}

function isExpired(session: Session): boolean {
  const expiresAt = Date.parse(session.expiresAt);
  return Number.isNaN(expiresAt) || expiresAt <= Date.now();
}

/** 앱을 열 때 저장된 세션을 읽는다. 만료됐거나 형식이 깨졌으면 버린다 */
export async function restoreSession(): Promise<Session | null> {
  try {
    const raw = await AsyncStorage.getItem(STORAGE_KEY);
    const saved = raw ? (JSON.parse(raw) as Session) : null;
    if (saved?.accessToken && saved.user?.id && !isExpired(saved)) {
      setSession(saved);
      return saved;
    }
  } catch {
    // 저장소를 못 읽으면 로그인 화면부터 시작한다
  }
  await AsyncStorage.removeItem(STORAGE_KEY).catch(() => {});
  setSession(null);
  return null;
}

export function saveSession(login: LoginResponse): Session {
  const session: Session = { accessToken: login.access_token, expiresAt: login.expires_at, user: login.user };
  setSession(session);
  AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(session)).catch(() => {});
  return session;
}

/** 내 정보(/auth/me)로 사용자 정보만 바꾼다 (이름 변경 등). 토큰은 그대로 */
export function updateSessionUser(user: AuthUser) {
  if (!current) return;
  const next = { ...current, user };
  setSession(next);
  AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(next)).catch(() => {});
}

export function clearSession() {
  if (current === null) return;
  setSession(null);
  AsyncStorage.removeItem(STORAGE_KEY).catch(() => {});
}
