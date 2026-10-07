import { useState } from 'react';
import { Linking, StyleSheet, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Button, Card, Chip, Disclaimer, Screen } from '@/components/ui';
import { Radius, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import { mockQuestions } from '@/mocks/user';

// 문항과 점수 기준은 임시값이다. 검증된 선별 도구가 정해지면 교체한다 (설계서 11장 #1).
export default function SelfCheckScreen() {
  const theme = useTheme();
  const [index, setIndex] = useState(0);
  const [answers, setAnswers] = useState<Record<string, number>>({});
  const [done, setDone] = useState(false);

  const q = mockQuestions[index];
  const total = mockQuestions.length;

  const restart = () => {
    setAnswers({});
    setIndex(0);
    setDone(false);
  };

  if (done) {
    const score = Object.values(answers).reduce((a, b) => a + b, 0);
    const visit = score >= total;
    return (
      <Screen>
        <Card>
          <ThemedText type="title">{visit ? '전문 검사를 권해요' : '지금은 양호해요'}</ThemedText>
          <ThemedText themeColor="textSecondary">
            점수 {score} / {total * 2}
          </ThemedText>
          <ThemedText>
            {visit
              ? '가까운 치매안심센터에서 무료로 정확한 검사를 받아보세요.'
              : '한 달 뒤에 다시 점검해 보세요.'}
          </ThemedText>
        </Card>
        {visit && (
          <Button label="📞 치매상담콜센터 1899-9988" onPress={() => Linking.openURL('tel:18999988')} />
        )}
        <Button label="다시 점검하기" variant="secondary" onPress={restart} />
        <Disclaimer text="자가점검은 진단이 아닌 선별 참고용입니다." />
      </Screen>
    );
  }

  return (
    <Screen>
      <ThemedText type="smallBold" themeColor="textSecondary">
        {index + 1} / {total}
      </ThemedText>
      <View style={[styles.progressTrack, { backgroundColor: theme.border }]}>
        <View
          style={[
            styles.progressFill,
            { backgroundColor: theme.primary, width: `${((index + 1) / total) * 100}%` },
          ]}
        />
      </View>

      <ThemedText type="title">{q.text}</ThemedText>

      <View style={styles.options}>
        {q.options.map((o) => (
          <Chip
            key={o.label}
            label={o.label}
            selected={answers[q.id] === o.score}
            onPress={() => setAnswers({ ...answers, [q.id]: o.score })}
          />
        ))}
      </View>

      <View style={styles.nav}>
        <View style={styles.navButton}>
          <Button
            label="이전"
            variant="secondary"
            disabled={index === 0}
            onPress={() => setIndex(index - 1)}
          />
        </View>
        <View style={styles.navButton}>
          <Button
            label={index === total - 1 ? '결과 보기' : '다음'}
            disabled={answers[q.id] === undefined}
            onPress={() => (index === total - 1 ? setDone(true) : setIndex(index + 1))}
          />
        </View>
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  progressTrack: {
    height: 8,
    borderRadius: Radius.pill,
    overflow: 'hidden',
  },
  progressFill: {
    height: '100%',
  },
  options: {
    gap: Spacing.two,
  },
  nav: {
    flexDirection: 'row',
    gap: Spacing.two,
  },
  navButton: {
    flex: 1,
  },
});
