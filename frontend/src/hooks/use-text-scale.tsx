import AsyncStorage from '@react-native-async-storage/async-storage';
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

/**
 * 앱 안 글자 크기 설정. ThemedText가 이 배율로 글자 크기와 줄 간격을 키운다.
 * 폰 자체 글자 크기 설정(시스템 배율)에 한 번 더 곱해진다.
 */
export const TEXT_SCALES = [
  { key: 'normal', label: '보통', value: 1 },
  { key: 'large', label: '크게', value: 1.15 },
  { key: 'xlarge', label: '아주 크게', value: 1.3 },
] as const;

export type TextScaleKey = (typeof TEXT_SCALES)[number]['key'];

const STORAGE_KEY = 'mindcare.textScale';
const DEFAULT_SCALE: TextScaleKey = 'normal';

type TextScaleContextValue = {
  scale: TextScaleKey;
  multiplier: number;
  setScale: (scale: TextScaleKey) => void;
  /** 저장된 설정을 다 읽었는지 (읽기 전에 화면을 그리면 글자 크기가 한 번 바뀌어 보인다) */
  ready: boolean;
};

const TextScaleContext = createContext<TextScaleContextValue>({
  scale: DEFAULT_SCALE,
  multiplier: 1,
  setScale: () => {},
  ready: true,
});

function isScaleKey(value: unknown): value is TextScaleKey {
  return TEXT_SCALES.some((s) => s.key === value);
}

export function TextScaleProvider({ children }: { children: ReactNode }) {
  const [scale, setScaleState] = useState<TextScaleKey>(DEFAULT_SCALE);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let active = true;
    AsyncStorage.getItem(STORAGE_KEY)
      .then((saved) => {
        if (active && isScaleKey(saved)) setScaleState(saved);
      })
      // 저장소를 못 읽어도 기본 크기로 앱은 연다
      .catch(() => {})
      .finally(() => active && setReady(true));
    return () => {
      active = false;
    };
  }, []);

  const setScale = useCallback((next: TextScaleKey) => {
    setScaleState(next);
    AsyncStorage.setItem(STORAGE_KEY, next).catch(() => {});
  }, []);

  const value = useMemo(
    () => ({
      scale,
      multiplier: TEXT_SCALES.find((s) => s.key === scale)?.value ?? 1,
      setScale,
      ready,
    }),
    [scale, setScale, ready],
  );

  return <TextScaleContext.Provider value={value}>{children}</TextScaleContext.Provider>;
}

export function useTextScale() {
  return useContext(TextScaleContext);
}
