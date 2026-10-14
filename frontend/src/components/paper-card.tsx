import { Link } from 'expo-router';
import { Pressable, StyleSheet, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Card, EvidenceBadge } from '@/components/ui';
import { Radius, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import { CategoryLabel } from '@/mocks/feed';
import { displayTitle, type FeedItem } from '@/types';

/** 피드 카드. 💡 개인화 설명 줄이 마인드 케어의 핵심 차별점이라 눈에 띄게 둔다. */
export function PaperCard({ item }: { item: FeedItem }) {
  const theme = useTheme();
  return (
    <Link href={{ pathname: '/paper/[id]', params: { id: item.id } }} asChild>
      <Pressable accessibilityRole="button" accessibilityLabel={displayTitle(item)}>
        {({ pressed }) => (
          <Card style={pressed && styles.pressed}>
            <View style={styles.meta}>
              <ThemedText type="smallBold" themeColor="textSecondary">
                {CategoryLabel[item.category]}
              </ThemedText>
              {item.evidence_grade ? <EvidenceBadge level={item.evidence_grade} /> : null}
            </View>

            <ThemedText type="subtitle">{displayTitle(item)}</ThemedText>

            {/* 카드에는 요약 3칸 중 '새로 밝혀진 것'만. 3칸 전체는 상세 화면에서 보여준다 */}
            {item.summary_finding ? (
              <ThemedText themeColor="textSecondary">{item.summary_finding}</ThemedText>
            ) : null}

            {item.personal_reason ? (
              <View style={[styles.reason, { backgroundColor: theme.highlight }]}>
                <ThemedText type="small">💡 {item.personal_reason}</ThemedText>
              </View>
            ) : null}

            <ThemedText type="small" themeColor="textSecondary">
              {item.journal}
            </ThemedText>
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
