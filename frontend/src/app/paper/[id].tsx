import { router, useLocalSearchParams } from 'expo-router';
import * as WebBrowser from 'expo-web-browser';
import { useCallback } from 'react';
import { StyleSheet, View } from 'react-native';

import { getPaper } from '@/api';
import { ThemedText } from '@/components/themed-text';
import { Button, Disclaimer, ErrorView, EvidenceExplain, LoadingView, Screen } from '@/components/ui';
import { studyTypeLabel } from '@/constants/labels';
import { FontFamily, Spacing } from '@/constants/theme';
import { useApi } from '@/hooks/use-api';
import { useTheme } from '@/hooks/use-theme';
import { displayTitle } from '@/types';
import { formatDate } from '@/utils/date';

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

  // 연구 배경 → 새로 밝혀진 것 → 한계. 요약 전인 논문은 세 칸 모두 null. 번호는 기사 머리말처럼 01·02·03
  const summaryRows = (
    [
      ['연구 배경', paper.summary_background],
      ['새로 밝혀진 것', paper.summary_finding],
      ['한계', paper.summary_limitation],
    ] as const
  ).filter(([, text]) => text);
  const meta = [paper.journal, formatDate(paper.published_at)].filter(Boolean).join(' · ');
  const pubmedUrl = paper.pubmed_url;

  return (
    <Screen>
      <View style={styles.header}>
        {/* 연구 유형을 모르면 "연구 유형 확인 중" (10/14 현서 님, 10/24 주현 님 표) */}
        <ThemedText type="smallBold" themeColor="textSecondary">
          {studyTypeLabel(paper.study_type)}
        </ThemedText>
        <ThemedText type="headline">
          {displayTitle(paper)}
        </ThemedText>
        {paper.title_ko ? (
          <ThemedText type="small" themeColor="textSecondary">
            {paper.title}
          </ThemedText>
        ) : null}
        {meta ? (
          <ThemedText type="small" themeColor="textSecondary" style={[styles.meta, { borderTopColor: theme.rule }]}>
            {meta}
          </ThemedText>
        ) : null}
      </View>

      {/* 등급이 없으면 상자는 숨긴다 */}
      {paper.evidence_grade ? <EvidenceExplain level={paper.evidence_grade} /> : null}

      {summaryRows.length > 0 ? (
        summaryRows.map(([label, text], index) => (
          <View key={label} style={styles.section}>
            <View style={[styles.sectionHead, { borderBottomColor: theme.border }]} accessibilityRole="header">
              <ThemedText type="subtitle" themeColor="accent" style={styles.number}>
                {String(index + 1).padStart(2, '0')}
              </ThemedText>
              <ThemedText type="bold">{label}</ThemedText>
            </View>
            <ThemedText>{text}</ThemedText>
          </View>
        ))
      ) : (
        <ThemedText themeColor="textSecondary">이 연구의 쉬운 요약을 준비하고 있어요.</ThemedText>
      )}

      {reason ? (
        <View style={[styles.personal, { backgroundColor: theme.highlight }]}>
          <ThemedText type="smallBold" themeColor="accent">
            우리 상황에서는?
          </ThemedText>
          <ThemedText>{reason}</ThemedText>
        </View>
      ) : null}

      <View style={styles.actions}>
        <Button
          label="이 연구에 대해 질문하기"
          onPress={() => router.navigate({ pathname: '/chat', params: { paperTitle: displayTitle(paper) } })}
        />
        {pubmedUrl ? (
          <Button
            label="원문 보기 (PubMed)"
            variant="secondary"
            onPress={() => WebBrowser.openBrowserAsync(pubmedUrl)}
          />
        ) : null}
      </View>
      <Disclaimer text="이 내용은 의학적 진단이나 치료를 대신하지 않습니다." />
    </Screen>
  );
}

const styles = StyleSheet.create({
  header: {
    gap: Spacing.two,
  },
  meta: {
    borderTopWidth: 1,
    paddingTop: Spacing.two,
    marginTop: Spacing.one,
  },
  section: {
    gap: Spacing.two,
  },
  sectionHead: {
    flexDirection: 'row',
    alignItems: 'baseline',
    gap: Spacing.two,
    borderBottomWidth: StyleSheet.hairlineWidth,
    paddingBottom: Spacing.one,
  },
  number: {
    fontFamily: FontFamily.serif,
  },
  personal: {
    padding: Spacing.three,
    gap: Spacing.two,
  },
  actions: {
    gap: Spacing.two,
  },
});
