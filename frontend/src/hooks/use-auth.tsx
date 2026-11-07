import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';

import { getMe } from '@/api';
import { getSession, restoreSession, subscribeSession, type Session } from '@/api/session';

type AuthContextValue = {
  session: Session | null;
  /** 저장된 세션을 다 읽었는지 (읽기 전에 화면을 그리면 로그인 화면이 잠깐 보인다) */
  ready: boolean;
};

const AuthContext = createContext<AuthContextValue>({ session: null, ready: false });

/**
 * 로그인 상태. 세션 자체는 api/session.ts가 들고 있고, 여기서는 화면에 알려 주기만 한다.
 * 401이 오면 api/client.ts가 세션을 지우고 → session이 null이 되어 _layout이 로그인 화면을 연다.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(getSession);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const unsubscribe = subscribeSession(setSession);
    let active = true;
    restoreSession().then((saved) => {
      if (!active) return;
      setReady(true);
      // 서버에서 로그아웃됐는지 확인한다. 401이면 client가 세션을 지운다.
      // 서버에 못 닿으면(오프라인 등) 저장된 세션으로 열고 각 화면이 오류를 보여 준다
      if (saved) getMe().catch(() => {});
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  return <AuthContext.Provider value={{ session, ready }}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  return useContext(AuthContext);
}
