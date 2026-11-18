import { useCallback, useState } from 'react';
import { Linking, Pressable, StyleSheet, View } from 'react-native';

import { createAssessment, currentUserId, getUserProfile } from '@/api';
import { toApiError } from '@/api/client';
import { ThemedText } from '@/components/themed-text';
import { Button, Card, Chip, Disclaimer, Screen, SectionLabel } from '@/components/ui';
import {
  KDSQ_C_DOMAINS,
  KDSQ_C_INSTRUCTION,
  KDSQ_C_MAX_SCORE,
  KDSQ_C_QUESTIONS,
  KDSQ_C_REFERRAL_SCORE,
  KDSQ_C_SOURCE,
} from '@/constants/kdsq-c';
import { Radius, Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import { useTheme } from '@/hooks/use-theme';
import { isoDate } from '@/utils/date';

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

/** 영역별 소계 표. 영역별 판정 기준은 없어서 점수와 막대만 보여 준다 */
function DomainScores({ answers }: { answers: Record<string, number> }) {
  const theme = useTheme();
  return (
    <Card>
      <ThemedText type="subtitle">영역별 점수</ThemedText>
      {KDSQ_C_DOMAINS.map((domain) => {
        const score = domain.questionIds.reduce((sum, id) => sum + (answers[id] ?? 0), 0);
        const max = domain.questionIds.length * 2;
        return (
          <View
            key={domain.label}
            style={[styles.domainRow, { borderTopColor: theme.border }]}
            accessible
            accessibilityLabel={`${domain.label} ${score}점, ${max}점 만점`}>
            <View style={styles.domainHeader}>
              <View style={styles.flex}>
                <ThemedText type="bold">{domain.label}</ThemedText>
                <ThemedText type="small" themeColor="textSecondary">
                  {domain.description}
                </ThemedText>
              </View>
              <ThemedText type="bold">
                {score} / {max}점
              </ThemedText>
            </View>
            <View style={[styles.progressTrack, { backgroundColor: theme.border }]}>
              <View style={[styles.progressFill, { backgroundColor: theme.primary, width: `${(score / max) * 100}%` }]} />
            </View>
          </View>
        );
      })}
      <ThemedText type="small" themeColor="textSecondary">
        영역별 점수는 어느 쪽 변화가 큰지 보기 위한 참고용이에요. 정밀검사 권유는 총점으로 판단해요.
      </ThemedText>
    </Card>
  );
}

/** 영역별 소계를 한 줄로 (검사 기록의 세부 결과) — '기억력 3/8 · 언어 1/4 …' */
function domainSummary(answers: Record<string, number>): string {
  return KDSQ_C_DOMAINS.map((domain) => {
    const score = domain.questionIds.reduce((sum, id) => sum + (answers[id] ?? 0), 0);
    return `${domain.label} ${score}/${domain.questionIds.length * 2}`;
  }).join(' · ');
}

/**
 * 결과를 돌보는 분의 검사 기록(KDSQ-C)으로 저장. 저장하면 챗봇 검사 기록 Tool·홈 '오늘의 돌봄'·AI 피드가 읽는다.
 * 같은 날 여러 번 저장해도 따로 남는다 (가장 최근 것이 최신 결과)
 */
function SaveResult({ score, answers }: { score: number; answers: Record<string, number> }) {
  const theme = useTheme();
  const loadProfile = useCallback(() => getUserProfile(currentUserId()), []);
  const { data: profile } = useApi(loadProfile);
  const patients = profile?.patients ?? [];
  const [chosen, setChosen] = useState<string | null>(null);
  const patientId = chosen ?? (patients.length === 1 ? patients[0].patient.id : null);
  const [saving, setSaving] = useState(false);
  const [savedFor, setSavedFor] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  if (patients.length === 0) return null;
  const saved = patientId !== null && savedFor.includes(patientId);

  const save = async () => {
    if (!patientId || saving || saved) return;
    setSaving(true);
    setError(null);
    try {
      await createAssessment(currentUserId(), patientId, {
        assessment_type: 'KDSQ-C',
        score,
        assessed_at: isoDate(),
        assessed_by: '보호자 자가점검 (앱)',
        result_detail: domainSummary(answers),
      });
      setSavedFor((list) => [...list, patientId]);
    } catch (e) {
      setError(toApiError(e).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card>
      <ThemedText type="subtitle">결과 남겨 두기</ThemedText>
      <ThemedText themeColor="textSecondary">
        기록해 두면 다음 점검과 비교할 수 있고, 챗봇도 이 결과를 참고해서 답해요.
      </ThemedText>
      {patients.length > 1 ? (
        <View style={styles.wrap}>
          {patients.map(({ patient }, index) => (
            <Chip
              key={patient.id}
              label={patient.name || `환자 ${index + 1}`}
              selected={patient.id === patientId}
              onPress={() => setChosen(patient.id)}
            />
          ))}
        </View>
      ) : null}
      {error ? (
        <ThemedText accessibilityRole="alert" style={{ color: theme.accent }}>
          {error}
        </ThemedText>
      ) : null}
      {saved ? (
        <ThemedText accessibilityRole="alert" type="bold">
          저장했어요.
        </ThemedText>
      ) : (
        <Button
          label={saving ? '저장하고 있어요…' : patientId ? '이 결과 저장하기' : '누구의 점검인지 골라 주세요'}
          onPress={save}
          disabled={saving || !patientId}
        />
      )}
    </Card>
  );
}

type Step = 'intro' | 'questions' | 'result';

/** 인지기능 자가점검 (KDSQ-C 15문항). 결과 화면에서 돌보는 분의 검사 기록으로 저장할 수 있다 */
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
              borderTopColor: suspected ? theme.accent : theme.rule,
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
        <DomainScores answers={answers} />
        <SaveResult score={score} answers={answers} />
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
      <ThemedText type="headline">{q.text}</ThemedText>
      {q.note ? (
        <ThemedText type="small" themeColor="textSecondary">
          ({q.note})
        </ThemedText>
      ) : null}

      {/* 보기는 화면 너비만큼 큰 줄로 (어르신도 누르기 쉽게). 고른 줄은 먹색으로 채운다 */}
      <View style={[styles.options, { borderTopColor: theme.rule }]} accessibilityRole="radiogroup">
        {q.options.map((o) => {
          const selected = answers[q.id] === o.score;
          return (
            <Pressable
              key={o.label}
              accessibilityRole="radio"
              accessibilityState={{ checked: selected }}
              onPress={() => setAnswers({ ...answers, [q.id]: o.score })}
              style={({ pressed }) => [
                styles.option,
                { borderBottomColor: theme.border },
                selected
                  ? { backgroundColor: theme.primary }
                  : pressed && { backgroundColor: theme.backgroundSelected },
              ]}>
              <View
                style={[
                  styles.radio,
                  { borderColor: selected ? theme.onPrimary : theme.rule },
                  selected && { backgroundColor: theme.onPrimary },
                ]}
              />
              <ThemedText type="bold" style={[styles.flex, selected && { color: theme.onPrimary }]}>
                {o.label}
              </ThemedText>
              <ThemedText type="small" style={{ color: selected ? theme.onPrimary : theme.textSecondary }}>
                {o.score}점
              </ThemedText>
            </Pressable>
          );
        })}
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
    borderTopWidth: 1,
  },
  option: {
    minHeight: 56,
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.three,
    paddingHorizontal: Spacing.three,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  radio: {
    width: 18,
    height: 18,
    borderRadius: Radius.pill,
    borderWidth: 2,
  },
  nav: {
    flexDirection: 'row',
    gap: Spacing.two,
  },
  navButton: {
    flex: 1,
  },
  result: {
    borderTopWidth: 3,
    padding: Spacing.three,
    gap: Spacing.two,
  },
  notice: {
    gap: Spacing.two,
  },
  domainRow: {
    borderTopWidth: StyleSheet.hairlineWidth,
    paddingTop: Spacing.two,
    gap: Spacing.one,
  },
  domainHeader: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: Spacing.two,
  },
  flex: {
    flex: 1,
  },
  wrap: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: Spacing.two,
  },
});
