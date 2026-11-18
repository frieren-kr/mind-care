import { StyleSheet, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { TextField } from '@/components/ui';
import { Spacing } from '@/constants/theme';

export type DateParts = { year: string; month: string; day: string };

/** 'YYYY-MM-DD' → 입력칸 값 (월·일 앞의 0은 뺀다) */
export function toDateParts(value: string | null | undefined): DateParts {
  const [year = '', month = '', day = ''] = (value ?? '').split('-');
  return { year, month: month ? String(Number(month)) : '', day: day ? String(Number(day)) : '' };
}

/** 연·월·일 숫자 입력칸 세 개 (돌보는 분 진단일과 같은 모양) */
export function DateFields({ value, onChange, label }: { value: DateParts; onChange: (v: DateParts) => void; label: string }) {
  return (
    <View style={styles.row}>
      <TextField
        style={styles.year}
        value={value.year}
        onChangeText={(year) => onChange({ ...value, year })}
        placeholder="2026"
        accessibilityLabel={`${label} 연도`}
        keyboardType="number-pad"
        maxLength={4}
      />
      <ThemedText>년</ThemedText>
      <TextField
        style={styles.monthDay}
        value={value.month}
        onChangeText={(month) => onChange({ ...value, month })}
        placeholder="12"
        accessibilityLabel={`${label} 월`}
        keyboardType="number-pad"
        maxLength={2}
      />
      <ThemedText>월</ThemedText>
      <TextField
        style={styles.monthDay}
        value={value.day}
        onChangeText={(day) => onChange({ ...value, day })}
        placeholder="3"
        accessibilityLabel={`${label} 일`}
        keyboardType="number-pad"
        maxLength={2}
      />
      <ThemedText>일</ThemedText>
    </View>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
  },
  year: {
    width: 88,
    textAlign: 'center',
  },
  monthDay: {
    width: 60,
    textAlign: 'center',
  },
});
