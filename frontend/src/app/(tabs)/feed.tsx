import { useCallback, useState } from 'react';
import { ScrollView, StyleSheet } from 'react-native';

import { currentUserId, getRecommendations, getUserProfile } from '@/api';
import { PaperCard } from '@/components/paper-card';
import { SkeletonPaperList } from '@/components/skeleton';
import { ThemedText } from '@/components/themed-text';
import { Chip, ErrorView, Screen } from '@/components/ui';
import { CategoryLabel } from '@/constants/labels';
import { Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import { useTheme } from '@/hooks/use-theme';
import type { Category } from '@/types';
import { feedCaption, fewItemsNotice } from '@/utils/feed';

const categoryOrder: Category[] = ['treatment', 'care', 'prevention', 'diagnosis'];

export default function FeedScreen() {
  const theme = useTheme();
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
  const notice = data && !feed.loading ? fewItemsNotice(data) : null;

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

      {/* 피드 대기 문구 (10/14 회의): AI 피드는 최대 15초, 서버를 켠 뒤 첫 검색은 30초까지 걸린다 */}
      {feed.error && <ErrorView message={feed.error.message} onRetry={() => feed.reload()} />}
      {feed.loading && data === null && (
        <>
          <ThemedText type="bold">맞춤 연구를 고르고 있어요</ThemedText>
          <ThemedText type="small" themeColor="textSecondary">
            돌보시는 분의 상황에 맞는 연구를 찾는 중이에요. 처음에는 30초쯤 걸릴 수 있으니 잠시만 기다려 주세요.
          </ThemedText>
          <SkeletonPaperList />
        </>
      )}
      {feed.loading && data !== null && (
        <ThemedText type="small" themeColor="textSecondary">
          추천을 다시 불러오고 있어요. 새로 만들 때는 15초쯤 걸릴 수 있어요.
        </ThemedText>
      )}

      {/* AI가 5편보다 적게 고른 경우 (최대 5편, 관련성이 낮은 논문으로 채우지 않는다) */}
      {notice ? (
        <ThemedText type="small" style={[styles.notice, { borderLeftColor: theme.primary }]}>
          {notice}
        </ThemedText>
      ) : null}

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
  notice: {
    borderLeftWidth: 3,
    paddingLeft: Spacing.two,
  },
});
