import {
  ActivityIndicator,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
  type TextInputProps,
  type ViewProps,
} from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Evidence, FontFamily, FontSize, MaxContentWidth, MinTouch, Radius, Spacing } from '@/constants/theme';
import { useTextScale } from '@/hooks/use-text-scale';
import { useTheme } from '@/hooks/use-theme';
import type { EvidenceLevel } from '@/types';

/**
 * 글자 입력칸. 글자 크기 설정을 따른다.
 * 안드로이드는 입력칸의 안내 문구(placeholder)를 앱 글꼴이 아니라 폰 시스템 글꼴로 그려서,
 * 안내 문구는 입력칸 위에 직접 겹쳐 그린다 (값이 비었을 때만).
 */
export function TextField({ style, placeholder, value, accessibilityLabel, ...rest }: TextInputProps) {
  const theme = useTheme();
  const { multiplier } = useTextScale();
  const fontSize = Math.round(FontSize.body * multiplier);
  // 바깥 배치(flex·width)는 감싸는 View에, 나머지는 입력칸에
  const { flex, width, ...inputStyle } = StyleSheet.flatten(style) ?? {};
  return (
    <View style={{ flex, width }}>
      <TextInput
        value={value}
        accessibilityLabel={accessibilityLabel ?? placeholder}
        style={[
          styles.input,
          { color: theme.text, backgroundColor: theme.backgroundElement, borderColor: theme.border, fontSize },
          inputStyle,
        ]}
        {...rest}
      />
      {!value && placeholder ? (
        <View
          style={[
            StyleSheet.absoluteFill,
            styles.placeholder,
            { alignItems: inputStyle.textAlign === 'center' ? 'center' : 'flex-start' },
          ]}>
          <Text numberOfLines={1} style={{ color: theme.textSecondary, fontFamily: FontFamily.regular, fontSize }}>
            {placeholder}
          </Text>
        </View>
      ) : null}
    </View>
  );
}

/** 화면 공통 스크롤 컨테이너. onRefresh를 주면 당겨서 새로고침이 켜진다 */
export function Screen({
  children,
  refreshing = false,
  onRefresh,
}: {
  children: React.ReactNode;
  refreshing?: boolean;
  onRefresh?: () => void;
}) {
  const theme = useTheme();
  return (
    <ScrollView
      style={{ backgroundColor: theme.background }}
      contentContainerStyle={styles.screenContent}
      refreshControl={
        onRefresh ? (
          <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={theme.primary} />
        ) : undefined
      }>
      {children}
    </ScrollView>
  );
}

/** 불러오는 중. hint는 오래 걸릴 때 기다려 달라는 안내 */
export function LoadingView({ message = '불러오는 중이에요…', hint }: { message?: string; hint?: string }) {
  const theme = useTheme();
  return (
    <View style={styles.status} accessibilityRole="progressbar" accessibilityLabel={message}>
      <ActivityIndicator size="large" color={theme.primary} />
      <ThemedText type="bold">{message}</ThemedText>
      {hint ? (
        <ThemedText type="small" themeColor="textSecondary" style={styles.hint}>
          {hint}
        </ThemedText>
      ) : null}
    </View>
  );
}

/** 오류 안내 + 다시 시도 */
export function ErrorView({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <Card>
      <ThemedText type="subtitle">불러오지 못했어요</ThemedText>
      <ThemedText themeColor="textSecondary">{message}</ThemedText>
      {onRetry ? <Button label="다시 시도" variant="secondary" onPress={onRetry} /> : null}
    </Card>
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

/** 섹션 제목. 이모지 대신 작은 강조색 글자로 구역을 나눈다 */
export function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <ThemedText type="smallBold" themeColor="primary">
      {children}
    </ThemedText>
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
  input: {
    minHeight: MinTouch,
    borderWidth: 1,
    borderRadius: Radius.card,
    paddingHorizontal: Spacing.three,
    fontFamily: FontFamily.regular,
  },
  placeholder: {
    justifyContent: 'center',
    paddingHorizontal: Spacing.three,
    pointerEvents: 'none',
  },
  status: {
    alignItems: 'center',
    gap: Spacing.three,
    paddingVertical: Spacing.five,
  },
  hint: {
    textAlign: 'center',
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
