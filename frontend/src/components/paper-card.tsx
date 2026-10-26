import { Link } from 'expo-router';
import { Pressable, StyleSheet, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Card, EvidenceBadge } from '@/components/ui';
import { CategoryLabel } from '@/constants/labels';
import { Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import { displayTitle, type FeedItem } from '@/types';

/** 피드 카드. 개인화 설명(personal_reason)이 마인드 케어의 핵심 차별점이라 강조 띠로 눈에 띄게 둔다. */
export function PaperCard({ item }: { item: FeedItem }) {
  const theme = useTheme();
  // personal_reason은 추천 응답에만 있어서, 상세 화면 '우리 상황에서는?'에 쓰도록 같이 넘긴다
  const params = item.personal_reason ? { id: item.id, reason: item.personal_reason } : { id: item.id };

  return (
    <Link href={{ pathname: '/paper/[id]', params }} asChild>
      <Pressable accessibilityRole="button" accessibilityLabel={displayTitle(item)}>
        {({ pressed }) => (
          <Card style={[styles.card, pressed && { backgroundColor: theme.backgroundSelected }]}>
            {item.category || item.evidence_grade ? (
              <View style={styles.meta}>
                {item.category ? (
                  <ThemedText type="smallBold" themeColor="primary">
                    {CategoryLabel[item.category]}
                  </ThemedText>
                ) : null}
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

            {/* 추천 이유는 2~3문장이라 카드에서는 2줄까지만. 전체는 상세 화면 '우리 상황에서는?' */}
            {item.personal_reason ? (
              <View style={[styles.reason, { backgroundColor: theme.highlight, borderLeftColor: theme.accent }]}>
                <ThemedText type="small" numberOfLines={2}>
                  {item.personal_reason}
                </ThemedText>
              </View>
            ) : null}

            <View style={[styles.footer, { borderTopColor: theme.border }]}>
              <ThemedText type="small" themeColor="textSecondary" numberOfLines={1} style={styles.journal}>
                {item.journal}
              </ThemedText>
              {item.published_at ? (
                <ThemedText type="small" themeColor="textSecondary">
                  {item.published_at}
                </ThemedText>
              ) : null}
            </View>
          </Card>
        )}
      </Pressable>
    </Link>
  );
}

const styles = StyleSheet.create({
  card: {
    gap: Spacing.three,
  },
  meta: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
  },
  reason: {
    borderLeftWidth: 3,
    paddingVertical: Spacing.two,
    paddingHorizontal: Spacing.three,
  },
  footer: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.three,
    borderTopWidth: StyleSheet.hairlineWidth,
    paddingTop: Spacing.two,
  },
  journal: {
    flex: 1,
  },
});
