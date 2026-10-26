import { router } from 'expo-router';
import { useCallback } from 'react';
import { StyleSheet, View } from 'react-native';

import { currentUserId, getRecommendations, getUserProfile } from '@/api';
import { PaperCard } from '@/components/paper-card';
import { SkeletonPaperCard } from '@/components/skeleton';
import { ThemedText } from '@/components/themed-text';
import { Button, Card, ErrorView, Screen, SectionLabel } from '@/components/ui';
import { Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import type { UserProfileContext } from '@/types';
import { feedCaption } from '@/utils/feed';

/** 관리 환자 전체에서 가장 가까운 진료 예약 (visit_date는 'YYYY-MM-DD'라 문자열 비교로 정렬된다) */
function nextVisit(profile: UserProfileContext) {
  const visits = profile.patients.flatMap(({ patient, upcoming_visits }, index) =>
    upcoming_visits.map((visit) => ({ visit, name: patient.name || `환자 ${index + 1}` })),
  );
  return visits.sort((a, b) => a.visit.visit_date.localeCompare(b.visit.visit_date))[0] ?? null;
}

export default function HomeScreen() {
  const loadProfile = useCallback(() => getUserProfile(currentUserId()), []);
  const profile = useApi(loadProfile);
  // 피드 탭과 같은 추천. AI 결과는 서버에 저장돼 있어(기본 3일) 다시 만들지 않는다
  const loadFeed = useCallback(() => getRecommendations(currentUserId()), []);
  const feed = useApi(loadFeed);

  const reloadAll = () => {
    profile.reload();
    feed.reload();
  };

  const me = profile.data;
  const top = feed.data?.items[0];
  const visit = me ? nextVisit(me) : null;

  return (
    <Screen refreshing={(profile.loading && me !== null) || (feed.loading && feed.data !== null)} onRefresh={reloadAll}>
      <ThemedText type="title">{me ? `안녕하세요, ${me.name}님` : '안녕하세요'}</ThemedText>
      {profile.error ? <ErrorView message={profile.error.message} onRetry={() => profile.reload()} /> : null}

      <View style={styles.section}>
        <SectionLabel>오늘의 맞춤 연구</SectionLabel>
        {feed.data ? (
          <ThemedText type="small" themeColor="textSecondary">
            {feedCaption(feed.data)}
          </ThemedText>
        ) : null}
        {feed.loading && feed.data === null ? <SkeletonPaperCard /> : null}
        {feed.error && !profile.error ? (
          <ErrorView message={feed.error.message} onRetry={() => feed.reload()} />
        ) : null}
        {top ? <PaperCard item={top} /> : null}
        {feed.data && feed.data.items.length === 0 ? (
          <ThemedText themeColor="textSecondary">아직 추천할 연구를 찾지 못했어요</ThemedText>
        ) : null}
        {feed.data && feed.data.items.length > 1 ? (
          <Button
            label={`추천 연구 ${feed.data.items.length}편 모두 보기`}
            onPress={() => router.navigate('/feed')}
          />
        ) : null}
      </View>

      {me ? (
        <Card>
          <ThemedText type="subtitle">돌보는 분</ThemedText>
          {me.patients.length === 0 ? (
            <ThemedText themeColor="textSecondary">아직 등록된 분이 없어요.</ThemedText>
          ) : (
            me.patients.map(({ patient }, index) => (
              <ThemedText key={patient.id}>
                {patient.name || `환자 ${index + 1}`}
                {patient.dementia_stage ? ` · ${patient.dementia_stage}` : ''}
              </ThemedText>
            ))
          )}
          {visit ? (
            <ThemedText themeColor="textSecondary">
              다음 진료: {visit.visit.visit_date} {visit.name}
              {visit.visit.hospital_name ? ` · ${visit.visit.hospital_name}` : ''}
            </ThemedText>
          ) : null}
          <Button label="내 정보 보기" variant="secondary" onPress={() => router.navigate('/my')} />
        </Card>
      ) : null}

      <Card>
        <ThemedText type="subtitle">인지기능 자가점검</ThemedText>
        {/* 점검 결과는 아직 저장하지 않아(저장 API 미구현) 최근 결과는 보여 주지 않는다 */}
        <ThemedText themeColor="textSecondary">기억력과 생활 변화를 15개 문항으로 점검해 보세요.</ThemedText>
        <Button label="점검하기" variant="secondary" onPress={() => router.navigate('/check')} />
      </Card>

      <Card>
        <ThemedText type="subtitle">오늘의 돌봄 기록</ThemedText>
        <ThemedText themeColor="textSecondary">오늘 돌보시는 분의 상태를 기록해 보세요.</ThemedText>
        {/* 돌봄 기록 화면은 아직 없다 */}
        <Button label="기록하기 (준비 중)" variant="secondary" disabled />
      </Card>

      <Card>
        <ThemedText type="subtitle">궁금한 점을 물어보세요</ThemedText>
        <Button label="챗봇에게 묻기" variant="secondary" onPress={() => router.navigate('/chat')} />
      </Card>
    </Screen>
  );
}

const styles = StyleSheet.create({
  section: {
    gap: Spacing.two,
  },
});
