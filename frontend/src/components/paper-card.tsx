import { Link } from 'expo-router';
import { Pressable, StyleSheet, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Card, EvidenceBadge } from '@/components/ui';
import { CategoryLabel } from '@/constants/labels';
import { Radius, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import { displayTitle, type FeedItem } from '@/types';

/** 피드 카드. 💡 개인화 설명 줄이 마인드 케어의 핵심 차별점이라 눈에 띄게 둔다. */
export function PaperCard({ item }: { item: FeedItem }) {
  const theme = useTheme();
  const meta = [item.journal, item.published_at].filter(Boolean).join(' · ');
  // personal_reason은 추천 응답에만 있어서, 상세 화면 '우리 상황에서는?'에 쓰도록 같이 넘긴다
  const params = item.personal_reason ? { id: item.id, reason: item.personal_reason } : { id: item.id };

  return (
    <Link href={{ pathname: '/paper/[id]', params }} asChild>
      <Pressable accessibilityRole="button" accessibilityLabel={displayTitle(item)}>
        {({ pressed }) => (
          <Card style={pressed && styles.pressed}>
            {item.category || item.evidence_grade ? (
              <View style={styles.meta}>
                <ThemedText type="smallBold" themeColor="textSecondary">
                  {item.category ? CategoryLabel[item.category] : ''}
                </ThemedText>
                {item.evidence_grade ? <EvidenceBadge level={item.evidence_grade} /> : null}
              </View>
            ) : null}

            <ThemedText type="subtitle">{displayTitle(item)}</ThemedText>

            {/* 카드에는 요약 3칸 중 '새로 밝혀진 것'만. 3칸 전체는 상세 화면에서 보여준다 */}
            {item.summary_finding ? (
              <ThemedText themeColor="textSecondary" numberOfLines={3}>
                {item.summary_finding}
              </ThemedText>
            ) : null}

            {item.personal_reason ? (
              <View style={[styles.reason, { backgroundColor: theme.highlight }]}>
                <ThemedText type="small">💡 {item.personal_reason}</ThemedText>
              </View>
            ) : null}

            {meta ? (
              <ThemedText type="small" themeColor="textSecondary">
                {meta}
              </ThemedText>
            ) : null}
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
