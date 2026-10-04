import { useState } from 'react';
import { ScrollView, StyleSheet } from 'react-native';

import { PaperCard } from '@/components/paper-card';
import { ThemedText } from '@/components/themed-text';
import { Chip, Screen } from '@/components/ui';
import { Spacing } from '@/constants/theme';
import { CategoryLabel, mockFeed } from '@/mocks/feed';
import type { Category } from '@/types';

const filters: (Category | 'all')[] = ['all', 'treatment', 'care', 'prevention', 'diagnosis'];

export default function FeedScreen() {
  const [filter, setFilter] = useState<Category | 'all'>('all');
  const items = mockFeed.items.filter((i) => filter === 'all' || i.category === filter);

  return (
    <Screen>
      <ThemedText type="smallBold" themeColor="textSecondary">
        {mockFeed.week_range}
      </ThemedText>

      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.filters}>
        {filters.map((f) => (
          <Chip
            key={f}
            label={f === 'all' ? '전체' : CategoryLabel[f]}
            selected={filter === f}
            onPress={() => setFilter(f)}
          />
        ))}
      </ScrollView>

      {items.map((item) => (
        <PaperCard key={item.id} item={item} />
      ))}
      {items.length === 0 && (
        <ThemedText themeColor="textSecondary">이번 주에는 이 분야의 새 연구가 없어요</ThemedText>
      )}
    </Screen>
  );
}

const styles = StyleSheet.create({
  filters: {
    gap: Spacing.two,
  },
});
