import { router, Stack, useLocalSearchParams } from 'expo-router';
import { useCallback, useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { createPatient, currentUserId, getUserProfile, updatePatient } from '@/api';
import { toApiError } from '@/api/client';
import { ThemedText } from '@/components/themed-text';
import { Button, Chip, ErrorView, LoadingView, Screen, SectionLabel, TextField } from '@/components/ui';
import { DEMENTIA_STAGES, INTEREST_OPTIONS, SYMPTOM_OPTIONS } from '@/constants/patient-options';
import { Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import { useTheme } from '@/hooks/use-theme';
import type { DementiaStage, Patient, PatientInput } from '@/types';

/** 오늘 날짜 'YYYY-MM-DD' (폰 시간 기준) */
function today(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** 연·월·일 칸을 'YYYY-MM-DD'로. 셋 다 비면 null(날짜 모름). 잘못된 날짜면 오류 문구 */
function parseDiagnosisDate(year: string, month: string, day: string): { value: string | null } | { error: string } {
  if (!year.trim() && !month.trim() && !day.trim()) return { value: null };
  const [y, m, d] = [year, month, day].map((v) => Number(v.trim()));
  const valid =
    Number.isInteger(y) && Number.isInteger(m) && Number.isInteger(d) && y >= 1900 && m >= 1 && m <= 12 && d >= 1;
  const date = new Date(y, m - 1, d);
  if (!valid || date.getMonth() !== m - 1 || date.getDate() !== d) {
    return { error: '진단일을 올바르게 입력해 주세요. (예: 2025년 3월 14일)' };
  }
  const value = `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
  if (value > today()) return { error: '진단일은 오늘 이후 날짜일 수 없어요.' };
  return { value };
}

/** 여러 개 고르는 칩 + 목록에 없는 항목 직접 추가 */
function MultiSelect({
  options,
  selected,
  onChange,
  placeholder,
}: {
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
  placeholder: string;
}) {
  const [custom, setCustom] = useState('');
  // 직접 입력한 항목도 칩으로 보여 준다
  const all = [...options, ...selected.filter((s) => !options.includes(s))];
  const toggle = (item: string) =>
    onChange(selected.includes(item) ? selected.filter((s) => s !== item) : [...selected, item]);
  const addCustom = () => {
    const item = custom.trim();
    if (item && !selected.includes(item)) onChange([...selected, item]);
    setCustom('');
  };

  return (
    <View style={styles.field}>
      <View style={styles.wrap}>
        {all.map((item) => (
          <Chip key={item} label={item} selected={selected.includes(item)} onPress={() => toggle(item)} />
        ))}
      </View>
      <View style={styles.inline}>
        <TextField
          style={styles.flex}
          value={custom}
          onChangeText={setCustom}
          placeholder={placeholder}
          onSubmitEditing={addCustom}
          returnKeyType="done"
        />
        <View>
          <Button label="추가" variant="secondary" onPress={addCustom} disabled={!custom.trim()} />
        </View>
      </View>
    </View>
  );
}

function PatientForm({ initial }: { initial?: Patient }) {
  const theme = useTheme();
  const [name, setName] = useState(initial?.name ?? '');
  const [stage, setStage] = useState<DementiaStage | null>(initial?.dementia_stage ?? null);
  const [initialYear, initialMonth, initialDay] = (initial?.diagnosis_date ?? '').split('-');
  const [year, setYear] = useState(initialYear ?? '');
  const [month, setMonth] = useState(initialMonth ? String(Number(initialMonth)) : '');
  const [day, setDay] = useState(initialDay ? String(Number(initialDay)) : '');
  const [symptoms, setSymptoms] = useState<string[]>(initial?.symptoms ?? []);
  const [interests, setInterests] = useState<string[]>(initial?.interests ?? []);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    const date = parseDiagnosisDate(year, month, day);
    if ('error' in date) {
      setError(date.error);
      return;
    }
    const data: PatientInput = {
      name: name.trim() || null,
      dementia_stage: stage,
      diagnosis_date: date.value,
      symptoms,
      interests,
    };
    setSaving(true);
    setError(null);
    try {
      const userId = currentUserId();
      if (initial) await updatePatient(userId, initial.id, data);
      else await createPatient(userId, data);
      router.back();
    } catch (e) {
      setError(toApiError(e).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Screen>
      <View style={styles.field}>
        <SectionLabel>부르는 이름</SectionLabel>
        <TextField value={name} onChangeText={setName} placeholder="예: 어머니" maxLength={30} />
        <ThemedText type="small" themeColor="textSecondary">
          실명 대신 &apos;어머니&apos;처럼 부르는 호칭을 써도 돼요.
        </ThemedText>
      </View>

      <View style={styles.field}>
        <SectionLabel>치매 단계</SectionLabel>
        <View style={styles.wrap}>
          {DEMENTIA_STAGES.map((s) => (
            <Chip key={s} label={s} selected={stage === s} onPress={() => setStage(s)} />
          ))}
          <Chip label="모름" selected={stage === null} onPress={() => setStage(null)} />
        </View>
      </View>

      <View style={styles.field}>
        <SectionLabel>진단일 (모르면 비워 두세요)</SectionLabel>
        <View style={styles.inline}>
          <TextField style={styles.year} value={year} onChangeText={setYear} placeholder="2025" keyboardType="number-pad" maxLength={4} />
          <ThemedText>년</ThemedText>
          <TextField style={styles.monthDay} value={month} onChangeText={setMonth} placeholder="3" keyboardType="number-pad" maxLength={2} />
          <ThemedText>월</ThemedText>
          <TextField style={styles.monthDay} value={day} onChangeText={setDay} placeholder="14" keyboardType="number-pad" maxLength={2} />
          <ThemedText>일</ThemedText>
        </View>
      </View>

      <View style={styles.field}>
        <SectionLabel>주요 증상</SectionLabel>
        <ThemedText type="small" themeColor="textSecondary">
          해당하는 것을 모두 골라 주세요. 맞춤 연구를 고를 때 쓰여요.
        </ThemedText>
        <MultiSelect options={SYMPTOM_OPTIONS} selected={symptoms} onChange={setSymptoms} placeholder="목록에 없는 증상 입력" />
      </View>

      <View style={styles.field}>
        <SectionLabel>관심 분야</SectionLabel>
        <MultiSelect options={INTEREST_OPTIONS} selected={interests} onChange={setInterests} placeholder="목록에 없는 관심 분야 입력" />
      </View>

      {error ? (
        <ThemedText style={[styles.error, { borderLeftColor: theme.accent }]}>{error}</ThemedText>
      ) : null}
      <View style={styles.field}>
        <Button label={saving ? '저장하고 있어요…' : '저장하기'} onPress={save} disabled={saving} />
        <Button label="취소" variant="secondary" onPress={() => router.back()} disabled={saving} />
      </View>
    </Screen>
  );
}

/** 돌보는 분 추가(id 없음) / 정보 수정(id 있음) */
export default function PatientFormScreen() {
  const { id } = useLocalSearchParams<{ id?: string }>();
  const loadProfile = useCallback(() => getUserProfile(currentUserId()), []);
  // 수정할 때 기존 정보를 채우려고 불러온다 (추가는 기다리지 않고 빈 칸으로 시작)
  const profile = useApi(loadProfile);
  const existing = id ? profile.data?.patients.find(({ patient }) => patient.id === id)?.patient : undefined;
  const title = id ? '정보 수정' : '돌보는 분 추가';

  if (id && !existing) {
    return (
      <Screen>
        <Stack.Screen options={{ title }} />
        {profile.error ? <ErrorView message={profile.error.message} onRetry={() => profile.reload()} /> : null}
        {profile.loading ? <LoadingView /> : null}
        {profile.data ? <ThemedText themeColor="textSecondary">이 분의 정보를 찾을 수 없어요.</ThemedText> : null}
      </Screen>
    );
  }

  return (
    <>
      <Stack.Screen options={{ title }} />
      {/* key: 다른 분을 수정하러 들어오면 입력칸을 새로 채운다 */}
      <PatientForm key={existing?.id ?? 'new'} initial={existing} />
    </>
  );
}

const styles = StyleSheet.create({
  field: {
    gap: Spacing.two,
  },
  wrap: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: Spacing.two,
  },
  inline: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
  },
  flex: {
    flex: 1,
  },
  year: {
    width: 88,
    textAlign: 'center',
  },
  monthDay: {
    width: 60,
    textAlign: 'center',
  },
  error: {
    borderLeftWidth: 3,
    paddingLeft: Spacing.two,
  },
});
