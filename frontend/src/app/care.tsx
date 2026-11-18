import { useCallback, useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { createCareLog, currentUserId, getCareLogs, getUserProfile } from '@/api';
import { toApiError } from '@/api/client';
import { ThemedText } from '@/components/themed-text';
import { Button, Card, Chip, Disclaimer, ErrorView, LoadingView, Screen, SectionLabel, TextField } from '@/components/ui';
import { Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import { useTheme } from '@/hooks/use-theme';
import type { CareLog, CareMood } from '@/types';
import { todayLabel } from '@/utils/date';

const MOODS: CareMood[] = ['평온', '안도', '뿌듯함', '걱정', '불안', '지침'];
const MAX_LENGTH = 1000; // user_schemas.CARE_LOG_MAX_CHARS
const DAYS = 14; // 챗봇이 읽는 기간과 같다

/** 언제 있었던 일인지. 시계 대신 고르기 쉬운 몇 개로 (daysAgo일 전 hour시) */
type When = { label: string; daysAgo: number; hour: number } | { label: '지금'; now: true };
const WHEN_OPTIONS: When[] = [
  { label: '지금', now: true },
  { label: '오늘 아침', daysAgo: 0, hour: 8 },
  { label: '오늘 낮', daysAgo: 0, hour: 13 },
  { label: '오늘 저녁', daysAgo: 0, hour: 19 },
  { label: '어젯밤', daysAgo: 1, hour: 22 },
  { label: '어제 낮', daysAgo: 1, hour: 13 },
];

function whenToDate(when: When): Date | null {
  if ('now' in when) return null;
  const d = new Date();
  d.setDate(d.getDate() - when.daysAgo);
  d.setHours(when.hour, 0, 0, 0);
  return d;
}

/** 오후 3:05 */
function timeLabel(date: Date): string {
  const h = date.getHours();
  return `${h < 12 ? '오전' : '오후'} ${h % 12 || 12}:${String(date.getMinutes()).padStart(2, '0')}`;
}

/** 최신순 기록을 날짜별로 묶는다 (폰 시간 기준) */
function groupByDay(logs: CareLog[]): { day: string; logs: CareLog[] }[] {
  const groups: { day: string; logs: CareLog[] }[] = [];
  for (const log of logs) {
    const day = todayLabel(new Date(log.logged_at));
    const last = groups[groups.length - 1];
    if (last?.day === day) last.logs.push(log);
    else groups.push({ day, logs: [log] });
  }
  return groups;
}

function LogEntry({ log }: { log: CareLog }) {
  const theme = useTheme();
  return (
    <View style={[styles.entry, { borderColor: theme.border }]}>
      <View style={styles.entryHead}>
        <ThemedText type="smallBold">{timeLabel(new Date(log.logged_at))}</ThemedText>
        {log.mood_tag ? (
          <ThemedText type="small" themeColor="textSecondary">
            · {log.mood_tag}
          </ThemedText>
        ) : null}
      </View>
      <ThemedText>{log.content}</ThemedText>
    </View>
  );
}

/** 한 분의 기록 쓰기 + 최근 14일 목록 */
function PatientCareLogs({ patientId }: { patientId: string }) {
  const theme = useTheme();
  const loadLogs = useCallback(() => getCareLogs(currentUserId(), patientId, { days: DAYS }), [patientId]);
  const logs = useApi(loadLogs);
  const [content, setContent] = useState('');
  const [mood, setMood] = useState<CareMood | null>(null);
  const [when, setWhen] = useState<When>(WHEN_OPTIONS[0]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedNote, setSavedNote] = useState(false);

  // 아직 오지 않은 때(예: 아침에 '오늘 저녁')는 고를 수 없다
  const now = new Date();
  const options = WHEN_OPTIONS.filter((w) => {
    const d = whenToDate(w);
    return d === null || d <= now;
  });

  const save = async () => {
    const text = content.trim();
    if (!text || saving) return;
    setSaving(true);
    setError(null);
    setSavedNote(false);
    try {
      await createCareLog(currentUserId(), patientId, {
        content: text,
        mood_tag: mood,
        logged_at: whenToDate(when)?.toISOString(),
      });
      setContent('');
      setMood(null);
      setWhen(WHEN_OPTIONS[0]);
      setSavedNote(true);
      logs.reload();
    } catch (e) {
      setError(toApiError(e).message);
    } finally {
      setSaving(false);
    }
  };

  const items = logs.data?.items ?? [];

  return (
    <>
      <View style={styles.section}>
        <SectionLabel>오늘 있었던 일</SectionLabel>
        <Card style={styles.form}>
          <TextField
            value={content}
            onChangeText={(t) => {
              setContent(t);
              setSavedNote(false);
            }}
            placeholder="예: 새벽 3시에 깨서 거실을 서성이셨다"
            accessibilityLabel="기록 내용"
            multiline
            maxLength={MAX_LENGTH}
            editable={!saving}
            style={styles.textArea}
          />
          <ThemedText type="smallBold">언제였나요?</ThemedText>
          <View style={styles.wrap}>
            {options.map((w) => (
              <Chip key={w.label} label={w.label} selected={when.label === w.label} onPress={() => setWhen(w)} />
            ))}
          </View>
          <ThemedText type="smallBold">그때 내 기분 (선택)</ThemedText>
          <View style={styles.wrap}>
            {MOODS.map((m) => (
              <Chip key={m} label={m} selected={mood === m} onPress={() => setMood(mood === m ? null : m)} />
            ))}
          </View>
          {error ? (
            <ThemedText accessibilityRole="alert" style={{ color: theme.accent }}>
              {error}
            </ThemedText>
          ) : null}
          {savedNote ? (
            <ThemedText accessibilityRole="alert" themeColor="textSecondary">
              저장했어요. 챗봇이 이 기록을 참고해서 답해요.
            </ThemedText>
          ) : null}
          <Button label={saving ? '저장하고 있어요…' : '기록 남기기'} onPress={save} disabled={saving || !content.trim()} />
        </Card>
      </View>

      <View style={styles.section}>
        <SectionLabel>최근 {DAYS}일 기록</SectionLabel>
        {logs.data ? (
          <ThemedText type="small" themeColor="textSecondary">
            {logs.data.total_count > items.length
              ? `${logs.data.total_count}건 중 최근 ${items.length}건`
              : `${logs.data.total_count}건`}
          </ThemedText>
        ) : null}
        {logs.loading && logs.data === null ? <LoadingView /> : null}
        {logs.error ? <ErrorView message={logs.error.message} onRetry={() => logs.reload()} /> : null}
        {logs.data && items.length === 0 ? (
          <ThemedText themeColor="textSecondary">아직 기록이 없어요. 오늘 있었던 일을 짧게 남겨 보세요.</ThemedText>
        ) : null}
        {groupByDay(items).map((group) => (
          <View key={group.day} style={styles.day}>
            <ThemedText type="bold">{group.day}</ThemedText>
            {group.logs.map((log) => (
              <LogEntry key={`${log.logged_at}-${log.content}`} log={log} />
            ))}
          </View>
        ))}
      </View>
    </>
  );
}

/** 돌봄 기록. 챗봇(마인드 케어에게 묻기)이 최근 14일 기록에서 패턴을 찾아 답한다 */
export default function CareLogScreen() {
  const loadProfile = useCallback(() => getUserProfile(currentUserId()), []);
  const profile = useApi(loadProfile);
  const patients = profile.data?.patients ?? [];
  const [chosen, setChosen] = useState<string | null>(null);
  const patientId = chosen ?? patients[0]?.patient.id ?? null;

  return (
    <Screen>
      {profile.loading && profile.data === null ? <LoadingView /> : null}
      {profile.error ? <ErrorView message={profile.error.message} onRetry={() => profile.reload()} /> : null}
      {profile.data && patients.length === 0 ? (
        <ThemedText themeColor="textSecondary">내 정보에서 돌보는 분을 먼저 추가해 주세요.</ThemedText>
      ) : null}

      {patients.length > 1 ? (
        <View style={styles.section}>
          <SectionLabel>누구의 기록인가요?</SectionLabel>
          <View style={styles.wrap}>
            {patients.map(({ patient }, index) => (
              <Chip
                key={patient.id}
                label={patient.name || `환자 ${index + 1}`}
                selected={patient.id === patientId}
                onPress={() => setChosen(patient.id)}
              />
            ))}
          </View>
        </View>
      ) : null}

      {/* key: 다른 분으로 바꾸면 입력칸과 목록을 새로 */}
      {patientId ? <PatientCareLogs key={patientId} patientId={patientId} /> : null}

      <Disclaimer text="기록은 챗봇 답변을 돕는 참고 자료예요. 갑자기 상태가 나빠지면 담당 의료진과 상의해 주세요." />
    </Screen>
  );
}

const styles = StyleSheet.create({
  section: {
    gap: Spacing.two,
  },
  // 위의 머리말 선과 겹치지 않게 Card의 위쪽 선은 뺀다 (로그인 화면과 같다)
  form: {
    gap: Spacing.two,
    borderTopWidth: 0,
    paddingTop: 0,
  },
  textArea: {
    minHeight: 112,
    paddingVertical: Spacing.three,
    textAlignVertical: 'top',
  },
  wrap: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: Spacing.two,
  },
  day: {
    gap: Spacing.one,
    paddingTop: Spacing.two,
  },
  entry: {
    gap: Spacing.half,
    borderLeftWidth: 2,
    paddingLeft: Spacing.three,
    paddingVertical: Spacing.one,
  },
  entryHead: {
    flexDirection: 'row',
    gap: Spacing.one,
  },
});
