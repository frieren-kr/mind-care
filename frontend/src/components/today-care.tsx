import { router, useFocusEffect, type Href } from 'expo-router';
import { useCallback, useRef, useState } from 'react';
import { Pressable, StyleSheet, View } from 'react-native';

import { createCareLog, currentUserId, getCareLogs } from '@/api';
import { toApiError } from '@/api/client';
import { ThemedText } from '@/components/themed-text';
import { Button, Chip, SectionLabel } from '@/components/ui';
import { MinTouch, Radius, Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import { useTheme } from '@/hooks/use-theme';
import type { PatientContext } from '@/types';
import { daysFromToday, formatDate, isoDate } from '@/utils/date';

/** 앱 자가점검(KDSQ-C)을 다시 권하는 간격. 1년 전과 비교하는 설문이라 자주 할 필요는 없다 */
const SELF_CHECK_EVERY_DAYS = 30;
/** 복약 확인을 돌봄 기록으로 남길 때 앞에 붙이는 말. 오늘 이 말로 시작하는 기록이 있으면 '챙김'으로 본다 */
const MEDS_PREFIX = '복약 확인:';

type Mission = {
  key: string;
  title: string;
  meta?: string | null;
  /** undefined = 할 일이 아닌 안내 (진료 D-day 등) */
  done?: boolean;
  href?: Href;
  action?: { label: string; onPress: () => void; busy: boolean };
};

function MissionRow({ mission }: { mission: Mission }) {
  const theme = useTheme();
  const { done, href, action } = mission;
  const checkable = done !== undefined;
  return (
    <Pressable
      accessibilityRole={href ? 'button' : undefined}
      accessibilityLabel={`${mission.title}${checkable ? (done ? ', 완료' : ', 할 일') : ''}`}
      disabled={!href}
      onPress={() => href && router.navigate(href)}
      style={({ pressed }) => [styles.row, { borderTopColor: theme.border }, pressed && { backgroundColor: theme.backgroundSelected }]}>
      {/* 신문 체크 상자: 할 일은 빈 네모, 끝낸 일은 먹색 네모 + ✓, 안내는 점 */}
      <View
        style={[
          styles.mark,
          checkable
            ? { borderColor: theme.rule, borderWidth: 2, backgroundColor: done ? theme.primary : 'transparent' }
            : { borderColor: 'transparent' },
        ]}>
        <ThemedText type="smallBold" style={{ color: done ? theme.onPrimary : theme.textSecondary }}>
          {done ? '✓' : checkable ? '' : '·'}
        </ThemedText>
      </View>
      <View style={styles.flex}>
        <ThemedText type="bold" themeColor={done ? 'textSecondary' : 'text'}>
          {mission.title}
        </ThemedText>
        {mission.meta ? (
          <ThemedText type="small" themeColor="textSecondary">
            {mission.meta}
          </ThemedText>
        ) : null}
      </View>
      {action && !done ? (
        <View>
          <Button label={action.busy ? '저장 중…' : action.label} variant="secondary" onPress={action.onPress} disabled={action.busy} />
        </View>
      ) : href ? (
        <ThemedText type="bold" aria-hidden>
          ›
        </ThemedText>
      ) : null}
    </Pressable>
  );
}

/** 한 분의 오늘 할 일. 돌봄 기록·복약 확인은 오늘 돌봄 기록에서, 자가점검·진료는 프로필(/feed)에서 판단한다 */
function PatientToday({ context }: { context: PatientContext }) {
  const theme = useTheme();
  const { patient, current_medications: meds, latest_assessments, upcoming_visits, recent_visits } = context;
  const loadLogs = useCallback(() => getCareLogs(currentUserId(), patient.id, { days: 1 }), [patient.id]);
  const logs = useApi(loadLogs);
  // 다른 화면(돌봄 기록·자가점검)에서 돌아오면 다시 불러온다
  const firstFocus = useRef(true);
  const { reload } = logs;
  useFocusEffect(
    useCallback(() => {
      if (firstFocus.current) {
        firstFocus.current = false;
        return;
      }
      reload();
    }, [reload]),
  );
  const [savingMeds, setSavingMeds] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // days=1은 '지금부터 24시간'이라 어제 저녁 기록이 섞인다 → 오늘 날짜(폰 기준)만
  const today = isoDate();
  const todayLogs = (logs.data?.items ?? []).filter((log) => isoDate(new Date(log.logged_at)) === today);
  const medsDone = todayLogs.some((log) => log.content?.startsWith(MEDS_PREFIX));
  const careLogsDone = todayLogs.some((log) => !log.content?.startsWith(MEDS_PREFIX));
  const known = logs.data !== null;
  const medNames = meds.map((m) => [m.drug_name, m.dosage].filter(Boolean).join(' ')).join(', ');

  // '챙겨 드렸어요' = 돌봄 기록 한 줄. 챗봇이 복약을 잘 챙기는지도 기록으로 볼 수 있다
  const checkMeds = async () => {
    setSavingMeds(true);
    setError(null);
    try {
      await createCareLog(currentUserId(), patient.id, { content: `${MEDS_PREFIX} ${medNames} 챙겨 드렸다.`, mood_tag: null });
      logs.reload();
    } catch (e) {
      setError(toApiError(e).message);
    } finally {
      setSavingMeds(false);
    }
  };

  const missions: Mission[] = [
    {
      key: 'care',
      title: '오늘 있었던 일 기록하기',
      meta: careLogsDone ? '오늘 기록을 남겼어요' : '짧게라도 남기면 챗봇이 변화를 같이 봐 드려요',
      done: known ? careLogsDone : undefined,
      href: '/care',
    },
  ];

  if (meds.length > 0) {
    missions.push({
      key: 'meds',
      title: '약 챙겨 드리기',
      meta: meds.map((m) => [m.drug_name, m.frequency].filter(Boolean).join(' · ')).join(' / '),
      done: known ? medsDone : undefined,
      action: { label: '챙겨 드렸어요', onPress: checkMeds, busy: savingMeds },
    });
  }

  const lastCheck = latest_assessments.find((a) => a.assessment_type === 'KDSQ-C');
  const daysSinceCheck = lastCheck ? -daysFromToday(lastCheck.assessed_at) : null;
  const checkDone = daysSinceCheck !== null && daysSinceCheck < SELF_CHECK_EVERY_DAYS;
  missions.push({
    key: 'check',
    title: checkDone ? '인지기능 자가점검' : '이번 달 자가점검 하기',
    meta: lastCheck
      ? `${daysSinceCheck === 0 ? '오늘' : `${daysSinceCheck}일 전`} 점검 · ${lastCheck.score ?? '-'}점 / 30점`
      : '아직 저장한 점검 결과가 없어요 (15문항 · 5분)',
    done: checkDone,
    href: checkDone ? undefined : '/check',
  });

  // 오늘 진료: 다녀오면 기록 / 일주일 안 진료: 물어볼 것 정리 (안내)
  const next = upcoming_visits[0];
  const visitedToday = recent_visits.find((v) => v.visit_date === today);
  const place = (v: typeof next) => [v.hospital_name, v.department].filter(Boolean).join(' ');
  if (next && daysFromToday(next.visit_date) === 0) {
    missions.push({
      key: 'visit',
      title: '오늘 진료 — 다녀오면 기록하기',
      meta: place(next) || null,
      done: false,
      href: { pathname: '/visit/form', params: { patientId: patient.id, id: next.id } },
    });
  } else if (visitedToday) {
    missions.push({ key: 'visit', title: '오늘 진료 기록', meta: place(visitedToday) || null, done: true });
  } else if (next && daysFromToday(next.visit_date) <= 7) {
    missions.push({
      key: 'visit',
      title: `D-${daysFromToday(next.visit_date)} 진료 준비`,
      meta: `${formatDate(next.visit_date)} ${place(next)} · 물어볼 것을 챗봇과 정리해 보세요`,
      href: '/chat',
    });
  }

  const todo = missions.filter((m) => m.done !== undefined);
  const doneCount = todo.filter((m) => m.done).length;

  return (
    <View style={styles.patient}>
      <View style={styles.header}>
        <ThemedText type="small" themeColor="textSecondary" style={styles.flex}>
          {[patient.dementia_stage, patient.symptoms.slice(0, 2).join(', ')].filter(Boolean).join(' · ')}
        </ThemedText>
        {known ? (
          <ThemedText type="smallBold" accessibilityLabel={`오늘 할 일 ${todo.length}개 중 ${doneCount}개 완료`}>
            {doneCount} / {todo.length} 완료
          </ThemedText>
        ) : null}
      </View>
      {/* 진행 막대: 자가점검 화면과 같은 모양 */}
      <View style={[styles.track, { backgroundColor: theme.border }]}>
        <View
          style={[styles.fill, { backgroundColor: theme.primary, width: `${todo.length ? (doneCount / todo.length) * 100 : 0}%` }]}
        />
      </View>
      {missions.map((m) => (
        <MissionRow key={m.key} mission={m} />
      ))}
      {error || logs.error ? (
        <ThemedText type="small" accessibilityRole="alert" style={{ color: theme.accent }}>
          {error ?? `오늘 기록을 불러오지 못했어요. ${logs.error?.message ?? ''}`}
        </ThemedText>
      ) : null}
    </View>
  );
}

/** 홈 '오늘의 돌봄' — 돌보는 분별 오늘 할 일 (노션 7.2 미션·대시보드) */
export function TodayCare({ patients }: { patients: PatientContext[] }) {
  const [chosen, setChosen] = useState<string | null>(null);
  const current = patients.find(({ patient }) => patient.id === chosen) ?? patients[0];

  return (
    <View style={styles.section}>
      <SectionLabel>오늘의 돌봄</SectionLabel>
      {patients.length === 0 ? (
        <ThemedText themeColor="textSecondary">내 정보에서 돌보는 분을 추가하면 오늘 할 일을 알려 드려요.</ThemedText>
      ) : null}
      {patients.length > 1 ? (
        <View style={styles.chips}>
          {patients.map(({ patient }, index) => (
            <Chip
              key={patient.id}
              label={patient.name || `환자 ${index + 1}`}
              selected={patient.id === current?.patient.id}
              onPress={() => setChosen(patient.id)}
            />
          ))}
        </View>
      ) : null}
      {current ? <PatientToday key={current.patient.id} context={current} /> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  section: {
    gap: Spacing.two,
  },
  chips: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: Spacing.two,
  },
  patient: {
    gap: Spacing.two,
  },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
  },
  track: {
    height: 6,
    borderRadius: Radius.pill,
    overflow: 'hidden',
  },
  fill: {
    height: '100%',
  },
  row: {
    minHeight: MinTouch,
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.three,
    borderTopWidth: StyleSheet.hairlineWidth,
    paddingVertical: Spacing.two,
  },
  mark: {
    width: 24,
    height: 24,
    alignItems: 'center',
    justifyContent: 'center',
  },
  flex: {
    flex: 1,
  },
});
