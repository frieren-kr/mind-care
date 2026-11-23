import { useCallback, useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { createCareLog, currentUserId, getCareLogs, getUserProfile } from '@/api';
import { toApiError } from '@/api/client';
import { MonthCalendar, monthStart } from '@/components/month-calendar';
import { ThemedText } from '@/components/themed-text';
import { Button, Card, Chip, Disclaimer, ErrorView, LoadingView, Screen, SectionLabel, TextField } from '@/components/ui';
import { Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import { useTheme } from '@/hooks/use-theme';
import type { CareLog, CareMood } from '@/types';
import { daysFromToday, isoDate, todayLabel } from '@/utils/date';

const MOODS: CareMood[] = ['평온', '안도', '뿌듯함', '걱정', '불안', '지침'];
const MAX_LENGTH = 1000; // user_schemas.CARE_LOG_MAX_CHARS
/** 서버가 받는 가장 오래된 기록 (user_schemas.CARE_LOG_MAX_DAYS). 그보다 전 날은 보기만 */
const WRITE_MAX_DAYS = 90;
/** 달력에서 넘겨 볼 수 있는 범위: 작년 1월 ~ 이번 달 */
const MIN_MONTH = new Date(new Date().getFullYear() - 1, 0, 1);

/** 그날 언제 있었던 일인지. 시계 대신 고르기 쉬운 몇 개로 (그날 hour시) */
type When = { label: string; hour: number } | { label: '지금'; now: true };
const TIMES_OF_DAY: When[] = [
  { label: '아침', hour: 8 },
  { label: '낮', hour: 13 },
  { label: '저녁', hour: 19 },
  { label: '밤', hour: 22 },
];

/** 고른 날의 시간 선택지. 오늘은 '지금' + 이미 지난 때만 */
function whenOptions(day: string): When[] {
  if (day !== isoDate()) return TIMES_OF_DAY;
  const now = new Date();
  return [{ label: '지금', now: true }, ...TIMES_OF_DAY.filter((w) => !('now' in w) && w.hour <= now.getHours())];
}

/** 'YYYY-MM-DD' + 선택 → 기록 시각. '지금'이면 null (서버 시각) */
function whenToDate(day: string, when: When): Date | null {
  if ('now' in when) return null;
  const [y, m, d] = day.split('-').map(Number);
  return new Date(y, m - 1, d, when.hour, 0, 0, 0);
}

/** 오후 3:05 */
function timeLabel(date: Date): string {
  const h = date.getHours();
  return `${h < 12 ? '오전' : '오후'} ${h % 12 || 12}:${String(date.getMinutes()).padStart(2, '0')}`;
}

function dayTitle(day: string): string {
  const [y, m, d] = day.split('-').map(Number);
  const label = todayLabel(new Date(y, m - 1, d));
  return day === isoDate() ? `${label} (오늘)` : label;
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

/** 고른 날에 기록 남기기 */
function AddLogForm({ patientId, day, onSaved }: { patientId: string; day: string; onSaved: () => void }) {
  const theme = useTheme();
  const options = whenOptions(day);
  const [content, setContent] = useState('');
  const [mood, setMood] = useState<CareMood | null>(null);
  const [when, setWhen] = useState<When>(options[0]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedNote, setSavedNote] = useState(false);

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
        logged_at: whenToDate(day, when)?.toISOString(),
      });
      setContent('');
      setMood(null);
      setSavedNote(true);
      onSaved();
    } catch (e) {
      setError(toApiError(e).message);
    } finally {
      setSaving(false);
    }
  };

  return (
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
  );
}

/** 한 분의 달력 + 고른 날의 기록 목록 + 그날 기록 남기기 */
function PatientCareLogs({ patientId }: { patientId: string }) {
  const today = isoDate();
  const [month, setMonth] = useState(() => monthStart(new Date()));
  const [selected, setSelected] = useState(today);
  // 보고 있는 달 전체 (달력 점 + 고른 날 목록)
  const start = isoDate(month);
  const end = isoDate(new Date(month.getFullYear(), month.getMonth() + 1, 0));
  const loadLogs = useCallback(
    () => getCareLogs(currentUserId(), patientId, { start, end }),
    [patientId, start, end],
  );
  const logs = useApi(loadLogs);

  const byDay: Record<string, CareLog[]> = {};
  for (const log of logs.data?.items ?? []) {
    const day = isoDate(new Date(log.logged_at));
    (byDay[day] ??= []).push(log);
  }
  const marked = Object.fromEntries(Object.entries(byDay).map(([d, l]) => [d, l.length]));
  // 목록은 그날 안에서 시간 순서대로
  const dayLogs = [...(byDay[selected] ?? [])].sort((a, b) => a.logged_at.localeCompare(b.logged_at));
  const writable = -daysFromToday(selected) <= WRITE_MAX_DAYS;

  // 다른 달로 넘기면 그 달의 마지막 고를 수 있는 날(이번 달이면 오늘, 지난 달이면 말일)을 고른다
  const changeMonth = (next: Date) => {
    setMonth(next);
    if (selected.slice(0, 7) !== isoDate(next).slice(0, 7)) {
      const last = isoDate(new Date(next.getFullYear(), next.getMonth() + 1, 0));
      setSelected(last > today ? today : last);
    }
  };

  return (
    <>
      <View style={styles.section}>
        <SectionLabel>돌봄 달력</SectionLabel>
        <MonthCalendar
          month={month}
          selected={selected}
          marked={marked}
          onSelect={setSelected}
          onChangeMonth={changeMonth}
          canSelect={(day) => day <= today}
          minMonth={MIN_MONTH}
          maxMonth={monthStart(new Date())}
        />
        {logs.error ? <ErrorView message={logs.error.message} onRetry={() => logs.reload()} /> : null}
      </View>

      <View style={styles.section}>
        <SectionLabel>{dayTitle(selected)}</SectionLabel>
        {logs.loading && logs.data === null ? <LoadingView /> : null}
        {logs.data && dayLogs.length === 0 ? (
          <ThemedText themeColor="textSecondary">이 날 남긴 기록이 없어요.</ThemedText>
        ) : null}
        {dayLogs.map((log) => (
          <LogEntry key={`${log.logged_at}-${log.content}`} log={log} />
        ))}
      </View>

      <View style={styles.section}>
        <SectionLabel>이 날 기록 남기기</SectionLabel>
        {writable ? (
          // key: 날짜를 바꾸면 시간 선택지·입력칸을 그날 기준으로 새로
          <AddLogForm key={selected} patientId={patientId} day={selected} onSaved={() => logs.reload()} />
        ) : (
          <ThemedText themeColor="textSecondary">
            {WRITE_MAX_DAYS}일보다 지난 날은 기록을 볼 수만 있어요.
          </ThemedText>
        )}
      </View>
    </>
  );
}

/** 돌봄 기록. 달력에서 날짜를 골라 그날 기록을 보고 남긴다. 챗봇이 최근 14일 기록에서 패턴을 찾아 답한다 */
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

      {/* key: 다른 분으로 바꾸면 달력·입력칸을 새로 */}
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
