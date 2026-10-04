/**
 * 마인드 케어 디자인 토큰.
 * 고령 사용자 기준: 본문 18pt 이상, 텍스트 대비 4.5:1 이상, 터치 영역 48dp 이상.
 * Figma 디자인 시스템이 확정되면 이 파일의 값만 바꾸면 된다.
 */

import '@/global.css';

import { Platform } from 'react-native';

import type { EvidenceLevel } from '@/types';

export const Colors = {
  light: {
    text: '#1A1C1E',
    background: '#F7F8FA',
    backgroundElement: '#FFFFFF',
    backgroundSelected: '#E3ECF7',
    textSecondary: '#4F5560',
    primary: '#1F5FAD',
    onPrimary: '#FFFFFF',
    border: '#D8DCE2',
    highlight: '#FFF6DB',
  },
  dark: {
    text: '#F1F3F5',
    background: '#121416',
    backgroundElement: '#1E2124',
    backgroundSelected: '#22344A',
    textSecondary: '#B4BAC2',
    primary: '#7DB2F0',
    onPrimary: '#0B1F36',
    border: '#353A40',
    highlight: '#3A3220',
  },
} as const;

export type ThemeColor = keyof typeof Colors.light & keyof typeof Colors.dark;

/** 근거 수준 배지. 색만으로 구분하지 않도록 라벨을 항상 함께 표시한다. */
export const Evidence: Record<EvidenceLevel, { label: string; color: string; background: string }> =
  {
    A: { label: '근거 강함', color: '#1B6B3A', background: '#DDF3E4' },
    B: { label: '근거 보통', color: '#7A5A00', background: '#FCEFC7' },
    C: { label: '연관성만 확인', color: '#9A4A00', background: '#FDE3CC' },
    D: { label: '초기 단계 연구', color: '#A3262A', background: '#FBDCDC' },
    guideline: { label: '공식 지침', color: '#1F4F8F', background: '#DDE9F8' },
  };

export const FontSize = {
  small: 15,
  body: 18,
  subtitle: 21,
  title: 26,
} as const;

export const Fonts = Platform.select({
  ios: {
    /** iOS `UIFontDescriptorSystemDesignDefault` */
    sans: 'system-ui',
    /** iOS `UIFontDescriptorSystemDesignSerif` */
    serif: 'ui-serif',
    /** iOS `UIFontDescriptorSystemDesignRounded` */
    rounded: 'ui-rounded',
    /** iOS `UIFontDescriptorSystemDesignMonospaced` */
    mono: 'ui-monospace',
  },
  default: {
    sans: 'normal',
    serif: 'serif',
    rounded: 'normal',
    mono: 'monospace',
  },
  web: {
    sans: 'var(--font-display)',
    serif: 'var(--font-serif)',
    rounded: 'var(--font-rounded)',
    mono: 'var(--font-mono)',
  },
});

export const Spacing = {
  half: 2,
  one: 4,
  two: 8,
  three: 16,
  four: 24,
  five: 32,
  six: 64,
} as const;

export const Radius = {
  card: 16,
  pill: 999,
} as const;

/** 버튼·탭 등 터치 영역 최소 높이 */
export const MinTouch = 48;

export const MaxContentWidth = 800;
