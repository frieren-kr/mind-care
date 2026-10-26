import { useState } from 'react';
import { Linking, StyleSheet, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Button, Card, Chip, Disclaimer, Screen, SectionLabel } from '@/components/ui';
import {
  KDSQ_C_INSTRUCTION,
  KDSQ_C_MAX_SCORE,
  KDSQ_C_QUESTIONS,
  KDSQ_C_REFERRAL_SCORE,
  KDSQ_C_SOURCE,
} from '@/constants/kdsq-c';
import { Radius, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

const MEDICAL_DISCLAIMER =
  '이 점검은 치매를 진단하는 검사가 아니라, 인지기능 저하가 의심되는지 살펴보는 선별 참고용입니다. ' +
  '결과와 관계없이 걱정되는 변화가 있으면 의사와 상담하거나 치매안심센터에서 정밀검사를 받아 보세요.';

/** 출처와 의료 면책. 시작·결과 화면에 같이 둔다 */
function SourceNotice() {
  return (
    <View style={styles.notice}>
      <Disclaimer text={MEDICAL_DISCLAIMER} />
      <Disclaimer text={`출처: ${KDSQ_C_SOURCE}`} />
    </View>
  );
}

type Step = 'intro' | 'questions' | 'result';

/** 인지기능 자가점검 (KDSQ-C 15문항). 결과는 아직 저장하지 않는다 (저장 API 미구현) */
export default function SelfCheckScreen() {
  const theme = useTheme();
  const [step, setStep] = useState<Step>('intro');
  const [index, setIndex] = useState(0);
  const [answers, setAnswers] = useState<Record<string, number>>({});

  const total = KDSQ_C_QUESTIONS.length;
  const q = KDSQ_C_QUESTIONS[index];

  const restart = () => {
    setAnswers({});
    setIndex(0);
    setStep('intro');
  };

  if (step === 'intro') {
    return (
      <Screen>
        <SectionLabel>인지기능 자가점검</SectionLabel>
        <ThemedText type="title">기억력과 생활 변화를 점검해 보세요</ThemedText>
        <ThemedText themeColor="textSecondary">
          {total}개 문항에 답하면 인지기능 저하가 의심되는지 알려 드려요.
        </ThemedText>
        <Card>
          <ThemedText type="smallBold">작성 방법</ThemedText>
          <ThemedText>{KDSQ_C_INSTRUCTION}</ThemedText>
        </Card>
        <Button label="점검 시작하기" onPress={() => setStep('questions')} />
        <SourceNotice />
      </Screen>
    );
  }

  if (step === 'result') {
    const score = Object.values(answers).reduce((a, b) => a + b, 0);
    const suspected = score >= KDSQ_C_REFERRAL_SCORE;
    return (
      <Screen>
        <SectionLabel>점검 결과</SectionLabel>
        <View
          style={[
            styles.result,
            {
              backgroundColor: suspected ? theme.highlight : theme.backgroundElement,
              borderLeftColor: suspected ? theme.accent : theme.primary,
            },
          ]}>
          <ThemedText type="title">
            {suspected ? '인지기능 저하가 의심돼요' : '인지기능 저하 의심 기준보다 낮아요'}
          </ThemedText>
          <ThemedText type="bold">
            {score}점 / {KDSQ_C_MAX_SCORE}점
          </ThemedText>
          <ThemedText>
            {suspected
              ? `${KDSQ_C_REFERRAL_SCORE}점 이상이면 정밀검사를 권해요. 가까운 치매안심센터에서 무료로 검사를 받을 수 있어요.`
              : '1년 전과 비교해 새로운 변화가 느껴지면 다시 점검해 보세요.'}
          </ThemedText>
        </View>
        {suspected && (
          <Button label="치매상담콜센터 1899-9988 전화하기" onPress={() => Linking.openURL('tel:18999988')} />
        )}
        <Button label="처음부터 다시 하기" variant="secondary" onPress={restart} />
        <SourceNotice />
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

      <ThemedText type="small" themeColor="textSecondary">
        1년 전과 비교해 지금은 어떤가요?
      </ThemedText>
      <ThemedText type="title">{q.text}</ThemedText>
      {q.note ? (
        <ThemedText type="small" themeColor="textSecondary">
          ({q.note})
        </ThemedText>
      ) : null}

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
            onPress={() => (index === 0 ? setStep('intro') : setIndex(index - 1))}
          />
        </View>
        <View style={styles.navButton}>
          <Button
            label={index === total - 1 ? '결과 보기' : '다음'}
            disabled={answers[q.id] === undefined}
            onPress={() => (index === total - 1 ? setStep('result') : setIndex(index + 1))}
          />
        </View>
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  progressTrack: {
    height: 6,
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
  result: {
    borderLeftWidth: 4,
    padding: Spacing.three,
    gap: Spacing.two,
  },
  notice: {
    gap: Spacing.two,
  },
});
