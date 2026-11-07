import AsyncStorage from '@react-native-async-storage/async-storage';
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

import { useColorScheme } from '@/hooks/use-color-scheme';

/**
 * 앱 안 화면 모드 설정 (내 정보 > 화면 모드). 'system'이면 폰 설정을 따른다.
 * 글자 크기 설정(use-text-scale)처럼 이 기기에만 저장한다.
 */
export const THEME_MODES = [
  { key: 'system', label: '휴대폰 설정 따르기' },
  { key: 'light', label: '밝게' },
  { key: 'dark', label: '어둡게' },
] as const;

export type ThemeMode = (typeof THEME_MODES)[number]['key'];

const STORAGE_KEY = 'mindcare.themeMode';

type ThemeModeContextValue = {
  mode: ThemeMode;
  setMode: (mode: ThemeMode) => void;
  /** 저장된 설정을 다 읽었는지 (읽기 전에 그리면 밝은 화면이 잠깐 보였다 바뀐다) */
  ready: boolean;
};

const ThemeModeContext = createContext<ThemeModeContextValue>({ mode: 'system', setMode: () => {}, ready: true });

function isThemeMode(value: unknown): value is ThemeMode {
  return THEME_MODES.some((m) => m.key === value);
}

export function ThemeModeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<ThemeMode>('system');
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let active = true;
    AsyncStorage.getItem(STORAGE_KEY)
      .then((saved) => {
        if (active && isThemeMode(saved)) setModeState(saved);
      })
      // 저장소를 못 읽어도 폰 설정을 따라 앱은 연다
      .catch(() => {})
      .finally(() => active && setReady(true));
    return () => {
      active = false;
    };
  }, []);

  const setMode = useCallback((next: ThemeMode) => {
    setModeState(next);
    AsyncStorage.setItem(STORAGE_KEY, next).catch(() => {});
  }, []);

  const value = useMemo(() => ({ mode, setMode, ready }), [mode, setMode, ready]);
  return <ThemeModeContext.Provider value={value}>{children}</ThemeModeContext.Provider>;
}

export function useThemeMode() {
  return useContext(ThemeModeContext);
}

/** 실제로 쓸 색 구성: 설정이 'system'이면 폰 설정, 아니면 고른 값 */
export function useResolvedColorScheme(): 'light' | 'dark' {
  const { mode } = useThemeMode();
  const system = useColorScheme();
  if (mode !== 'system') return mode;
  return system === 'dark' ? 'dark' : 'light';
}
