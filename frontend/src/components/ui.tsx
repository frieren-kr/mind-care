import { Pressable, ScrollView, StyleSheet, View, type ViewProps } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Evidence, MaxContentWidth, MinTouch, Radius, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import type { EvidenceLevel } from '@/types';

/** 화면 공통 스크롤 컨테이너 */
export function Screen({ children }: { children: React.ReactNode }) {
  const theme = useTheme();
  return (
    <ScrollView
      style={{ backgroundColor: theme.background }}
      contentContainerStyle={styles.screenContent}>
      {children}
    </ScrollView>
  );
}

export function Card({ style, ...rest }: ViewProps) {
  const theme = useTheme();
  return (
    <View
      style={[styles.card, { backgroundColor: theme.backgroundElement, borderColor: theme.border }, style]}
      {...rest}
    />
  );
}

type ButtonProps = {
  label: string;
  onPress?: () => void;
  variant?: 'primary' | 'secondary';
  disabled?: boolean;
};

export function Button({ label, onPress, variant = 'primary', disabled }: ButtonProps) {
  const theme = useTheme();
  const primary = variant === 'primary';
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [
        styles.button,
        {
          backgroundColor: primary ? theme.primary : theme.backgroundElement,
          borderColor: theme.primary,
          opacity: disabled ? 0.4 : pressed ? 0.75 : 1,
        },
      ]}>
      <ThemedText type="bold" style={{ color: primary ? theme.onPrimary : theme.primary }}>
        {label}
      </ThemedText>
    </Pressable>
  );
}

/** 근거 수준 배지: 색 + 글자 라벨 */
export function EvidenceBadge({ level }: { level: EvidenceLevel }) {
  const e = Evidence[level];
  return (
    <View
      accessibilityLabel={`근거 수준: ${e.label}`}
      style={[styles.badge, { backgroundColor: e.background }]}>
      <View style={[styles.dot, { backgroundColor: e.color }]} />
      <ThemedText type="smallBold" style={{ color: e.color }}>
        {e.label}
      </ThemedText>
    </View>
  );
}

/** 선택형 칩 (카테고리 필터, 난이도 토글, 자가점검 보기 등) */
export function Chip({
  label,
  selected,
  onPress,
}: {
  label: string;
  selected: boolean;
  onPress: () => void;
}) {
  const theme = useTheme();
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected }}
      onPress={onPress}
      style={[
        styles.chip,
        {
          backgroundColor: selected ? theme.primary : theme.backgroundElement,
          borderColor: selected ? theme.primary : theme.border,
        },
      ]}>
      <ThemedText type="smallBold" style={{ color: selected ? theme.onPrimary : theme.text }}>
        {label}
      </ThemedText>
    </Pressable>
  );
}

/** 의료 면책 고지 */
export function Disclaimer({ text }: { text: string }) {
  return (
    <ThemedText type="small" themeColor="textSecondary" style={styles.disclaimer}>
      {text}
    </ThemedText>
  );
}

const styles = StyleSheet.create({
  screenContent: {
    padding: Spacing.three,
    paddingBottom: Spacing.six,
    gap: Spacing.three,
    width: '100%',
    maxWidth: MaxContentWidth,
    alignSelf: 'center',
  },
  card: {
    borderRadius: Radius.card,
    borderWidth: StyleSheet.hairlineWidth,
    padding: Spacing.three,
    gap: Spacing.two,
  },
  button: {
    minHeight: MinTouch,
    borderRadius: Radius.card,
    borderWidth: 1.5,
    paddingHorizontal: Spacing.three,
    alignItems: 'center',
    justifyContent: 'center',
  },
  badge: {
    flexDirection: 'row',
    alignItems: 'center',
    alignSelf: 'flex-start',
    gap: Spacing.one,
    paddingHorizontal: Spacing.two,
    paddingVertical: Spacing.half,
    borderRadius: Radius.pill,
  },
  dot: {
    width: 8,
    height: 8,
    borderRadius: 4,
  },
  chip: {
    minHeight: 40,
    paddingHorizontal: Spacing.three,
    borderRadius: Radius.pill,
    borderWidth: 1,
    alignItems: 'center',
    justifyContent: 'center',
  },
  disclaimer: {
    textAlign: 'center',
  },
});
