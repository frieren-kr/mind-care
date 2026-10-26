import { router, useLocalSearchParams } from 'expo-router';
import * as WebBrowser from 'expo-web-browser';
import { useCallback } from 'react';
import { StyleSheet, View } from 'react-native';

import { getPaper } from '@/api';
import { ThemedText } from '@/components/themed-text';
import {
  Button,
  Disclaimer,
  ErrorView,
  EvidenceBadge,
  LoadingView,
  Screen,
  SectionLabel,
} from '@/components/ui';
import { studyTypeLabel } from '@/constants/labels';
import { Spacing } from '@/constants/theme';
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
  const meta = [paper.journal, paper.published_at].filter(Boolean).join(' · ');
  const pubmedUrl = paper.pubmed_url;

  return (
    <Screen>
      <View style={styles.header}>
        {/* 등급이 없으면 배지는 숨긴다. 연구 유형을 모르면 "연구 유형 확인 중" (10/14 현서 님, 10/24 주현 님 표) */}
        <View style={styles.row}>
          {paper.evidence_grade ? <EvidenceBadge level={paper.evidence_grade} /> : null}
          <ThemedText type="smallBold" themeColor="textSecondary">
            {studyTypeLabel(paper.study_type)}
          </ThemedText>
        </View>
        <ThemedText type="title">{displayTitle(paper)}</ThemedText>
        {paper.title_ko ? (
          <ThemedText type="small" themeColor="textSecondary">
            {paper.title}
          </ThemedText>
        ) : null}
        {meta ? (
          <ThemedText type="small" themeColor="textSecondary">
            {meta}
          </ThemedText>
        ) : null}
      </View>

      <View style={[styles.section, { borderTopColor: theme.border }]}>
        <SectionLabel>한눈에 보기</SectionLabel>
        {summaryRows.length > 0 ? (
          summaryRows.map(([label, text]) => (
            <View
              key={label}
              style={[
                styles.summaryRow,
                // 핵심인 '새로 밝혀진 것'만 강조색 띠
                { borderLeftColor: label === '새로 밝혀진 것' ? theme.primary : theme.border },
              ]}>
              <ThemedText type="smallBold" themeColor="textSecondary">
                {label}
              </ThemedText>
              <ThemedText>{text}</ThemedText>
            </View>
          ))
        ) : (
          <ThemedText themeColor="textSecondary">이 연구의 쉬운 요약을 준비하고 있어요.</ThemedText>
        )}
      </View>

      {reason ? (
        <View style={[styles.personal, { backgroundColor: theme.highlight, borderLeftColor: theme.accent }]}>
          <ThemedText type="smallBold" themeColor="accent">
            우리 상황에서는?
          </ThemedText>
          <ThemedText>{reason}</ThemedText>
        </View>
      ) : null}

      <Disclaimer text="이 내용은 의학적 진단이나 치료를 대신하지 않습니다." />
      <View style={styles.actions}>
        <Button
          label="이 연구에 대해 질문하기"
          onPress={() => router.navigate({ pathname: '/chat', params: { paperId: paper.id } })}
        />
        {pubmedUrl ? (
          <Button
            label="원문 보기 (PubMed)"
            variant="secondary"
            onPress={() => WebBrowser.openBrowserAsync(pubmedUrl)}
          />
        ) : null}
      </View>
    </Screen>
  );
}

const styles = StyleSheet.create({
  header: {
    gap: Spacing.two,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    flexWrap: 'wrap',
    gap: Spacing.two,
  },
  section: {
    borderTopWidth: StyleSheet.hairlineWidth,
    paddingTop: Spacing.four,
    gap: Spacing.four,
  },
  summaryRow: {
    borderLeftWidth: 3,
    paddingLeft: Spacing.three,
    gap: Spacing.one,
  },
  personal: {
    borderLeftWidth: 4,
    padding: Spacing.three,
    gap: Spacing.two,
  },
  actions: {
    gap: Spacing.two,
  },
});
