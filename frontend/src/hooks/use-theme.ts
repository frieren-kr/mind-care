/**
 * Learn more about light and dark modes:
 * https://docs.expo.dev/guides/color-schemes/
 */

import { Colors } from '@/constants/theme';
import { useResolvedColorScheme } from '@/hooks/use-theme-mode';

/** 현재 색 구성. 내 정보 > 화면 모드 설정(휴대폰 설정 따르기 / 밝게 / 어둡게)을 따른다 */
export function useTheme() {
  return Colors[useResolvedColorScheme()];
}
