import { router, Stack, useLocalSearchParams } from 'expo-router';
import { useCallback, useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { createVisit, currentUserId, getPendingVisits, getUserProfile, updateVisit } from '@/api';
import { toApiError } from '@/api/client';
import { DateFields, toDateParts } from '@/components/date-fields';
import { ThemedText } from '@/components/themed-text';
import { Button, Checkbox, Chip, ErrorView, LoadingView, Screen, SectionLabel, TextField } from '@/components/ui';
import { Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import { useTheme } from '@/hooks/use-theme';
import type { MedicalVisit, MedicalVisitInput } from '@/types';
import { isoDate, parseDateParts } from '@/utils/date';

const DEPARTMENTS = ['신경과', '정신건강의학과', '신경외과', '가정의학과', '치매안심센터'];

/** 비운 칸은 null로 (수정할 때 지운 값도 서버에서 지워지게) */
const text = (value: string) => value.trim() || null;

function VisitForm({
  patientId,
  initial,
  markVisited = false,
}: {
  patientId: string;
  initial?: MedicalVisit;
  /** 내 정보 '지난 진료'의 다녀왔어요로 들어오면 다녀옴 체크를 켠 채로 연다 */
  markVisited?: boolean;
}) {
  const theme = useTheme();
  const [date, setDate] = useState(toDateParts(initial?.visit_date));
  const [hospital, setHospital] = useState(initial?.hospital_name ?? '');
  const [department, setDepartment] = useState(initial?.department ?? '');
  const [reason, setReason] = useState(initial?.visit_reason ?? '');
  const [visited, setVisited] = useState(markVisited || (initial?.is_visited ?? false));
  const [doctorNote, setDoctorNote] = useState(initial?.doctor_note ?? '');
  const [medicationChange, setMedicationChange] = useState(initial?.medication_change ?? '');
  const [followUp, setFollowUp] = useState(initial?.follow_up_plan ?? '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const parsed = parseDateParts(date.year, date.month, date.day, { allowFuture: true });
  // 다녀옴 표시와 진료 메모는 진료일이 오늘이거나 지난 경우에만
  const canMarkVisited = 'value' in parsed && parsed.value !== null && parsed.value <= isoDate();

  const save = async () => {
    if ('error' in parsed || parsed.value === null) {
      setError('error' in parsed ? parsed.error : '진료 날짜를 입력해 주세요.');
      return;
    }
    const isVisited = canMarkVisited && visited;
    const data: MedicalVisitInput = {
      visit_date: parsed.value,
      is_visited: isVisited,
      hospital_name: text(hospital),
      department: text(department),
      visit_reason: text(reason),
      ...(isVisited
        ? { doctor_note: text(doctorNote), medication_change: text(medicationChange), follow_up_plan: text(followUp) }
        : {}),
    };
    setSaving(true);
    setError(null);
    try {
      const userId = currentUserId();
      if (initial) await updateVisit(userId, patientId, initial.id, data);
      else await createVisit(userId, patientId, data);
      router.back();
    } catch (e) {
      setError(toApiError(e).message);
      setSaving(false);
    }
  };

  return (
    <Screen>
      <View style={styles.field}>
        <SectionLabel>진료 날짜</SectionLabel>
        <DateFields value={date} onChange={setDate} label="진료 날짜" />
      </View>
      <View style={styles.field}>
        <SectionLabel>병원 (선택)</SectionLabel>
        <TextField value={hospital} onChangeText={setHospital} placeholder="예: 서울대학교병원" maxLength={100} />
      </View>
      <View style={styles.field}>
        <SectionLabel>진료과 (선택)</SectionLabel>
        <View style={styles.wrap}>
          {DEPARTMENTS.map((d) => (
            <Chip key={d} label={d} selected={department === d} onPress={() => setDepartment(department === d ? '' : d)} />
          ))}
        </View>
      </View>
      <View style={styles.field}>
        <SectionLabel>가는 이유 (선택)</SectionLabel>
        <TextField value={reason} onChangeText={setReason} placeholder="예: 정기 진료, 약 조절 상담" maxLength={200} />
      </View>

      {canMarkVisited ? (
        <View style={styles.field}>
          <SectionLabel>다녀온 뒤</SectionLabel>
          <Checkbox label="진료를 다녀왔어요" checked={visited} onChange={setVisited} disabled={saving} />
          {visited ? (
            <>
              <ThemedText type="smallBold">의사 선생님 당부 (선택)</ThemedText>
              <TextField
                value={doctorNote}
                onChangeText={setDoctorNote}
                placeholder="예: 낮잠을 30분 넘지 않게"
                accessibilityLabel="의사 선생님 당부"
                multiline
                maxLength={500}
                style={styles.textArea}
              />
              <ThemedText type="smallBold">약 변경 (선택)</ThemedText>
              <TextField
                value={medicationChange}
                onChangeText={setMedicationChange}
                placeholder="예: 도네페질 5mg → 10mg"
                accessibilityLabel="약 변경"
                maxLength={300}
              />
              <ThemedText type="smallBold">다음 진료 계획 (선택)</ThemedText>
              <TextField
                value={followUp}
                onChangeText={setFollowUp}
                placeholder="예: 3개월 뒤 인지검사"
                accessibilityLabel="다음 진료 계획"
                maxLength={300}
              />
            </>
          ) : null}
        </View>
      ) : null}

      {error ? (
        <ThemedText accessibilityRole="alert" style={{ color: theme.accent }}>
          {error}
        </ThemedText>
      ) : null}
      <View style={styles.field}>
        <Button label={saving ? '저장하고 있어요…' : '저장하기'} onPress={save} disabled={saving} />
        <Button label="취소" variant="secondary" onPress={() => router.back()} disabled={saving} />
      </View>
    </Screen>
  );
}

/** 진료 예약 추가(id 없음) / 수정·다녀옴 표시(id 있음). patientId는 필수 */
export default function VisitFormScreen() {
  const { patientId, id, visited } = useLocalSearchParams<{ patientId: string; id?: string; visited?: string }>();
  const loadProfile = useCallback(() => getUserProfile(currentUserId()), []);
  const profile = useApi(loadProfile);
  // 지난 날짜의 미방문 진료는 /feed에 없어서 따로 불러온다 (내 정보 '지난 진료'에서 들어온 경우)
  const loadPending = useCallback(
    () => (patientId && id ? getPendingVisits(currentUserId(), patientId) : Promise.resolve([])),
    [patientId, id],
  );
  const pending = useApi(loadPending);
  const context = profile.data?.patients.find(({ patient }) => patient.id === patientId);
  const existing = id
    ? [...(context?.upcoming_visits ?? []), ...(context?.recent_visits ?? []), ...(pending.data ?? [])].find(
        (v) => v.id === id,
      )
    : undefined;
  const who = context?.patient.name;
  const title = id ? '진료 기록' : '진료 예약 추가';

  if (!patientId || (id && !existing)) {
    return (
      <Screen>
        <Stack.Screen options={{ title }} />
        {profile.error ? <ErrorView message={profile.error.message} onRetry={() => profile.reload()} /> : null}
        {profile.loading || pending.loading ? <LoadingView /> : null}
        {(profile.data && !pending.loading) || !patientId ? (
          <ThemedText themeColor="textSecondary">이 진료 기록을 찾을 수 없어요.</ThemedText>
        ) : null}
      </Screen>
    );
  }

  return (
    <>
      <Stack.Screen options={{ title: who ? `${who} · ${title}` : title }} />
      <VisitForm key={existing?.id ?? 'new'} patientId={patientId} initial={existing} markVisited={visited === '1'} />
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
  textArea: {
    minHeight: 88,
    paddingVertical: Spacing.three,
    textAlignVertical: 'top',
  },
});
