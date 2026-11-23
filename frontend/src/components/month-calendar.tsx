import { useState } from 'react';
import { Pressable, StyleSheet, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Chip } from '@/components/ui';
import { MinTouch, Radius, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import { isoDate } from '@/utils/date';

const WEEKDAYS = ['일', '월', '화', '수', '목', '금', '토'];

/** 그 달 1일 (폰 시간 기준) */
export function monthStart(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

export function addMonths(date: Date, n: number): Date {
  return new Date(date.getFullYear(), date.getMonth() + n, 1);
}

/**
 * 한 달 달력. 기록이 있는 날은 아래 점, 오늘은 굵게 + 밑줄, 고른 날은 먹색 네모.
 * 고를 수 없는 날(미래 등)은 흐리게. 일요일 숫자는 적갈색 (신문 달력처럼)
 * 위의 '2026년 11월 ▾'를 누르면 연도·월을 바로 고르는 칸이 펼쳐진다 (‹ ›는 한 달씩)
 */
export function MonthCalendar({
  month,
  selected,
  marked,
  onSelect,
  onChangeMonth,
  canSelect,
  minMonth,
  maxMonth,
}: {
  /** 보여 줄 달의 1일 */
  month: Date;
  /** 고른 날 'YYYY-MM-DD' */
  selected: string;
  /** 기록이 있는 날 'YYYY-MM-DD' → 건수 */
  marked: Record<string, number>;
  onSelect: (day: string) => void;
  onChangeMonth: (month: Date) => void;
  canSelect: (day: string) => boolean;
  /** 넘겨 볼 수 있는 가장 이른 달·늦은 달 (각 1일) */
  minMonth: Date;
  maxMonth: Date;
}) {
  const theme = useTheme();
  const [picking, setPicking] = useState(false);
  const canGoPrev = month > minMonth;
  const canGoNext = month < maxMonth;
  const inRange = (d: Date) => d >= minMonth && d <= maxMonth;
  const years = Array.from(
    { length: maxMonth.getFullYear() - minMonth.getFullYear() + 1 },
    (_, i) => minMonth.getFullYear() + i,
  );
  // 연도를 바꾸면 같은 월로, 그 달이 범위 밖이면 범위 안의 가장 가까운 달로
  const pickYear = (y: number) => {
    const next = new Date(y, month.getMonth(), 1);
    onChangeMonth(next < minMonth ? minMonth : next > maxMonth ? maxMonth : next);
  };
  const today = isoDate();
  const year = month.getFullYear();
  const m = month.getMonth();
  const lead = new Date(year, m, 1).getDay(); // 1일 앞의 빈 칸
  const days = new Date(year, m + 1, 0).getDate();
  const cells: (string | null)[] = [
    ...Array.from({ length: lead }, () => null),
    ...Array.from({ length: days }, (_, i) => isoDate(new Date(year, m, i + 1))),
  ];
  while (cells.length % 7) cells.push(null);
  const weeks = Array.from({ length: cells.length / 7 }, (_, w) => cells.slice(w * 7, w * 7 + 7));

  const navButton = (label: string, a11y: string, enabled: boolean, onPress: () => void) => (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={a11y}
      accessibilityState={{ disabled: !enabled }}
      disabled={!enabled}
      onPress={onPress}
      hitSlop={8}
      style={({ pressed }) => [styles.nav, pressed && { backgroundColor: theme.backgroundSelected }, !enabled && styles.dim]}>
      <ThemedText type="subtitle">{label}</ThemedText>
    </Pressable>
  );

  return (
    <View style={styles.root}>
      <View style={styles.header}>
        {navButton('‹', '이전 달', canGoPrev, () => onChangeMonth(addMonths(month, -1)))}
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={`${year}년 ${m + 1}월, 연도와 월 고르기`}
          accessibilityState={{ expanded: picking }}
          onPress={() => setPicking(!picking)}
          hitSlop={8}
          style={({ pressed }) => [styles.title, pressed && { backgroundColor: theme.backgroundSelected }]}>
          <ThemedText type="subtitle">
            {year}년 {m + 1}월
          </ThemedText>
          <ThemedText type="smallBold" aria-hidden>
            {picking ? '▴' : '▾'}
          </ThemedText>
        </Pressable>
        {navButton('›', '다음 달', canGoNext, () => onChangeMonth(addMonths(month, 1)))}
      </View>
      {picking ? (
        <View style={[styles.picker, { borderColor: theme.border }]}>
          <ThemedText type="smallBold" themeColor="textSecondary">
            연도
          </ThemedText>
          <View style={styles.chips}>
            {years.map((y) => (
              <Chip key={y} label={`${y}년`} selected={y === year} onPress={() => pickYear(y)} />
            ))}
          </View>
          <ThemedText type="smallBold" themeColor="textSecondary">
            월
          </ThemedText>
          <View style={styles.monthGrid}>
            {Array.from({ length: 12 }, (_, i) => {
              const target = new Date(year, i, 1);
              const enabled = inRange(target);
              return (
                <Pressable
                  key={i}
                  accessibilityRole="button"
                  accessibilityLabel={`${i + 1}월`}
                  accessibilityState={{ selected: i === m, disabled: !enabled }}
                  disabled={!enabled}
                  onPress={() => {
                    onChangeMonth(target);
                    setPicking(false);
                  }}
                  style={({ pressed }) => [
                    styles.monthCell,
                    { borderColor: theme.border },
                    i === m && { backgroundColor: theme.primary, borderColor: theme.primary },
                    i !== m && pressed && { backgroundColor: theme.backgroundSelected },
                    !enabled && styles.dim,
                  ]}>
                  <ThemedText type={i === m ? 'bold' : 'default'} style={i === m ? { color: theme.onPrimary } : undefined}>
                    {i + 1}월
                  </ThemedText>
                </Pressable>
              );
            })}
          </View>
        </View>
      ) : null}
      <View style={[styles.row, styles.weekHead, { borderBottomColor: theme.rule }]}>
        {WEEKDAYS.map((w, i) => (
          <ThemedText
            key={w}
            type="smallBold"
            style={[styles.weekday, { color: i === 0 ? theme.accent : theme.textSecondary }]}>
            {w}
          </ThemedText>
        ))}
      </View>
      {weeks.map((week, w) => (
        <View key={w} style={styles.row}>
          {week.map((day, i) => {
            if (!day) return <View key={i} style={styles.cell} />;
            const isSelected = day === selected;
            const isToday = day === today;
            const enabled = canSelect(day);
            const count = marked[day] ?? 0;
            const dayNum = Number(day.slice(8));
            return (
              <Pressable
                key={day}
                accessibilityRole="button"
                accessibilityLabel={`${m + 1}월 ${dayNum}일${isToday ? ' 오늘' : ''}${count ? `, 기록 ${count}건` : ''}`}
                accessibilityState={{ selected: isSelected, disabled: !enabled }}
                disabled={!enabled}
                onPress={() => onSelect(day)}
                style={styles.cell}>
                {({ pressed }) => (
                  <View
                    style={[
                      styles.dayBox,
                      isSelected && { backgroundColor: theme.primary },
                      !isSelected && pressed && { backgroundColor: theme.backgroundSelected },
                    ]}>
                    <ThemedText
                      type={isToday || isSelected ? 'bold' : 'default'}
                      style={[
                        isSelected
                          ? { color: theme.onPrimary }
                          : i === 0
                            ? { color: theme.accent }
                            : undefined,
                        isToday && !isSelected && styles.todayText,
                        !enabled && styles.dim,
                      ]}>
                      {dayNum}
                    </ThemedText>
                    {/* 기록이 있는 날 점 */}
                    <View
                      style={[
                        styles.dot,
                        { backgroundColor: count ? (isSelected ? theme.onPrimary : theme.accent) : 'transparent' },
                      ]}
                    />
                  </View>
                )}
              </Pressable>
            );
          })}
        </View>
      ))}
    </View>
  );
}

const styles = StyleSheet.create({
  root: {
    gap: Spacing.one,
  },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
  nav: {
    minWidth: MinTouch,
    minHeight: MinTouch,
    alignItems: 'center',
    justifyContent: 'center',
    borderRadius: Radius.card,
  },
  row: {
    flexDirection: 'row',
  },
  title: {
    minHeight: MinTouch,
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.one,
    paddingHorizontal: Spacing.two,
    borderRadius: Radius.card,
  },
  picker: {
    gap: Spacing.two,
    borderWidth: StyleSheet.hairlineWidth,
    padding: Spacing.three,
    marginBottom: Spacing.two,
  },
  chips: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: Spacing.two,
  },
  monthGrid: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: Spacing.two,
  },
  // 한 줄에 4개 (3줄)
  monthCell: {
    width: '22%',
    flexGrow: 1,
    minHeight: MinTouch,
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: 1,
    borderRadius: Radius.card,
  },
  weekHead: {
    borderBottomWidth: 1,
    paddingBottom: Spacing.one,
    marginBottom: Spacing.one,
  },
  weekday: {
    flex: 1,
    textAlign: 'center',
  },
  cell: {
    flex: 1,
    aspectRatio: 1,
    maxHeight: 56,
    padding: 2,
  },
  dayBox: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    borderRadius: Radius.card,
  },
  todayText: {
    textDecorationLine: 'underline',
  },
  dot: {
    width: 5,
    height: 5,
    borderRadius: Radius.pill,
    marginTop: 1,
  },
  dim: {
    opacity: 0.3,
  },
});
