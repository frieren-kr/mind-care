import { useState } from 'react';
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
          // 보조 버튼은 바탕 없이 먹색 테두리만 (신문 지면 테마)
          backgroundColor: primary ? theme.primary : pressed ? theme.backgroundSelected : 'transparent',
          borderColor: theme.rule,
          opacity: disabled ? 0.4 : pressed && primary ? 0.8 : 1,
        },
      ]}>
      <ThemedText type="bold" style={{ color: primary ? theme.onPrimary : theme.primary }}>
        {label}
      </ThemedText>
    </Pressable>
  );
}

/** 근거 수준 배지: 등급 글자(A~D, 명조) + 글자 라벨. 색만으로 구분하지 않는다 */
export function EvidenceBadge({ level }: { level: EvidenceLevel }) {
  const e = Evidence[level];
  return (
    <View
      accessibilityLabel={`근거 수준: ${e.label}`}
      style={[styles.badge, { backgroundColor: e.background }]}>
      {level !== 'guideline' ? (
        <ThemedText type="bold" style={[styles.badgeLetter, { color: e.color }]}>
          {level}
        </ThemedText>
      ) : null}
      <ThemedText type="smallBold" style={{ color: e.color }}>
        {e.label}
      </ThemedText>
    </View>
  );
}

/** 근거 등급이 무슨 뜻인지 한 줄로 (논문 상세) */
const EvidenceMeaning: Record<EvidenceLevel, string> = {
  A: '여러 연구 결과를 모아 분석해서 믿을 만한 정도가 가장 높은 단계예요.',
  B: '사람을 무작위로 나눠 비교한 시험처럼, 비교적 믿을 만한 연구예요.',
  C: '관련이 있다는 것까지 확인한 연구예요. 원인이라고 단정하긴 어려워요.',
  D: '사례 보고나 의견처럼 아직 초기 단계의 근거예요.',
  guideline: '학회·기관이 근거를 검토해 만든 공식 지침이에요.',
};

/** 등급 글자를 크게 보여 주는 설명 상자 (논문 상세) */
export function EvidenceExplain({ level }: { level: EvidenceLevel }) {
  const theme = useTheme();
  const e = Evidence[level];
  return (
    <View
      accessible
      accessibilityLabel={`근거 수준 ${e.label}. ${EvidenceMeaning[level]}`}
      style={[styles.explain, { backgroundColor: theme.backgroundElement, borderColor: theme.border }]}>
      <View style={[styles.explainLetter, { backgroundColor: e.background }]}>
        <ThemedText type="headline" style={{ color: e.color }}>
          {level === 'guideline' ? '지침' : level}
        </ThemedText>
      </View>
      <View style={styles.flex}>
        <ThemedText type="bold" style={{ color: e.color }}>
          {e.label}
        </ThemedText>
        <ThemedText type="small" themeColor="textSecondary">
          {EvidenceMeaning[level]}
        </ThemedText>
      </View>
    </View>
  );
}

