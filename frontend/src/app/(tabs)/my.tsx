import { useState } from 'react';
import { StyleSheet, View } from 'react-native';

import { ThemedText } from '@/components/themed-text';
import { Card, Chip, Screen } from '@/components/ui';
import { Spacing } from '@/constants/theme';
import { mockProfile } from '@/mocks/user';
import type { ReadingLevel } from '@/types';

export default function MyScreen() {
  const p = mockProfile;
  // 백엔드 연동 전에는 화면 안에서만 바뀐다
  const [level, setLevel] = useState<ReadingLevel>(p.reading_level);

  return (
    <Screen>
      <Card>
        <ThemedText type="subtitle">{p.user_name}님</ThemedText>
        <ThemedText themeColor="textSecondary">
          {p.user_type === 'caregiver' ? '간병인' : '본인'} · {p.patient.relation} ({p.patient.stage})
        </ThemedText>
        <ThemedText themeColor="textSecondary">주요 증상: {p.patient.symptoms.join(', ')}</ThemedText>
      </Card>

      <Card>
        <ThemedText type="subtitle">어떤 설명이 편하세요?</ThemedText>
        <View style={styles.row}>
          <Chip label="쉽게" selected={level === 'easy'} onPress={() => setLevel('easy')} />
          <Chip label="자세하게" selected={level === 'detail'} onPress={() => setLevel('detail')} />
        </View>
      </Card>

      <Card>
        <ThemedText type="subtitle">도움이 필요하면</ThemedText>
        <ThemedText>치매상담콜센터 1899-9988 (24시간)</ThemedText>
        <ThemedText>가까운 치매안심센터에서 무료 검사를 받을 수 있어요</ThemedText>
      </Card>
    </Screen>
  );
}

const styles = StyleSheet.create({
  row: {
    flexDirection: 'row',
    gap: Spacing.two,
  },
});
