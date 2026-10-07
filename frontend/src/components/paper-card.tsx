import { Link } from 'expo-router';
import { Pressable, StyleSheet, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Card, EvidenceBadge } from '@/components/ui';
import { Radius, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import { CategoryLabel } from '@/mocks/feed';
import type { FeedItem } from '@/types';

/** 피드 카드. 💡 개인화 설명 줄이 마인드 케어의 핵심 차별점이라 눈에 띄게 둔다. */
export function PaperCard({ item }: { item: FeedItem }) {
  const theme = useTheme();
  return (
    <Link href={{ pathname: '/paper/[id]', params: { id: item.id } }} asChild>
      <Pressable accessibilityRole="button" accessibilityLabel={item.title}>
        {({ pressed }) => (
          <Card style={pressed && styles.pressed}>
            <View style={styles.meta}>
              <ThemedText type="smallBold" themeColor="textSecondary">
                {CategoryLabel[item.category]}
              </ThemedText>
              <EvidenceBadge level={item.evidence_level} />
            </View>

            <ThemedText type="subtitle">{item.title}</ThemedText>

            {item.summary_bullets.map((b) => (
              <ThemedText key={b} themeColor="textSecondary">
                • {b}
              </ThemedText>
            ))}

            <View style={[styles.reason, { backgroundColor: theme.highlight }]}>
              <ThemedText type="small">💡 {item.personal_reason}</ThemedText>
            </View>

            <View style={styles.meta}>
              <ThemedText type="small" themeColor="textSecondary">
                {item.journal} · {item.read_minutes}분
              </ThemedText>
              <ThemedText
                type="small"
                accessibilityLabel={item.is_bookmarked ? '저장됨' : '저장 안 됨'}>
                {item.is_bookmarked ? '🔖 저장됨' : '🔖'}
              </ThemedText>
            </View>
          </Card>
        )}
      </Pressable>
    </Link>
  );
}

const styles = StyleSheet.create({
  meta: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: Spacing.two,
  },
  reason: {
    borderRadius: Radius.card / 2,
    padding: Spacing.two,
  },
  pressed: {
    opacity: 0.8,
  },
});
