import { router, useLocalSearchParams } from 'expo-router';
import * as WebBrowser from 'expo-web-browser';
import { useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Button, Card, Chip, Disclaimer, EvidenceBadge, Screen } from '@/components/ui';
import { Radius, Spacing } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';
import { getMockPaper } from '@/mocks/feed';
import { mockProfile } from '@/mocks/user';
import { displayTitle, type ReadingLevel } from '@/types';

export default function PaperDetailScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const paper = getMockPaper(id);
  const theme = useTheme();
  const [level, setLevel] = useState<ReadingLevel>(mockProfile.reading_level);
  const summaryRows: [string, string | null][] = [
    ['연구 배경', paper.summary_background],
    ['새로 밝혀진 것', paper.summary_finding],
    ['한계', paper.summary_limitation],
  ];

  return (
    <Screen>
      <View style={styles.row}>
        <ThemedText type="smallBold">설명</ThemedText>
        <Chip label="쉽게" selected={level === 'easy'} onPress={() => setLevel('easy')} />
        <Chip label="자세하게" selected={level === 'detail'} onPress={() => setLevel('detail')} />
      </View>

      <ThemedText type="title">{displayTitle(paper)}</ThemedText>
      <View style={styles.row}>
        {paper.evidence_grade ? <EvidenceBadge level={paper.evidence_grade} /> : null}
        <ThemedText type="small" themeColor="textSecondary">
          {paper.study_type}
        </ThemedText>
      </View>
      <ThemedText type="small" themeColor="textSecondary">
        {paper.journal} · {paper.published_at} · {paper.authors}
      </ThemedText>

      <Card>
        <ThemedText type="subtitle">📌 한눈에 보기</ThemedText>
        {summaryRows.map(([label, text]) => (
          <View key={label} style={styles.summaryRow}>
            <ThemedText type="smallBold" themeColor="textSecondary">
              {label}
            </ThemedText>
            <ThemedText>{text ?? '요약을 준비하고 있어요.'}</ThemedText>
          </View>
        ))}
      </Card>

      <View style={styles.section}>
        <ThemedText type="subtitle">📖 쉬운 요약</ThemedText>
        {paper.body[level].map((p) => (
          <ThemedText key={p.text}>
            {p.text}
            {p.citation ? <ThemedText type="smallBold" themeColor="primary"> [{p.citation}]</ThemedText> : null}
          </ThemedText>
        ))}
      </View>

      {paper.personal_reason ? (
        <View style={[styles.personal, { backgroundColor: theme.highlight }]}>
          <ThemedText type="subtitle">💡 우리 상황에서는?</ThemedText>
          <ThemedText>{paper.personal_reason}</ThemedText>
        </View>
      ) : null}

      <View style={styles.section}>
        <ThemedText type="subtitle">⚠️ 한계와 주의점</ThemedText>
        {paper.limitations.map((l) => (
          <ThemedText key={l}>• {l}</ThemedText>
        ))}
      </View>

      <View style={styles.section}>
        <ThemedText type="subtitle">📚 용어 풀이</ThemedText>
        {paper.glossary.map((g) => (
          <ThemedText key={g.term}>
            <ThemedText type="bold">{g.term}</ThemedText>: {g.meaning}
          </ThemedText>
        ))}
      </View>

      <Disclaimer text="이 내용은 의학적 진단이나 치료를 대신하지 않습니다." />
      <Button
        label="💬 이 연구에 대해 질문하기"
        onPress={() => router.navigate({ pathname: '/chat', params: { paperId: paper.id } })}
      />
      <Button
        label="📄 원문 보기 (PubMed)"
        variant="secondary"
        onPress={() => WebBrowser.openBrowserAsync(paper.pubmed_url)}
      />
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
  section: {
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
