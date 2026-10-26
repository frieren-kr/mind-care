import { useCallback, useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { currentUserId, getUserProfile, updateFeedSettings } from '@/api';
import { toApiError } from '@/api/client';
import { ThemedText } from '@/components/themed-text';
import { Card, Chip, ErrorView, LoadingView, Screen } from '@/components/ui';
import { Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
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
    </Card>
  );
}

export default function MyScreen() {
  const loadProfile = useCallback(() => getUserProfile(currentUserId()), []);
  const { data: profile, error, loading, reload } = useApi(loadProfile);
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
          <Card>
            <ThemedText type="subtitle">{profile.name}님</ThemedText>
            <ThemedText themeColor="textSecondary">
              간병인
              {profile.caregiver_profile?.relationship ? ` · ${profile.caregiver_profile.relationship}` : ''}
            </ThemedText>
          </Card>

          <ThemedText type="subtitle">돌보는 분 ({profile.patients.length}명)</ThemedText>
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

      {/* '어떤 설명이 편하세요? (쉽게/자세하게)'는 난이도별 요약을 만들지 정해질 때까지 숨긴다 (10/26) */}
      <Card>
        <ThemedText type="subtitle">도움이 필요하면</ThemedText>
        <ThemedText>치매상담콜센터 1899-9988 (24시간)</ThemedText>
        <ThemedText>가까운 치매안심센터에서 무료 검사를 받을 수 있어요</ThemedText>
      </Card>
    </Screen>
  );
}

const styles = StyleSheet.create({
  wrapRow: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: Spacing.two,
  },
});
