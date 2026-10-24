import { router, useLocalSearchParams } from 'expo-router';
import * as WebBrowser from 'expo-web-browser';
import { useCallback } from 'react';
import { StyleSheet, View } from 'react-native';

import { getPaper } from '@/api';
import { ThemedText } from '@/components/themed-text';
import { Button, Card, Disclaimer, ErrorView, EvidenceBadge, LoadingView, Screen } from '@/components/ui';
import { Radius, Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import { useTheme } from '@/hooks/use-theme';
import { displayTitle } from '@/types';

export default function PaperDetailScreen() {
  // reason: 피드 카드에서 들어올 때만 있는 personal_reason (GET /papers 응답에는 없다)
  const { id, reason } = useLocalSearchParams<{ id: string; reason?: string }>();
  const theme = useTheme();
  const loadPaper = useCallback(() => getPaper(id), [id]);
  const { data: paper, error, loading, reload } = useApi(loadPaper);

  if (!paper) {
    return (
      <Screen>
        {error ? <ErrorView message={error.message} onRetry={() => reload()} /> : null}
        {loading ? <LoadingView /> : null}
      </Screen>
    );
  }

  // 연구 배경 → 새로 밝혀진 것 → 한계. 요약 전인 논문은 세 칸 모두 null
  const summaryRows = (
    [
      ['연구 배경', paper.summary_background],
      ['새로 밝혀진 것', paper.summary_finding],
      ['한계', paper.summary_limitation],
    ] as const
  ).filter(([, text]) => text);
  // 'other'는 분류 규칙으로 판단하지 못한 논문이라 유형 이름을 보여주지 않는다
  const studyType = paper.study_type !== 'other' ? paper.study_type : null;
  const meta = [paper.journal, paper.published_at].filter(Boolean).join(' · ');
  const pubmedUrl = paper.pubmed_url;

  return (
    <Screen>
      <ThemedText type="title">{displayTitle(paper)}</ThemedText>
      {paper.title_ko ? (
        <ThemedText type="small" themeColor="textSecondary">
          {paper.title}
        </ThemedText>
      ) : null}
      <View style={styles.row}>
        {paper.evidence_grade ? <EvidenceBadge level={paper.evidence_grade} /> : null}
        {/* 등급이 없으면 배지는 숨기고, 연구 유형도 모르면 "확인 중"으로 안내한다 (10/14 현서 님 의견) */}
        <ThemedText type="small" themeColor="textSecondary">
          {studyType ?? (paper.evidence_grade ? null : '연구 유형 확인 중')}
        </ThemedText>
      </View>
      {meta ? (
        <ThemedText type="small" themeColor="textSecondary">
          {meta}
        </ThemedText>
      ) : null}

      <Card>
        <ThemedText type="subtitle">📌 한눈에 보기</ThemedText>
        {summaryRows.length > 0 ? (
          summaryRows.map(([label, text]) => (
            <View key={label} style={styles.summaryRow}>
              <ThemedText type="smallBold" themeColor="textSecondary">
                {label}
              </ThemedText>
              <ThemedText>{text}</ThemedText>
            </View>
          ))
        ) : (
          <ThemedText themeColor="textSecondary">이 연구의 쉬운 요약을 준비하고 있어요.</ThemedText>
        )}
      </Card>

      {reason ? (
        <View style={[styles.personal, { backgroundColor: theme.highlight }]}>
          <ThemedText type="subtitle">💡 우리 상황에서는?</ThemedText>
          <ThemedText>{reason}</ThemedText>
        </View>
      ) : null}

      <Disclaimer text="이 내용은 의학적 진단이나 치료를 대신하지 않습니다." />
      <Button
        label="💬 이 연구에 대해 질문하기"
        onPress={() => router.navigate({ pathname: '/chat', params: { paperId: paper.id } })}
      />
      {pubmedUrl ? (
        <Button
          label="📄 원문 보기 (PubMed)"
          variant="secondary"
          onPress={() => WebBrowser.openBrowserAsync(pubmedUrl)}
        />
      ) : null}
    </Screen>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    flexWrap: 'wrap',
    gap: Spacing.two,
  },
  summaryRow: {
    gap: Spacing.one,
  },
  personal: {
    borderRadius: Radius.card,
    padding: Spacing.three,
    gap: Spacing.two,
  },
});
