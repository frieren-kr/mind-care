import { useCallback, useState } from 'react';
import { ScrollView, StyleSheet } from 'react-native';

import { currentUserId, getRecommendations, getUserProfile } from '@/api';
import { PaperCard } from '@/components/paper-card';
import { ThemedText } from '@/components/themed-text';
import { Chip, ErrorView, LoadingView, Screen } from '@/components/ui';
import { CategoryLabel } from '@/constants/labels';
import { Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import type { Category, RecommendationResponse } from '@/types';

const categoryOrder: Category[] = ['treatment', 'care', 'prevention', 'diagnosis'];

/** "오늘 만든 추천" / "3일 전에 만든 추천" (날짜 기준) */
function generatedLabel(iso: string): string | null {
  const created = new Date(iso);
  if (Number.isNaN(created.getTime())) return null;
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((startOfDay(new Date()) - startOfDay(created)) / 86_400_000);
  return days <= 0 ? '오늘 만든 맞춤 추천' : `${days}일 전에 만든 맞춤 추천`;
}

function feedCaption(feed: RecommendationResponse): string {
  if (feed.mode === 'search') return '돌보시는 분의 증상·관심사와 가까운 연구예요';
  return (feed.generated_at && generatedLabel(feed.generated_at)) || '맞춤 추천 연구예요';
}

export default function FeedScreen() {
  // null = 관리 환자 전체 기준
  const [patientId, setPatientId] = useState<string | null>(null);
  const [filter, setFilter] = useState<Category | 'all'>('all');

  const loadProfile = useCallback(() => getUserProfile(currentUserId()), []);
  const profile = useApi(loadProfile);
  const loadFeed = useCallback(() => getRecommendations(currentUserId(), { patientId }), [patientId]);
  const feed = useApi(loadFeed);

  const refresh = () =>
    feed.reload(() => getRecommendations(currentUserId(), { patientId, refresh: true }));

  const patients = profile.data?.patients ?? [];
  const data = feed.data;
  // category는 AI 추천(mode="ai")에만 있다. 검색 방식이면 필터를 숨긴다
  const categories = categoryOrder.filter((c) => data?.items.some((i) => i.category === c));
  const activeFilter = filter !== 'all' && categories.includes(filter) ? filter : 'all';
  const items = (data?.items ?? []).filter((i) => activeFilter === 'all' || i.category === activeFilter);

  return (
    <Screen refreshing={feed.loading && data !== null} onRefresh={refresh}>
      {patients.length >= 2 && (
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chips}>
          <Chip label="모든 분" selected={patientId === null} onPress={() => setPatientId(null)} />
          {patients.map(({ patient }, index) => (
            <Chip
              key={patient.id}
              label={patient.name || `환자 ${index + 1}`}
              selected={patientId === patient.id}
              onPress={() => setPatientId(patient.id)}
            />
          ))}
        </ScrollView>
      )}

      {data && (
        <ThemedText type="smallBold" themeColor="textSecondary">
          {feedCaption(data)}
        </ThemedText>
      )}
      {data?.mode === 'ai' && patientId !== null && (
        <ThemedText type="small" themeColor="textSecondary">
          맞춤 추천은 돌보시는 모든 분을 함께 고려해서 만들어요.
        </ThemedText>
      )}

      {categories.length > 1 && (
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chips}>
          <Chip label="전체" selected={activeFilter === 'all'} onPress={() => setFilter('all')} />
          {categories.map((c) => (
            <Chip key={c} label={CategoryLabel[c]} selected={activeFilter === c} onPress={() => setFilter(c)} />
          ))}
        </ScrollView>
      )}

      {feed.error && <ErrorView message={feed.error.message} onRetry={() => feed.reload()} />}
      {feed.loading && data === null && <LoadingView message="맞춤 연구를 찾고 있어요…" />}

      {items.map((item) => (
        <PaperCard key={item.id} item={item} />
      ))}
      {data && !feed.loading && items.length === 0 && (
        <ThemedText themeColor="textSecondary">아직 추천할 연구를 찾지 못했어요</ThemedText>
      )}
    </Screen>
  );
}

const styles = StyleSheet.create({
  chips: {
    gap: Spacing.two,
  },
});
