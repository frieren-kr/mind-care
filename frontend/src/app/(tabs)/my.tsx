import { router, useFocusEffect } from 'expo-router';
import { useCallback, useRef, useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { NAME_MAX_LENGTH, currentUserId, getUserProfile, logout, updateFeedSettings, updateUserName } from '@/api';
import { toApiError } from '@/api/client';
import { ThemedText } from '@/components/themed-text';
import { Button, Card, Chip, ErrorView, LoadingView, Screen, TextField } from '@/components/ui';
import { Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import { useAuth } from '@/hooks/use-auth';
import { TEXT_SCALES, useTextScale } from '@/hooks/use-text-scale';
import { useTheme } from '@/hooks/use-theme';
import type { PatientContext } from '@/types';

/** 피드 갱신 주기 선택지 (users.feed_refresh_days, 1~7일, 기본 3일) */
const refreshDayOptions = [1, 2, 3, 4, 5, 6, 7];
const DEFAULT_REFRESH_DAYS = 3;

function PatientCard({ context, index }: { context: PatientContext; index: number }) {
  const { patient, current_medications, upcoming_visits } = context;
  const nextVisit = upcoming_visits[0];
  const stage = [patient.dementia_stage, patient.diagnosis_date && `${patient.diagnosis_date} 진단`]
    .filter(Boolean)
    .join(' · ');

  return (
    <Card>
      <ThemedText type="subtitle">{patient.name || `환자 ${index + 1}`}</ThemedText>
      {stage ? <ThemedText themeColor="textSecondary">{stage}</ThemedText> : null}
      {patient.symptoms.length > 0 && (
        <ThemedText>주요 증상: {patient.symptoms.join(', ')}</ThemedText>
      )}
      {patient.interests.length > 0 && (
        <ThemedText>관심 분야: {patient.interests.join(', ')}</ThemedText>
      )}
      {current_medications.length > 0 && (
        <ThemedText>복용 중인 약: {current_medications.map((m) => m.drug_name).join(', ')}</ThemedText>
      )}
      {nextVisit ? (
        <ThemedText>
          다음 진료: {nextVisit.visit_date}
          {nextVisit.hospital_name ? ` · ${nextVisit.hospital_name}` : ''}
        </ThemedText>
      ) : null}
      <Button
        label="정보 수정"
        variant="secondary"
        onPress={() => router.push({ pathname: '/patient/form', params: { id: patient.id } })}
      />
    </Card>
  );
}

/** 이름(닉네임) 보기·수정. 홈 인사말 등 다른 화면은 다시 불러올 때 새 이름이 나온다 */
function NameCard({ name, onSaved }: { name: string; onSaved: () => void }) {
  const theme = useTheme();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(name);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const trimmed = draft.trim();

  const startEdit = () => {
    setDraft(name);
    setError(null);
    setEditing(true);
  };

  const save = async () => {
    if (saving) return;
    if (!trimmed) {
      setError('이름을 입력해 주세요.');
      return;
    }
    if (trimmed === name) {
      setEditing(false);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await updateUserName(currentUserId(), trimmed);
      setEditing(false);
      onSaved();
    } catch (e) {
      setError(toApiError(e).message);
    } finally {
      setSaving(false);
    }
  };

  if (!editing) {
    return (
      <Card>
        <View style={styles.headerRow}>
          <ThemedText type="subtitle" style={styles.flex}>
            {name}님
          </ThemedText>
          <Button label="이름 수정" variant="secondary" onPress={startEdit} />
        </View>
      </Card>
    );
  }

  return (
    <Card>
      <ThemedText type="smallBold">앱에서 부를 이름</ThemedText>
      <TextField
        value={draft}
        onChangeText={setDraft}
        placeholder="이름 또는 별명"
        accessibilityLabel="이름"
        maxLength={NAME_MAX_LENGTH}
        autoFocus
        returnKeyType="done"
        onSubmitEditing={save}
        editable={!saving}
      />
      {error ? (
        <ThemedText type="small" accessibilityRole="alert" style={{ color: theme.accent }}>
          {error}
        </ThemedText>
      ) : null}
      <View style={styles.headerRow}>
        <View style={styles.flex}>
          <Button label="취소" variant="secondary" onPress={() => setEditing(false)} disabled={saving} />
        </View>
        <View style={styles.flex}>
          <Button label={saving ? '저장하는 중…' : '저장'} onPress={save} disabled={saving || !trimmed} />
        </View>
      </View>
    </Card>
  );
}

export default function MyScreen() {
  const loadProfile = useCallback(() => getUserProfile(currentUserId()), []);
  const { data: profile, error, loading, reload } = useApi(loadProfile);
  // 돌보는 분 추가·수정 화면에서 돌아오면 다시 불러온다 (처음 열 때는 useApi가 이미 불러온다)
  const firstFocus = useRef(true);
  useFocusEffect(
    useCallback(() => {
      if (firstFocus.current) {
        firstFocus.current = false;
        return;
      }
      reload();
    }, [reload]),
  );
  const { scale: textScale, setScale: setTextScale } = useTextScale();
  const { session } = useAuth();
  const [loggingOut, setLoggingOut] = useState(false);
  const [savedDays, setSavedDays] = useState<number | null>(null);
  const [savingDays, setSavingDays] = useState<number | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const refreshDays = savedDays ?? profile?.feed_refresh_days ?? DEFAULT_REFRESH_DAYS;

  const saveRefreshDays = async (days: number) => {
    if (days === refreshDays || savingDays !== null) return;
    setSavingDays(days);
    setSaveError(null);
    try {
      const saved = await updateFeedSettings(currentUserId(), days);
      setSavedDays(saved.feed_refresh_days);
    } catch (e) {
      setSaveError(toApiError(e).message);
    } finally {
      setSavingDays(null);
    }
  };

  return (
    <Screen refreshing={loading && profile !== null} onRefresh={() => reload()}>
      {error ? <ErrorView message={error.message} onRetry={() => reload()} /> : null}
      {loading && profile === null ? <LoadingView /> : null}

      {profile ? (
        <>
          {/* 관계(며느리 등)는 화면에는 안 보여 주지만 AI 추천·챗봇 입력에는 그대로 쓰인다 */}
          <NameCard name={profile.name} onSaved={() => reload()} />

          <View style={styles.headerRow}>
            <ThemedText type="subtitle" style={styles.flex}>
              돌보는 분 ({profile.patients.length}명)
            </ThemedText>
            <Button
              label="+ 추가"
              variant="secondary"
              onPress={() => router.push({ pathname: '/patient/form' })}
            />
          </View>
          {profile.patients.map((context, index) => (
            <PatientCard key={context.patient.id} context={context} index={index} />
          ))}
          {profile.patients.length === 0 && (
            <ThemedText themeColor="textSecondary">아직 등록된 분이 없어요.</ThemedText>
          )}

          <Card>
            <ThemedText type="subtitle">새 연구 추천 주기</ThemedText>
            <ThemedText themeColor="textSecondary">
              맞춤 추천을 며칠마다 새로 만들지 골라 주세요. 바꾼 주기는 다음에 피드를 열 때부터 적용돼요.
            </ThemedText>
            <View style={styles.wrapRow}>
              {refreshDayOptions.map((days) => (
                <Chip
                  key={days}
                  label={days === DEFAULT_REFRESH_DAYS ? `${days}일 (기본)` : `${days}일`}
                  selected={(savingDays ?? refreshDays) === days}
                  onPress={() => saveRefreshDays(days)}
                />
              ))}
            </View>
            {savingDays !== null ? (
              <ThemedText type="small" themeColor="textSecondary">
                저장하고 있어요…
              </ThemedText>
            ) : saveError ? (
              <ThemedText type="small" themeColor="textSecondary">
                저장하지 못했어요. {saveError}
              </ThemedText>
            ) : (
              <ThemedText type="small" themeColor="textSecondary">
                {refreshDays}일마다 새 추천을 만들어요.
              </ThemedText>
            )}
          </Card>
        </>
      ) : null}

      {/* 글자 크기는 서버와 상관없는 이 폰의 설정이라 프로필을 못 불러와도 보여 준다 */}
      <Card>
        <ThemedText type="subtitle">글자 크기</ThemedText>
        <View style={styles.wrapRow}>
          {TEXT_SCALES.map((s) => (
            <Chip key={s.key} label={s.label} selected={textScale === s.key} onPress={() => setTextScale(s.key)} />
          ))}
        </View>
        <ThemedText themeColor="textSecondary">이 크기로 연구 소식을 보여 드려요.</ThemedText>
      </Card>

      {/* '어떤 설명이 편하세요? (쉽게/자세하게)'는 난이도별 요약을 만들지 정해질 때까지 숨긴다 (10/26) */}
      <Card>
        <ThemedText type="subtitle">도움이 필요하면</ThemedText>
        <ThemedText>치매상담콜센터 1899-9988 (24시간)</ThemedText>
        <ThemedText>가까운 치매안심센터에서 무료 검사를 받을 수 있어요</ThemedText>
      </Card>

      {/* 프로필을 못 불러와도(토큰 문제 등) 로그아웃은 할 수 있어야 해서 맨 아래 따로 둔다 */}
      <Card>
        <ThemedText type="subtitle">계정</ThemedText>
        {session ? <ThemedText themeColor="textSecondary">{session.user.email}</ThemedText> : null}
        <Button
          label={loggingOut ? '로그아웃하는 중…' : '로그아웃'}
          variant="secondary"
          disabled={loggingOut}
          onPress={() => {
            setLoggingOut(true);
            // 끝나면 세션이 지워져 로그인 화면으로 바뀐다 (이 화면은 닫힌다)
            logout();
          }}
        />
      </Card>
    </Screen>
  );
}

const styles = StyleSheet.create({
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
  },
  flex: {
    flex: 1,
  },
  wrapRow: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: Spacing.two,
  },
});
