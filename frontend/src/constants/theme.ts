/**
 * 마인드 케어 디자인 토큰.
 * 고령 사용자 기준: 본문 18pt 이상, 텍스트 대비 4.5:1 이상, 터치 영역 48dp 이상.
 * Figma 디자인 시스템이 확정되면 이 파일의 값만 바꾸면 된다.
 */

import '@/global.css';

import { Platform } from 'react-native';

import type { EvidenceLevel } from '@/types';

/**
 * "신문 지면" 테마 (11/7, 시안 A안). 옅은 미색 바탕 + 먹색 글자, 카드 대신 선으로 나눈다.
 * 강조색(버튼·선택)은 남색 대신 먹색 — 남색 배경은 "AI 앱" 느낌이 나서 뺐다.
 * 포인트는 적갈색(accent) 한 가지: 섹션 머리말, "우리 상황에서", 요약 번호.
 */
export const Colors = {
  light: {
    text: '#1E1C18',
    // 11/8 확정: 종이색 #F5F1E8과 흰색 #FFFFFF의 중간 (종이색은 너무 누렇고, 흰색은 차가워서)
    background: '#FAF8F4',
    /** 입력칸·등급 설명처럼 바탕보다 살짝 떠 보여야 하는 면 (바탕이 옅어져서 흰색으로) */
    backgroundElement: '#FFFFFF',
    backgroundSelected: '#EDE6D6',
    textSecondary: '#5E5A52',
    primary: '#1E1C18',
    onPrimary: '#FFFFFF',
    /** 얇은 구분선 */
    border: '#D9D2C3',
    /** 굵은 구분선 (머리말 이중선, 탭바 위, 섹션 시작) */
    rule: '#1E1C18',
    /** "우리 상황에서" 블록 바탕 */
    highlight: '#EDE6D6',
    accent: '#A4471C',
  },
  dark: {
    text: '#EDE8DE',
    background: '#171614',
    backgroundElement: '#211F1C',
    backgroundSelected: '#2C2822',
    textSecondary: '#B5AE9F',
    primary: '#EDE8DE',
    onPrimary: '#171614',
    border: '#3A362F',
    rule: '#EDE8DE',
    highlight: '#2C2822',
    accent: '#E39A6E',
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
  /** 명조 기사 제목. 명조는 같은 크기에서 고딕보다 작아 보여 한 단계 키운다 */
  headline: 23,
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

/**
 * 모서리. "AI 앱처럼 전부 둥글지 않게" (10/26 주현 님 의견): 카드·버튼·입력창은 거의 각지게 두고,
 * 포인트(근거 배지·선택 칩)와 챗봇 말풍선만 둥글게 한다.
 */
export const Radius = {
  card: 2,
  bubble: 18,
  pill: 999,
} as const;

/**
 * 앱 글꼴 Pretendard (assets/fonts, SIL OFL 1.1). 굵기마다 파일이 달라서 fontWeight 대신 이름으로 고른다.
 * serif = 나눔명조 Bold (SIL OFL 1.1) — 논문 제목·머리말 같은 '기사 제목'에만 쓴다. 본문은 Pretendard.
 */
export const FontFamily = {
  regular: 'Pretendard-Regular',
  semibold: 'Pretendard-SemiBold',
  bold: 'Pretendard-Bold',
  serif: 'NanumMyeongjo-Bold',
} as const;

export const FontAssets = {
  [FontFamily.regular]: require('@/assets/fonts/Pretendard-Regular.otf'),
  [FontFamily.semibold]: require('@/assets/fonts/Pretendard-SemiBold.otf'),
  [FontFamily.bold]: require('@/assets/fonts/Pretendard-Bold.otf'),
  [FontFamily.serif]: require('@/assets/fonts/NanumMyeongjo-Bold.ttf'),
};

/** 버튼·탭 등 터치 영역 최소 높이 */
export const MinTouch = 48;

export const MaxContentWidth = 800;
