import { Link } from 'expo-router';
import { Pressable, StyleSheet, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { EvidenceBadge } from '@/components/ui';
import { CategoryLabel } from '@/constants/labels';
import { Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import { displayTitle, type FeedItem } from '@/types';
import { formatDate } from '@/utils/date';

/**
 * 피드 기사 한 편 (신문 지면 테마: 상자 대신 아래 가는 선으로 나눈다).
 * 개인화 설명(personal_reason)이 마인드 케어의 핵심 차별점이라 '우리 상황에서' 블록으로 눈에 띄게 둔다.
 */
export function PaperCard({ item }: { item: FeedItem }) {
  const theme = useTheme();
  // personal_reason은 추천 응답에만 있어서, 상세 화면 '우리 상황에서는?'에 쓰도록 같이 넘긴다
  const params = item.personal_reason ? { id: item.id, reason: item.personal_reason } : { id: item.id };
  const meta = [item.journal, formatDate(item.published_at)].filter(Boolean).join(' · ');

  return (
    <Link href={{ pathname: '/paper/[id]', params }} asChild>
      <Pressable accessibilityRole="button" accessibilityLabel={displayTitle(item)}>
        {({ pressed }) => (
          <View
            style={[
              styles.card,
              { borderBottomColor: theme.border },
              pressed && { backgroundColor: theme.backgroundSelected },
            ]}>
            {item.category || item.evidence_grade ? (
              <View style={styles.kicker}>
                {item.category ? <ThemedText type="smallBold">{CategoryLabel[item.category]}</ThemedText> : null}
                {item.evidence_grade ? <EvidenceBadge level={item.evidence_grade} /> : null}
              </View>
            ) : null}

            <ThemedText type="headline">{displayTitle(item)}</ThemedText>

            {/* 카드에는 요약 3칸 중 '새로 밝혀진 것'만. 3칸 전체는 상세 화면에서 보여준다 */}
            {item.summary_finding ? (
              <ThemedText themeColor="textSecondary" numberOfLines={3}>
                {item.summary_finding}
              </ThemedText>
            ) : null}

            {/* 추천 이유는 2~3문장이라 카드에서는 3줄까지만. 전체는 상세 화면 '우리 상황에서는?' */}
            {item.personal_reason ? (
              <View style={[styles.reason, { backgroundColor: theme.highlight }]}>
                <ThemedText type="smallBold" themeColor="accent">
                  우리 상황에서
                </ThemedText>
                <ThemedText type="small" numberOfLines={3}>
                  {item.personal_reason}
                </ThemedText>
              </View>
            ) : null}

            {meta ? (
              <ThemedText type="small" themeColor="textSecondary" numberOfLines={1}>
                {meta}
              </ThemedText>
            ) : null}
          </View>
        )}
      </Pressable>
    </Link>
  );
}

const styles = StyleSheet.create({
  card: {
    gap: Spacing.two,
    paddingVertical: Spacing.three,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  kicker: {
    flexDirection: 'row',
    alignItems: 'center',
    flexWrap: 'wrap',
    gap: Spacing.two,
  },
  reason: {
    gap: Spacing.one,
    paddingVertical: Spacing.two,
    paddingHorizontal: Spacing.three,
  },
});