/** 신문 머리말 아래 이중선 (굵은 선 + 가는 선) */
export function DoubleRule() {
  const theme = useTheme();
  return <View style={[styles.doubleRule, { borderColor: theme.rule }]} />;
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

/** 섹션 머리말. 신문처럼 적갈색 작은 제목 + 오른쪽으로 이어지는 가는 선 */
export function SectionLabel({ children }: { children: React.ReactNode }) {
  const theme = useTheme();
  return (
    <View style={styles.sectionLabel} accessibilityRole="header">
      <ThemedText type="smallBold" themeColor="accent">
        {children}
      </ThemedText>
      <View style={[styles.sectionLine, { backgroundColor: theme.border }]} />
    </View>
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

/** 체크박스 (네모 + 글자). 글자까지 눌러도 바뀐다 */
export function Checkbox({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  const theme = useTheme();
  return (
    <Pressable
      accessibilityRole="checkbox"
      accessibilityState={{ checked, disabled }}
      accessibilityLabel={label}
      disabled={disabled}
      onPress={() => onChange(!checked)}
      style={[styles.checkRow, disabled && { opacity: 0.4 }]}>
      <View
        style={[
          styles.checkBox,
          { borderColor: theme.rule, backgroundColor: checked ? theme.primary : 'transparent' },
        ]}>
        {checked ? (
          <ThemedText type="smallBold" style={{ color: theme.onPrimary }}>
            ✓
          </ThemedText>
        ) : null}
      </View>
      <ThemedText>{label}</ThemedText>
    </Pressable>
  );
}

export type SelectOption<T extends string | number> = { value: T; label: string };

/**
 * 드롭다운 선택. 누르면 아래로 선택지 목록이 펼쳐지고, 고르면 닫힌다.
 * (안드로이드·웹 모두 같은 모양이 나오도록 직접 그린다)
 */
export function Select<T extends string | number>({
  label,
  value,
  options,
  onChange,
  disabled,
}: {
  /** 화면 낭독기용 이름 */
  label: string;
  value: T;
  options: SelectOption<T>[];
  onChange: (value: T) => void;
  disabled?: boolean;
}) {
  const theme = useTheme();
  const [open, setOpen] = useState(false);
  const current = options.find((o) => o.value === value);

  return (
    <View>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`${label}: ${current?.label ?? ''}`}
        accessibilityState={{ expanded: open, disabled }}
        disabled={disabled}
        onPress={() => setOpen(!open)}
        style={[
          styles.selectField,
          { borderColor: open ? theme.rule : theme.border, backgroundColor: theme.backgroundElement },
          disabled && { opacity: 0.4 },
        ]}>
        <ThemedText style={styles.flex}>{current?.label ?? '선택해 주세요'}</ThemedText>
        <ThemedText type="bold" themeColor="textSecondary">
          {open ? '▴' : '▾'}
        </ThemedText>
      </Pressable>
      {open ? (
        <View style={[styles.selectList, { borderColor: theme.rule, backgroundColor: theme.backgroundElement }]}>
          {options.map((o, index) => {
            const selected = o.value === value;
            return (
              <Pressable
                key={String(o.value)}
                accessibilityRole="button"
                accessibilityState={{ selected }}
                onPress={() => {
                  setOpen(false);
                  if (!selected) onChange(o.value);
                }}
                style={({ pressed }) => [
                  styles.selectItem,
                  index > 0 && { borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: theme.border },
                  (pressed || selected) && { backgroundColor: theme.backgroundSelected },
                ]}>
                <ThemedText type={selected ? 'bold' : 'default'} style={styles.flex}>
                  {o.label}
                </ThemedText>
                {selected ? <ThemedText type="bold">✓</ThemedText> : null}
              </Pressable>
            );
          })}
        </View>
      ) : null}
    </View>
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
  // 등급 배지는 각지게 (신문 지면 테마). 선택 칩과 챗봇 말풍선만 둥글다
  badge: {
    flexDirection: 'row',
    alignItems: 'center',
    alignSelf: 'flex-start',
    gap: Spacing.two,
    paddingHorizontal: Spacing.two,
    paddingVertical: Spacing.half,
  },
  badgeLetter: {
    fontFamily: FontFamily.serif,
  },
  explain: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.three,
    padding: Spacing.three,
    borderWidth: StyleSheet.hairlineWidth,
  },
  explainLetter: {
    width: 56,
    height: 56,
    alignItems: 'center',
    justifyContent: 'center',
  },
  flex: {
    flex: 1,
  },
  doubleRule: {
    height: 5,
    borderTopWidth: 2,
    borderBottomWidth: 1,
  },
  checkRow: {
    minHeight: MinTouch,
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
    alignSelf: 'flex-start',
  },
  checkBox: {
    width: 24,
    height: 24,
    borderWidth: 1.5,
    alignItems: 'center',
    justifyContent: 'center',
  },
  selectField: {
    minHeight: MinTouch,
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
    paddingHorizontal: Spacing.three,
    borderWidth: 1,
  },
  selectList: {
    borderWidth: 1,
    borderTopWidth: 0,
  },
  selectItem: {
    minHeight: MinTouch,
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
    paddingHorizontal: Spacing.three,
  },
  sectionLabel: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
  },
  sectionLine: {
    flex: 1,
    height: StyleSheet.hairlineWidth,
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
