import { router, Stack, useLocalSearchParams } from 'expo-router';
import { useCallback, useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { createMedication, currentUserId, getUserProfile, updateMedication } from '@/api';
import { toApiError } from '@/api/client';
import { ThemedText } from '@/components/themed-text';
import { Button, Chip, ErrorView, LoadingView, Screen, SectionLabel, TextField } from '@/components/ui';
import { Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import { useTheme } from '@/hooks/use-theme';
import type { Medication, MedicationInput } from '@/types';
import { isoDate } from '@/utils/date';

/** 자주 쓰는 복용 시간. 고르면 칸에 채워지고, 직접 고쳐 쓸 수도 있다 */
const FREQUENCY_OPTIONS = ['1일 1회 아침 식후', '1일 1회 취침 전', '1일 2회 아침·저녁 식후', '1일 3회 식후'];

function MedicationForm({ patientId, initial }: { patientId: string; initial?: Medication }) {
  const theme = useTheme();
  const [name, setName] = useState(initial?.drug_name ?? '');
  const [dosage, setDosage] = useState(initial?.dosage ?? '');
  const [frequency, setFrequency] = useState(initial?.frequency ?? '');
  const [note, setNote] = useState(initial?.note ?? '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (data: MedicationInput) => {
    setSaving(true);
    setError(null);
    try {
      const userId = currentUserId();
      if (initial) await updateMedication(userId, patientId, initial.id, data);
      else await createMedication(userId, patientId, data);
      router.back();
    } catch (e) {
      setError(toApiError(e).message);
      setSaving(false);
    }
  };

  const save = () => {
    if (!name.trim()) {
      setError('약 이름을 입력해 주세요.');
      return;
    }
    run({
      drug_name: name.trim(),
      dosage: dosage.trim() || null,
      frequency: frequency.trim() || null,
      note: note.trim() || null,
    });
  };

  // 기록은 지우지 않고 '복용 중단'으로 남긴다 (복약 이력 — 챗봇이 약 변화를 볼 수 있게)
  const stop = () => run({ is_taking: false, end_date: isoDate() });

  return (
    <Screen>
      <View style={styles.field}>
        <SectionLabel>약 이름</SectionLabel>
        <TextField value={name} onChangeText={setName} placeholder="예: 도네페질 (아리셉트)" maxLength={100} />
      </View>
      <View style={styles.field}>
        <SectionLabel>1회 복용량 (선택)</SectionLabel>
        <TextField value={dosage} onChangeText={setDosage} placeholder="예: 5mg 1정" maxLength={50} />
      </View>
      <View style={styles.field}>
        <SectionLabel>언제 드시나요? (선택)</SectionLabel>
        <View style={styles.wrap}>
          {FREQUENCY_OPTIONS.map((f) => (
            <Chip key={f} label={f} selected={frequency === f} onPress={() => setFrequency(frequency === f ? '' : f)} />
          ))}
        </View>
        <TextField value={frequency} onChangeText={setFrequency} placeholder="직접 입력" accessibilityLabel="복용 시간" maxLength={100} />
      </View>
      <View style={styles.field}>
        <SectionLabel>메모 (선택)</SectionLabel>
        <TextField value={note} onChangeText={setNote} placeholder="예: 처방 병원, 부작용" maxLength={300} />
      </View>
      {error ? (
        <ThemedText accessibilityRole="alert" style={{ color: theme.accent }}>
          {error}
        </ThemedText>
      ) : null}
      <View style={styles.field}>
        <Button label={saving ? '저장하고 있어요…' : '저장하기'} onPress={save} disabled={saving} />
        {initial ? <Button label="이제 안 드세요 (복용 중단)" variant="secondary" onPress={stop} disabled={saving} /> : null}
        <Button label="취소" variant="secondary" onPress={() => router.back()} disabled={saving} />
      </View>
    </Screen>
  );
}

/** 복용 약 추가(id 없음) / 수정·복용 중단(id 있음). patientId는 필수 */
export default function MedicationFormScreen() {
  const { patientId, id } = useLocalSearchParams<{ patientId: string; id?: string }>();
  const loadProfile = useCallback(() => getUserProfile(currentUserId()), []);
  const profile = useApi(loadProfile);
  const context = profile.data?.patients.find(({ patient }) => patient.id === patientId);
  const existing = id ? context?.current_medications.find((m) => m.id === id) : undefined;
  const who = context?.patient.name;
  const title = id ? '약 정보 수정' : '약 추가';

  if (!patientId || (id && !existing)) {
    return (
      <Screen>
        <Stack.Screen options={{ title }} />
        {profile.error ? <ErrorView message={profile.error.message} onRetry={() => profile.reload()} /> : null}
        {profile.loading ? <LoadingView /> : null}
        {profile.data || !patientId ? (
          <ThemedText themeColor="textSecondary">이 약 정보를 찾을 수 없어요.</ThemedText>
        ) : null}
      </Screen>
    );
  }

  return (
    <>
      <Stack.Screen options={{ title: who ? `${who} · ${title}` : title }} />
      <MedicationForm key={existing?.id ?? 'new'} patientId={patientId} initial={existing} />
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
});
