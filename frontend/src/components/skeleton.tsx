import { useEffect, useState } from 'react';
import { Animated, StyleSheet, View, type DimensionValue } from 'react-native';

import { Card } from '@/components/ui';
import { Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

/**
 * 불러오는 동안 보여 주는 카드 모양 뼈대(스켈레톤). 빙글빙글 표시보다 기다리는 시간이 짧게 느껴진다.
 * 블록들이 함께 천천히 밝아졌다 어두워진다.
 */
function usePulse() {
  const [opacity] = useState(() => new Animated.Value(0.45));
  useEffect(() => {
    const loop = Animated.loop(
      Animated.sequence([
        Animated.timing(opacity, { toValue: 1, duration: 700, useNativeDriver: true }),
        Animated.timing(opacity, { toValue: 0.45, duration: 700, useNativeDriver: true }),
      ]),
    );
    loop.start();
    return () => loop.stop();
  }, [opacity]);
  return opacity;
}

function Block({ width, height, opacity }: { width: DimensionValue; height: number; opacity: Animated.Value }) {
  const theme = useTheme();
  return <Animated.View style={[styles.block, { width, height, opacity, backgroundColor: theme.border }]} />;
}

/** 피드 카드(PaperCard)와 같은 배치의 뼈대 */
export function SkeletonPaperCard() {
  const opacity = usePulse();
  return (
    <Card style={styles.card} accessible accessibilityLabel="연구를 불러오는 중">
      <Block width="18%" height={16} opacity={opacity} />
      <View style={styles.lines}>
        <Block width="92%" height={22} opacity={opacity} />
        <Block width="60%" height={22} opacity={opacity} />
      </View>
      <View style={styles.lines}>
        <Block width="100%" height={16} opacity={opacity} />
        <Block width="100%" height={16} opacity={opacity} />
        <Block width="75%" height={16} opacity={opacity} />
      </View>
      <Block width="40%" height={14} opacity={opacity} />
    </Card>
  );
}

/** 카드 여러 장 */
export function SkeletonPaperList({ count = 3 }: { count?: number }) {
  return (
    <>
      {Array.from({ length: count }, (_, i) => (
        <SkeletonPaperCard key={i} />
      ))}
    </>
  );
}

const styles = StyleSheet.create({
  card: {
    gap: Spacing.three,
  },
  lines: {
    gap: Spacing.two,
  },
  block: {
    borderRadius: 2,
  },
});
